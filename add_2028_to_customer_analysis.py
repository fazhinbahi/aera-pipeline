"""
Add 2028 AdjFC columns to customer_analysis in BigQuery.

Steps:
  1. Download existing customer_analysis from BQ (18k rows)
  2. Load adjfc_2028.parquet and pivot to wide format
     (grain: Material Number × Country Name × Sub-Segments × Customer Number)
  3. Left-join 2028 columns onto existing table
  4. Fill missing months with 0
  5. Re-upload to BQ (full replace)

New columns added:
  AdjFC_Jan_2028 … AdjFC_Dec_2028, AdjFC_Total_2028
  (months with no data in Aera yet will be 0)
"""

import os, sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_to_bq import _client, _upload

DIR    = os.path.dirname(os.path.abspath(__file__))
GCP    = "euphoric-hull-442815-n8"
DS     = "aera_demand_planning"
TABLE  = "customer_analysis"

MONTHS_2028 = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
JOIN_KEYS   = ["Material_Number","Country_Name","Sub_Segments","Customer_Number"]


def download_customer_analysis(client) -> pd.DataFrame:
    print("Downloading existing customer_analysis from BigQuery…")
    q   = f"SELECT * FROM `{GCP}.{DS}.{TABLE}`"
    rows = client.query(q).result()
    df   = pd.DataFrame([dict(r) for r in rows])
    print(f"  {len(df):,} rows × {len(df.columns)} cols")
    return df


def build_2028_wide() -> pd.DataFrame:
    """Pivot adjfc_2028.parquet to one row per grain with AdjFC_Mon_2028 columns."""
    print("Loading adjfc_2028.parquet…")
    raw = pd.read_parquet(os.path.join(DIR, "adjfc_2028.parquet"))
    raw = raw[raw["Month Year"].str.contains("2028", na=False)].copy()
    raw["Month_Short"] = raw["Month Year"].str.replace(" 2028", "", regex=False)

    # Pivot to wide
    pivot = raw.pivot_table(
        index=["Material Number", "Country Name", "Sub-Segments", "Customer Number"],
        columns="Month_Short",
        values="Adj FC 9LC",
        aggfunc="sum",
    ).reset_index()
    pivot.columns.name = None

    # Rename to BQ-safe column names and ensure all 12 months present
    rename = {
        "Material Number": "Material_Number",
        "Country Name":    "Country_Name",
        "Sub-Segments":    "Sub_Segments",
        "Customer Number": "Customer_Number",
    }
    for m in MONTHS_2028:
        if m in pivot.columns:
            rename[m] = f"AdjFC_{m}_2028"
        else:
            pivot[m] = 0.0
            rename[m] = f"AdjFC_{m}_2028"

    pivot = pivot.rename(columns=rename)

    # Fill NaN → 0 for all month columns
    for m in MONTHS_2028:
        col = f"AdjFC_{m}_2028"
        pivot[col] = pivot[col].fillna(0)

    pivot["AdjFC_Total_2028"] = pivot[[f"AdjFC_{m}_2028" for m in MONTHS_2028]].sum(axis=1)

    print(f"  {len(pivot):,} 2028 grain rows (non-zero after pivot)")
    print(f"  Months with data: {[m for m in MONTHS_2028 if pivot[f'AdjFC_{m}_2028'].sum() > 0]}")
    return pivot


def merge_and_upload(client):
    existing = download_customer_analysis(client)
    fc28     = build_2028_wide()

    # Drop any existing 2028 columns (idempotent re-run)
    cols_2028 = [c for c in existing.columns if "2028" in c]
    if cols_2028:
        print(f"  Dropping existing 2028 columns: {cols_2028}")
        existing.drop(columns=cols_2028, inplace=True)

    print("Merging 2028 columns into customer_analysis…")
    merged = existing.merge(fc28, on=JOIN_KEYS, how="left")

    # Fill missing grains (no 2028 forecast) with 0
    new_cols = [f"AdjFC_{m}_2028" for m in MONTHS_2028] + ["AdjFC_Total_2028"]
    for col in new_cols:
        if col in merged.columns:
            merged[col] = merged[col].fillna(0)
        else:
            merged[col] = 0.0

    grains_with_2028 = (merged["AdjFC_Total_2028"] > 0).sum()
    print(f"  Merged result: {len(merged):,} rows × {len(merged.columns)} cols")
    print(f"  Grains with any 2028 forecast: {grains_with_2028:,}")

    print("Uploading updated customer_analysis to BigQuery (full replace)…")
    _upload(client, merged, TABLE)
    print(f"✓ Done — customer_analysis now includes AdjFC Jan–Dec 2028 + AdjFC_Total_2028")


def main():
    client = _client()
    merge_and_upload(client)

    # Verify
    print("\nVerifying new columns in BigQuery…")
    q = f"""
    SELECT
      COUNT(*) AS total_rows,
      COUNTIF(AdjFC_Total_2028 > 0) AS grains_with_2028_fc,
      ROUND(SUM(AdjFC_Total_2028), 1) AS sum_2028_total_9lc
    FROM `{GCP}.{DS}.{TABLE}`
    """
    rows = client.query(q).result()
    for r in rows:
        print(f"  Total rows   : {r['total_rows']:,}")
        print(f"  Grains w/ 2028 FC: {r['grains_with_2028_fc']:,}")
        print(f"  Total 2028 9LC : {r['sum_2028_total_9lc']:,}")


if __name__ == "__main__":
    main()
