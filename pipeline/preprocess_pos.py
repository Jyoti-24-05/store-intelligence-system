"""
pipeline/preprocess_pos.py
--------------------------
Transforms raw Brigade_Bangalore retail CSV (101 rows, 39 cols, multi-SKU)
into the spec-compliant pos_transactions.csv (24 rows, one per order).

Input:  data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv
Output: data/pos_transactions.csv        (for pos_correlator)
        data/pos_transactions_enriched.csv (for internal analytics)

Run:
    python pipeline/preprocess_pos.py
    python pipeline/preprocess_pos.py --input "data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv" --output data/pos_transactions.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def preprocess_pos(input_path: str, output_path: str) -> pd.DataFrame:
    df = pd.read_csv(input_path)

    # ── Step 1: Parse timestamps ──────────────────────────────────────────────
    # order_date: "10-04-2026" (DD-MM-YYYY)   order_time: "16:55:36"
    df["timestamp"] = pd.to_datetime(
        df["order_date"] + " " + df["order_time"],
        format="%d-%m-%Y %H:%M:%S",
    ).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Step 2: Aggregate to order level ─────────────────────────────────────
    # basket_value = sum of all total_amount lines (includes GWP/carry bags)
    # Deliberate: zero-value promotional lines (GWP pouches, carry bags)
    # are included — they represent real basket composition even if ₹0.
    agg = (
        df.groupby("order_id")
        .agg(
            store_id         = ("store_id",        "first"),
            transaction_id   = ("invoice_number",  "first"),
            timestamp        = ("timestamp",        "first"),  # same for all lines
            basket_value_inr = ("total_amount",     "sum"),
            customer_name    = ("customer_name",    "first"),
            item_count       = ("qty",              "sum"),
            departments      = ("dep_name",         lambda x: sorted(x.dropna().unique().tolist())),
            salespersons     = ("salesperson_name", lambda x: sorted(x.dropna().unique().tolist())),
        )
        .reset_index()
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    # ── Step 3: Validate — expect exactly 24 orders ──────────────────────────
    n = len(agg)
    if n != 24:
        print(f"WARNING: Expected 24 orders, got {n}. Check input file.")

    # ── Step 4: Write spec-compliant output ───────────────────────────────────
    spec_df = agg[["store_id", "transaction_id", "timestamp", "basket_value_inr"]].copy()
    spec_df.to_csv(output_path, index=False)
    print(f"Preprocessed {n} transactions → {output_path}")
    print(spec_df.to_string())

    # ── Step 5: Write enriched output for analytics ───────────────────────────
    enriched_path = output_path.replace(".csv", "_enriched.csv")
    agg["departments"] = agg["departments"].apply(str)
    agg["salespersons"] = agg["salespersons"].apply(str)
    agg.to_csv(enriched_path, index=False)
    print(f"Enriched output → {enriched_path}")

    return spec_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default="data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv")
    parser.add_argument("--output", default="data/pos_transactions.csv")
    args = parser.parse_args()
    preprocess_pos(args.input, args.output)