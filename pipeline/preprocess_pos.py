"""
pipeline/preprocess_pos.py
--------------------------
Transforms raw POS CSVs (multi-SKU, one row per line item) into the
spec-compliant pos_transactions_<STORE_ID>.csv (one row per order) that
pos_correlator expects.

Multi-store design
------------------
Each store has a slightly different raw CSV schema.  A thin per-store
"schema descriptor" dict drives column mapping so the core aggregation
logic stays identical.  Adding Store 3 means adding one new descriptor.

Store 1 — ST1008, Brigade Road, Bangalore
  File   : Brigade_Bangalore_10_April_26 (1)bc6219c.csv
  Key columns (subset of 39):
    order_id, invoice_number, order_date (DD-MM-YYYY), order_time,
    store_id, total_amount, qty, dep_name, salesperson_name, customer_name
  Transaction ID : invoice_number  (order_id groups lines within one order)
  Expected orders: 24

Store 2 — ST1076, Purplle Store 1076, Mumbai
  File   : pos_transactions_ST1076.csv
  Key columns:
    order_id, order_date (DD-MM-YYYY), order_time,
    store_id, product_id, brand_name, total_amount
  Transaction ID : order_id  (there is no separate invoice_number)
  Expected orders: None (not validated — count unknown at spec time)

Output (both stores)
--------------------
  data/pos_transactions_<STORE_ID>.csv       ← spec-compliant (4 cols)
  data/pos_transactions_<STORE_ID>_enriched.csv ← analytics (all agg cols)

  Spec output schema:
    store_id, transaction_id, timestamp, basket_value_inr

Run examples
------------
  # Store 1 (explicit store-id):
  python pipeline/preprocess_pos.py \\
      --input  "data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv" \\
      --output data/pos_transactions_ST1008.csv \\
      --store-id ST1008

  # Store 2:
  python pipeline/preprocess_pos.py \\
      --input  data/pos_transactions_ST1076.csv \\
      --output data/pos_transactions_ST1076.csv \\
      --store-id ST1076

  # Legacy call (no --store-id) — inferred from output filename, falls back to ST1008:
  python pipeline/preprocess_pos.py \\
      --input  "data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv" \\
      --output data/pos_transactions.csv
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Per-store schema descriptors
# ─────────────────────────────────────────────────────────────────────────────

# Each descriptor controls:
#   group_by       — column whose distinct values define one order / transaction
#   transaction_id — column to use as the canonical transaction_id in output
#                    (None → use group_by column)
#   date_col       — column holding the date string
#   time_col       — column holding the time string
#   date_format    — strptime format string for date + time combined
#   amount_col     — column to sum for basket_value_inr
#   qty_col        — column to sum for item_count (None → skip)
#   store_id_col   — column that carries the store_id (None → use --store-id arg)
#   expected_orders— int for exact-count validation, None to skip
#   enrichment_cols— list of (output_name, source_col, agg_fn) for the
#                    enriched CSV.  agg_fn is a string key into AGG_FNS below.
#                    Columns absent in a given CSV are silently skipped.

_AGG_FNS: dict[str, Any] = {
    "first":        "first",
    "sum":          "sum",
    "sorted_unique": lambda x: sorted(x.dropna().unique().tolist()),
}

STORE_SCHEMAS: dict[str, dict] = {
    "ST1008": {
        "group_by":        "order_id",
        "transaction_id":  "invoice_number",   # distinct from order_id in ST1008
        "date_col":        "order_date",
        "time_col":        "order_time",
        "date_format":     "%d-%m-%Y %H:%M:%S",
        "amount_col":      "total_amount",
        "qty_col":         "qty",
        "store_id_col":    "store_id",
        "expected_orders": 24,
        "enrichment_cols": [
            # (output_name,     source_col,        agg_fn)
            ("customer_name",   "customer_name",   "first"),
            ("item_count",      "qty",             "sum"),
            ("departments",     "dep_name",        "sorted_unique"),
            ("salespersons",    "salesperson_name","sorted_unique"),
        ],
    },
    "ST1076": {
        "group_by":        "order_id",
        "transaction_id":  None,               # use order_id itself
        "date_col":        "order_date",
        "time_col":        "order_time",
        "date_format":     "%d-%m-%Y %H:%M:%S",
        "amount_col":      "total_amount",
        "qty_col":         None,               # ST1076 CSV has no qty column
        "store_id_col":    "store_id",
        "expected_orders": None,               # count unknown — skip validation
        "enrichment_cols": [
            ("product_ids",  "product_id",  "sorted_unique"),
            ("brands",       "brand_name",  "sorted_unique"),
        ],
    },
}

# Fallback for unknown store_ids — mirrors ST1008 behaviour (safest default)
_DEFAULT_SCHEMA = STORE_SCHEMAS["ST1008"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _infer_store_id(output_path: str, df: pd.DataFrame) -> str:
    """Best-effort store_id inference when --store-id is not supplied.

    Priority:
      1. 'store_id' column in the CSV (first non-null value)
      2. ST1008 / ST1076 substring in the output filename
      3. Hard fallback: ST1008
    """
    if "store_id" in df.columns:
        val = df["store_id"].dropna().iloc[0] if not df["store_id"].dropna().empty else None
        if val:
            return str(val).strip()

    name = Path(output_path).stem.upper()
    for sid in STORE_SCHEMAS:
        if sid in name:
            return sid

    return "ST1008"


def _select_schema(store_id: str) -> dict:
    return STORE_SCHEMAS.get(store_id, _DEFAULT_SCHEMA)


def _col(df: pd.DataFrame, name: str | None) -> bool:
    """True if column name is non-None and present in df."""
    return name is not None and name in df.columns


# ─────────────────────────────────────────────────────────────────────────────
# Core preprocessor
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_pos(
    input_path:  str,
    output_path: str,
    store_id:    str | None = None,
) -> pd.DataFrame:
    """Aggregate a raw multi-SKU POS CSV to one row per order.

    Parameters
    ----------
    input_path  : path to raw CSV file
    output_path : destination for spec-compliant 4-column CSV
    store_id    : ST1008 | ST1076 | …  (inferred from CSV / filename if None)

    Returns
    -------
    spec_df : the 4-column spec-compliant DataFrame (also written to disk)
    """
    df = pd.read_csv(input_path)
    print(f"Read {len(df)} rows from {input_path}")

    # ── Resolve store_id & schema ─────────────────────────────────────────────
    if store_id is None:
        store_id = _infer_store_id(output_path, df)
        print(f"Inferred store_id: {store_id}")
    else:
        print(f"Store ID: {store_id}")

    schema = _select_schema(store_id)

    # ── Step 1: Parse timestamps ──────────────────────────────────────────────
    date_col = schema["date_col"]
    time_col = schema["time_col"]
    fmt      = schema["date_format"]

    if not _col(df, date_col) or not _col(df, time_col):
        raise ValueError(
            f"Expected date/time columns '{date_col}' and '{time_col}' "
            f"not found in {input_path}.\n"
            f"Available columns: {list(df.columns)}"
        )

    df["_ts_raw"] = df[date_col].str.strip() + " " + df[time_col].str.strip()
    df["timestamp"] = pd.to_datetime(df["_ts_raw"], format=fmt, errors="coerce")

    bad_ts = df["timestamp"].isna().sum()
    if bad_ts > 0:
        print(f"WARNING: {bad_ts} rows had unparseable timestamps — they will "
              f"use NaT and may be dropped by pos_correlator.")

    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Step 2: Resolve store_id column (or fall back to arg) ─────────────────
    sid_col = schema["store_id_col"]
    if _col(df, sid_col):
        # Normalise whitespace in store_id values from the CSV
        df[sid_col] = df[sid_col].astype(str).str.strip()
    else:
        # No store_id in CSV — inject the arg value so aggregation can use "first"
        df["_store_id_injected"] = store_id
        sid_col = "_store_id_injected"

    # ── Step 3: Build aggregation spec ───────────────────────────────────────
    group_col  = schema["group_by"]
    txn_col    = schema["transaction_id"]   # None → use group_col
    amount_col = schema["amount_col"]
    qty_col    = schema["qty_col"]

    if not _col(df, group_col):
        raise ValueError(
            f"Group-by column '{group_col}' not found in {input_path}."
        )
    if not _col(df, amount_col):
        raise ValueError(
            f"Amount column '{amount_col}' not found in {input_path}."
        )

    # Core aggregation — always present
    agg_spec: dict[str, Any] = {
        "store_id":         (sid_col,    "first"),
        "timestamp":        ("timestamp","first"),  # all lines share same order time
        "basket_value_inr": (amount_col, "sum"),
    }

    # transaction_id column — present in ST1008, absent in ST1076
    if txn_col is not None and _col(df, txn_col):
        agg_spec["transaction_id"] = (txn_col, "first")
    # else: we'll assign transaction_id = order_id after groupby

    # Optional enrichment columns — only add if column exists in this CSV
    for out_name, src_col, fn_key in schema.get("enrichment_cols", []):
        if _col(df, src_col):
            agg_spec[out_name] = (src_col, _AGG_FNS[fn_key])

    # ── Step 4: Aggregate ─────────────────────────────────────────────────────
    agg = (
        df.groupby(group_col)
        .agg(**agg_spec)
        .reset_index()
        .rename(columns={group_col: "order_id"})
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    # If there was no separate transaction_id column, use order_id as the txn ID
    if "transaction_id" not in agg.columns:
        agg["transaction_id"] = agg["order_id"]

    # ── Step 5: Validate order count ─────────────────────────────────────────
    n = len(agg)
    expected = schema["expected_orders"]
    if expected is not None:
        if n != expected:
            print(f"WARNING: Expected {expected} orders for {store_id}, "
                  f"got {n}. Check input file.")
        else:
            print(f"Order count validated: {n} orders ✓")
    else:
        print(f"Aggregated to {n} orders.")

    # ── Step 6: Write spec-compliant output ───────────────────────────────────
    spec_cols = ["store_id", "transaction_id", "timestamp", "basket_value_inr"]
    spec_df = agg[spec_cols].copy()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    spec_df.to_csv(output_path, index=False)
    print(f"Spec output  → {output_path}  ({n} rows)")
    print(spec_df.to_string())

    # ── Step 7: Write enriched output ─────────────────────────────────────────
    # Serialise list columns so they round-trip as strings in CSV
    enriched = agg.copy()
    for col in enriched.columns:
        if enriched[col].apply(lambda v: isinstance(v, list)).any():
            enriched[col] = enriched[col].apply(str)

    enriched_path = str(output_path).replace(".csv", "_enriched.csv")
    enriched.to_csv(enriched_path, index=False)
    print(f"Enriched output → {enriched_path}")

    return spec_df


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Preprocess raw multi-SKU POS CSV to one-row-per-order format."
    )
    parser.add_argument(
        "--input",
        default="data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv",
        help="Path to raw POS CSV",
    )
    parser.add_argument(
        "--output",
        default="data/pos_transactions.csv",
        help="Destination for spec-compliant output CSV",
    )
    parser.add_argument(
        "--store-id",
        default=None,
        dest="store_id",
        help="Store ID (ST1008 | ST1076). Inferred from CSV/filename if omitted.",
    )
    args = parser.parse_args()
    preprocess_pos(args.input, args.output, store_id=args.store_id)