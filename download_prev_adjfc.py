"""
Download yesterday's adjfc_raw from BigQuery and save as adjfc_nz_prev_month.parquet.

This runs at the START of the daily pipeline (before fetch_adjfc.py overwrites the
local adjfc_nz.parquet). In GitHub Actions all parquets are gitignored so the previous
month's file never persists between runs. Instead we use the adjfc_raw BQ table, which
still holds the PREVIOUS run's data at this point in the pipeline.

Result: adjfc_nz_prev_month.parquet with original column names that build_pmcf_pivot()
expects (spaces and hyphens preserved).
"""

import os
import sys

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from load_to_bq import _client

GCP   = "euphoric-hull-442815-n8"
DS    = "aera_demand_planning"
OUT   = os.path.join(DIR, "adjfc_nz_prev_month.parquet")

# Map BQ-sanitised column names back to the originals that build_pmcf_pivot() reads
BQ_TO_ORIG = {
    "Business_Segment":    "Business Segment",
    "Sub_Segments":        "Sub-Segments",
    "Country_Name":        "Country Name",
    "Customer_Number":     "Customer Number",
    "Distributor_Name":    "Distributor Name",
    "Month_Year":          "Month Year",
    "Material_Number":     "Material Number",
    "Adjusted_FC":         "Adjusted FC",
    "Adj_FC_9LC":          "Adj FC 9LC",
    "Adj_FC_Actuals":      "Adj FC + Actuals",
    "Sub_Brand_Long_Description":           "Sub-Brand Long Description",
    "External_Material_Group_Description":  "External Material Group Description",
    "Category_Grouper_Description_Z":       "Category Grouper Description (Z)",
}


def main():
    client = _client()

    print("Downloading adjfc_raw from BigQuery (previous run's AdjFC)…")
    q  = f"SELECT * FROM `{GCP}.{DS}.adjfc_raw`"
    df = client.query(q).to_dataframe()
    print(f"  {len(df):,} rows × {df.shape[1]} cols downloaded")

    if df.empty:
        print("  ⚠ adjfc_raw is empty — skipping save (first ever run?)")
        return

    df = df.rename(columns={k: v for k, v in BQ_TO_ORIG.items() if k in df.columns})

    df.to_parquet(OUT, index=False)

    months = sorted(df["Month Year"].unique()) if "Month Year" in df.columns else []
    adj_total = df["Adj FC 9LC"].sum() if "Adj FC 9LC" in df.columns else 0
    print(f"  Months: {months[:6]}{'…' if len(months) > 6 else ''}")
    print(f"  Total Adj FC 9LC: {adj_total:,.0f}")
    print(f"  ✓ Saved → {OUT}")


if __name__ == "__main__":
    main()
