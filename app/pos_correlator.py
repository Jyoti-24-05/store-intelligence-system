"""POS correlation engine.

Correlation rule (exact, from spec)
------------------------------------
A visitor who was in the **billing zone** in the **5-minute window BEFORE**
a POS transaction timestamp counts as a converted visitor for that session.

Billing zone is identified by any event that is:
  - event_type == BILLING_QUEUE_JOIN, OR
  - event_type == ZONE_ENTER with zone_id containing "billing" or "bill"
    (case-insensitive) — covers store layouts that name it "BILLING",
    "billing_counter", etc.

Startup
-------
Call load_pos_csv() once at application startup (via lifespan in main.py)
to seed pos_transactions from the CSV file.

After each ingest batch, ingestion.py calls correlate_recent() which
processes only unmatched transactions.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import select, and_, or_

from app.database import Event as EventRow, POSTransaction, VisitorSession, get_db

# Path to CSV — overridable via env var
POS_CSV_PATH = os.getenv("POS_CSV", "/app/data/pos_transactions.csv")

# Billing zone match: BILLING_QUEUE_JOIN always counts; ZONE_ENTER matches
# zone_ids containing any of these substrings (case-insensitive)
BILLING_ZONE_KEYWORDS = ("billing", "bill", "checkout", "counter", "cashier")

# Correlation window: visitor must have been in billing zone within this
# many minutes BEFORE the transaction timestamp
CORRELATION_WINDOW_MINUTES = 5


# ─────────────────────────────────────────────
# CSV loader (called at startup)
# ─────────────────────────────────────────────

async def load_pos_csv() -> int:
    """Load pos_transactions.csv into the DB.

    Skips rows already present (idempotent). Returns count of newly inserted rows.
    """
    csv_path = Path(POS_CSV_PATH)
    if not csv_path.exists():
        return 0

    inserted = 0
    async with get_db() as db:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                transaction_id = row.get("transaction_id", "").strip()
                if not transaction_id:
                    continue

                # Skip if already loaded
                existing = await db.get(POSTransaction, transaction_id)
                if existing is not None:
                    continue

                # Parse timestamp — support ISO-8601 and common CSV formats
                ts_raw = row.get("timestamp", "").strip()
                try:
                    ts = _parse_timestamp(ts_raw)
                except ValueError:
                    continue  # skip unparseable rows silently

                tx = POSTransaction(
                    transaction_id     = transaction_id,
                    store_id           = row.get("store_id", "").strip(),
                    timestamp          = ts,
                    basket_value_inr   = float(row.get("basket_value_inr", 0) or 0),
                    matched_visitor_id = row.get("matched_visitor_id") or None,
                )
                db.add(tx)
                inserted += 1

        await db.commit()

    return inserted


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
         transaction timestamp (most likely buyer).
      3. Set transaction.matched_visitor_id and mark their session converted.

    Returns count of newly matched transactions.
    """
    matched = 0

    async with get_db() as db:
        # Fetch all unmatched transactions
        unmatched_result = await db.execute(
            select(POSTransaction)
            .where(POSTransaction.matched_visitor_id == None)
            .order_by(POSTransaction.timestamp)
        )
        unmatched_txns = unmatched_result.scalars().all()

        for txn in unmatched_txns:
            window_start = txn.timestamp - timedelta(minutes=CORRELATION_WINDOW_MINUTES)
            window_end   = txn.timestamp

            # Find billing-zone events in the correlation window
            billing_events_result = await db.execute(
                select(EventRow.visitor_id, EventRow.timestamp)
                .where(
                    EventRow.store_id  == txn.store_id,
                    EventRow.timestamp >= window_start,
                    EventRow.timestamp <= window_end,
                    or_(
                        EventRow.event_type == "BILLING_QUEUE_JOIN",
                        and_(
                            EventRow.event_type == "ZONE_ENTER",
                            _billing_zone_filter(),
                        ),
                    ),
                )
                .order_by(EventRow.timestamp.desc())   # most recent first
            )
            billing_events = billing_events_result.all()

            if not billing_events:
                continue

            # Pick the visitor whose billing event is closest to the transaction
            best_visitor_id = billing_events[0].visitor_id

            # Update transaction
            txn.matched_visitor_id = best_visitor_id

            # Mark visitor session as converted
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
    from sqlalchemy import or_, func
    return or_(*(
        func.lower(EventRow.zone_id).contains(kw)
        for kw in BILLING_ZONE_KEYWORDS
    ))


def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO-8601 or common CSV timestamp strings to UTC datetime."""
    # Try standard ISO formats
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