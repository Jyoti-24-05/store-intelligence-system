"""POS correlation engine — multi-store.

Correlation rule (exact, from spec)
------------------------------------
A visitor who was in the **billing zone** in the **5-minute window BEFORE**
a POS transaction timestamp counts as a converted visitor for that session.

Billing zone is identified by any event that is:
  - event_type == BILLING_QUEUE_JOIN, OR
  - event_type == ZONE_ENTER with zone_id containing "billing" or "bill"
    (case-insensitive) — covers store layouts that name it "BILLING",
    "billing_counter", etc.

Multi-store CSV discovery
--------------------------
Rather than a single hardcoded POS_CSV_PATH, the loader now discovers all
per-store CSV files under POS_CSV_DIR (default: /app/data).

Discovery order (first match wins per store):
  1. Explicit paths in POS_CSV_PATHS env var (comma-separated)
  2. Files matching  <POS_CSV_DIR>/pos_transactions_<STORE_ID>.csv
     for each known store in KNOWN_STORE_IDS
  3. Any file matching <POS_CSV_DIR>/pos_transactions_*.csv  (glob fallback)
  4. Legacy single-file path POS_CSV_PATH for backwards compatibility
     (still works if only one store and the old env var is set)

Environment variables
---------------------
  POS_CSV_DIR    — directory to scan (default: /app/data)
  POS_CSV_PATHS  — explicit comma-separated list of CSV paths (overrides scan)
  POS_CSV        — legacy single path (used only when nothing else is found)

Startup
-------
Call load_pos_csv() once at application startup (via lifespan in main.py).
It is idempotent — rows already present are skipped.

After each ingest batch, ingestion.py calls correlate_recent() which
processes only unmatched transactions.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Generator

from sqlalchemy import select, and_, or_

from app.database import Event as EventRow, POSTransaction, VisitorSession, get_db

# ── Environment config ────────────────────────────────────────────────────────

# Directory to scan for pos_transactions_*.csv files
POS_CSV_DIR = os.getenv("POS_CSV_DIR", "/app/data")

# Explicit comma-separated list of paths (highest priority, overrides scan)
POS_CSV_PATHS = os.getenv("POS_CSV_PATHS", "")

# Legacy single-path variable — used as a last-resort fallback
POS_CSV_PATH_LEGACY = os.getenv("POS_CSV", "/app/data/pos_transactions.csv")

# Known store IDs — controls the preferred-filename scan (step 2 in discovery)
# Add new stores here; the glob fallback (step 3) catches anything not listed.
KNOWN_STORE_IDS = ("ST1008", "ST1076")

# Billing zone match: BILLING_QUEUE_JOIN always counts; ZONE_ENTER matches
# zone_ids containing any of these substrings (case-insensitive)
BILLING_ZONE_KEYWORDS = ("billing", "bill", "checkout", "counter", "cashier")

# Correlation window: visitor must have been in billing zone within this
# many minutes BEFORE the transaction timestamp
CORRELATION_WINDOW_MINUTES = 5


# ─────────────────────────────────────────────
# CSV file discovery
# ─────────────────────────────────────────────

def _discover_pos_csvs() -> list[Path]:
    """Return an ordered, deduplicated list of POS CSV paths to load.

    Discovery priority (see module docstring):
      1. POS_CSV_PATHS env var (explicit, comma-separated)
      2. Per-store named files: pos_transactions_<STORE_ID>.csv
      3. Glob: pos_transactions_*.csv  (catches future stores automatically)
      4. Legacy POS_CSV single-path fallback
    """
    seen:  set[Path]  = set()
    paths: list[Path] = []

    def _add(p: Path) -> None:
        p = p.resolve()
        if p not in seen and p.exists() and p.stat().st_size > 0:
            seen.add(p)
            paths.append(p)

    data_dir = Path(POS_CSV_DIR)

    # 1 — Explicit paths from env var
    if POS_CSV_PATHS.strip():
        for raw in POS_CSV_PATHS.split(","):
            raw = raw.strip()
            if raw:
                _add(Path(raw))

    # 2 — Known-store named files (deterministic, no surprises)
    for store_id in KNOWN_STORE_IDS:
        _add(data_dir / f"pos_transactions_{store_id}.csv")

    # 3 — Glob fallback: picks up any store not in KNOWN_STORE_IDS
    for p in sorted(data_dir.glob("pos_transactions_*.csv")):
        # Skip enriched files (pos_transactions_ST1008_enriched.csv)
        if "_enriched" not in p.stem:
            _add(p)

    # 4 — Legacy single-path fallback (backwards compat for Store-1-only deploys)
    legacy = Path(POS_CSV_PATH_LEGACY)
    if "_enriched" not in legacy.stem:
        _add(legacy)

    return paths


# ─────────────────────────────────────────────
# CSV loader (called at startup)
# ─────────────────────────────────────────────

async def load_pos_csv() -> int:
    """Discover and load all per-store POS CSVs into the DB.

    Iterates every CSV returned by _discover_pos_csvs() and upserts rows
    into pos_transactions.  Already-present rows are skipped (idempotent).

    Returns total count of newly inserted rows across all files.
    """
    csv_paths = _discover_pos_csvs()

    if not csv_paths:
        print("[pos_correlator] No POS CSV files found — skipping load.")
        return 0

    total_inserted = 0
    for csv_path in csv_paths:
        n = await _load_single_csv(csv_path)
        print(f"[pos_correlator] Loaded {n} new transactions from {csv_path}")
        total_inserted += n

    print(f"[pos_correlator] Total new POS rows inserted: {total_inserted}")
    return total_inserted


async def _load_single_csv(csv_path: Path) -> int:
    """Insert rows from one CSV file; skip duplicates. Returns new-row count."""
    inserted = 0

    async with get_db() as db:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                transaction_id = row.get("transaction_id", "").strip()
                if not transaction_id:
                    continue

                # Idempotent — skip if already loaded
                existing = await db.get(POSTransaction, transaction_id)
                if existing is not None:
                    continue

                ts_raw = row.get("timestamp", "").strip()
                try:
                    ts = _parse_timestamp(ts_raw)
                except ValueError:
                    # Unparseable timestamp — log and skip rather than crash
                    print(f"[pos_correlator] WARNING: Skipping row with "
                          f"unparseable timestamp {ts_raw!r} in {csv_path.name}")
                    continue

                # store_id: prefer CSV column, fall back to inferring from filename
                store_id = row.get("store_id", "").strip()
                if not store_id:
                    store_id = _infer_store_id_from_filename(csv_path)

                tx = POSTransaction(
                    transaction_id     = transaction_id,
                    store_id           = store_id,
                    timestamp          = ts,
                    basket_value_inr   = float(row.get("basket_value_inr", 0) or 0),
                    matched_visitor_id = row.get("matched_visitor_id") or None,
                )
                db.add(tx)
                inserted += 1

        await db.commit()

    return inserted


def _infer_store_id_from_filename(csv_path: Path) -> str:
    """Extract store_id from a filename like pos_transactions_ST1076.csv.

    Falls back to empty string if no known store_id is found — the correlator
    will still work but cross-store filtering won't apply to those rows.
    """
    stem = csv_path.stem.upper()   # e.g. "POS_TRANSACTIONS_ST1076"
    for sid in KNOWN_STORE_IDS:
        if sid in stem:
            return sid
    # Generic fallback: look for ST followed by digits
    import re
    m = re.search(r"ST\d{4}", stem)
    return m.group(0) if m else ""


# ─────────────────────────────────────────────
# Correlation engine (called after each ingest)
# ─────────────────────────────────────────────

async def correlate_recent() -> int:
    """Correlate all unmatched POS transactions with visitor sessions.

    For each unmatched transaction:
      1. Find all visitors who had a billing-zone event in the 5-minute
         window [transaction.timestamp - 5min, transaction.timestamp]
         for the same store.
      2. Pick the visitor whose billing-zone event is closest to the
         transaction timestamp (most recent first → best candidate).
      3. Set transaction.matched_visitor_id and mark their session converted.

    Returns count of newly matched transactions.

    Store isolation: the store_id column on both EventRow and POSTransaction
    ensures ST1008 visitors are never matched to ST1076 transactions and
    vice versa — no extra logic needed.
    """
    matched = 0

    async with get_db() as db:
        # Fetch all unmatched transactions ordered by time
        unmatched_result = await db.execute(
            select(POSTransaction)
            .where(POSTransaction.matched_visitor_id == None)
            .order_by(POSTransaction.timestamp)
        )
        unmatched_txns = unmatched_result.scalars().all()

        for txn in unmatched_txns:
            window_start = txn.timestamp - timedelta(minutes=CORRELATION_WINDOW_MINUTES)
            window_end   = txn.timestamp

            # Find billing-zone events in the correlation window for this store
            billing_events_result = await db.execute(
                select(EventRow.visitor_id, EventRow.timestamp)
                .where(
                    EventRow.store_id  == txn.store_id,
                    EventRow.timestamp >= window_start,
                    EventRow.timestamp <= window_end,
                    EventRow.is_staff  == False,        # never match staff
                    or_(
                        EventRow.event_type == "BILLING_QUEUE_JOIN",
                        and_(
                            EventRow.event_type == "ZONE_ENTER",
                            _billing_zone_filter(),
                        ),
                    ),
                )
                .order_by(EventRow.timestamp.desc())    # most recent → best match
            )
            billing_events = billing_events_result.all()

            if not billing_events:
                continue

            # Pick the visitor whose billing event is closest to the transaction
            best_visitor_id = billing_events[0].visitor_id

            # Guard: don't re-match a visitor who's already matched to another
            # transaction within the same window (one visitor → one conversion)
            already_matched_result = await db.execute(
                select(POSTransaction.transaction_id)
                .where(
                    POSTransaction.matched_visitor_id == best_visitor_id,
                    POSTransaction.timestamp          >= window_start,
                    POSTransaction.timestamp          <= window_end,
                    POSTransaction.transaction_id     != txn.transaction_id,
                )
                .limit(1)
            )
            if already_matched_result.scalar_one_or_none() is not None:
                # Already matched in this window — skip to next candidate if any
                for row in billing_events[1:]:
                    already = await db.execute(
                        select(POSTransaction.transaction_id)
                        .where(
                            POSTransaction.matched_visitor_id == row.visitor_id,
                            POSTransaction.timestamp          >= window_start,
                            POSTransaction.timestamp          <= window_end,
                            POSTransaction.transaction_id     != txn.transaction_id,
                        )
                        .limit(1)
                    )
                    if already.scalar_one_or_none() is None:
                        best_visitor_id = row.visitor_id
                        break
                else:
                    # All candidates already matched — leave this txn unmatched
                    continue

            # Update transaction
            txn.matched_visitor_id = best_visitor_id

            # Mark the most recent session for this visitor as converted
            session_result = await db.execute(
                select(VisitorSession)
                .where(
                    VisitorSession.visitor_id == best_visitor_id,
                    VisitorSession.store_id   == txn.store_id,
                )
                .order_by(VisitorSession.entry_time.desc())
                .limit(1)
            )
            session = session_result.scalar_one_or_none()
            if session is not None:
                session.converted = True

            matched += 1

        await db.commit()

    return matched


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _billing_zone_filter():
    """SQLAlchemy filter: zone_id contains any billing keyword (case-insensitive)."""
    from sqlalchemy import func
    return or_(*(
        func.lower(EventRow.zone_id).contains(kw)
        for kw in BILLING_ZONE_KEYWORDS
    ))


def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO-8601 or common CSV timestamp strings to a UTC-aware datetime."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts!r}")