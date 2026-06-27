"""
Load Aera demand planning data into BigQuery.

Project  : euphoric-hull-442815-n8
Dataset  : aera_demand_planning
Tables   : sply_analysis       — 63-col consolidated pivot (Material × Country × Sub-Segments)
           adjfc_raw           — full AdjFC parquet (254k rows)
           order_history_raw   — full Order History parquet (38k rows)

Looker Studio connector: BigQuery → project euphoric-hull-442815-n8 → dataset aera_demand_planning

Usage:
  python3.13 load_to_bq.py                  # load all three tables
  python3.13 load_to_bq.py --table sply     # load only sply_analysis
  python3.13 load_to_bq.py --table adjfc
  python3.13 load_to_bq.py --table oh
"""

import argparse
import os
import re
import subprocess
import sys
import time

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import credentials

DIR        = os.path.dirname(os.path.abspath(__file__))
OH_PARQUET   = os.path.join(DIR, "order_history_emea_apac.parquet")
FC_PARQUET   = os.path.join(DIR, "adjfc_nz.parquet")
PMCF_PARQUET = os.path.join(DIR, "pmcf_monthly.parquet")

GCP_PROJECT = "euphoric-hull-442815-n8"
DATASET     = "aera_demand_planning"
GCLOUD_ACC  = "jfaizan07@gmail.com"


def _client() -> bigquery.Client:
    # Cloud/CI: use service account JSON from env var
    sa_json = os.getenv("GCP_SA_JSON")
    if sa_json:
        import json as _json
        from google.oauth2 import service_account
        info = _json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery"]
        )
        return bigquery.Client(project=GCP_PROJECT, credentials=creds)
    # Local dev fallback: gcloud CLI
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        raise SystemExit("✗ Set GCP_SA_JSON env var, or run: gcloud auth login")
    creds = credentials.Credentials(token=token)
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


def _bq_col(name: str) -> str:
    """Sanitise a column name to BigQuery-safe format (a-z, 0-9, _)."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    s = re.sub(r"_+", "_", s).strip("_")
    if s and s[0].isdigit():
        s = "col_" + s
    return s or "col"


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure consistent column types so pyarrow can serialise without errors.
    - Object columns with leading-zero values (IDs/codes) → kept as str
    - Object columns that are purely numeric → float64 (empty string → NaN)
    - Remaining object columns → str
    """
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            sample = df[col].dropna().astype(str)
            # Preserve as string if any non-empty value has a leading zero (e.g. customer/material codes)
            if sample.str.match(r'^0\d').any():
                df[col] = df[col].fillna("").astype(str)
                continue
            numeric = pd.to_numeric(df[col], errors="coerce")
            if numeric.notna().sum() / max(len(df), 1) >= 0.5:
                df[col] = numeric
            else:
                df[col] = df[col].fillna("").astype(str)
    return df


def _upload(client: bigquery.Client, df: pd.DataFrame, table_name: str):
    table_ref = f"{GCP_PROJECT}.{DATASET}.{table_name}"
    print(f"  Uploading {len(df):,} rows × {df.shape[1]} cols → {table_ref}…")

    df = _clean_df(df)
    df.columns = [_bq_col(c) for c in df.columns]

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        autodetect=True,
    )

    t0  = time.time()
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()
    elapsed = time.time() - t0

    tbl = client.get_table(table_ref)
    print(f"  ✓ {tbl.num_rows:,} rows in {table_ref}  ({elapsed:.1f}s)")


def _load_parquets() -> tuple:
    oh = pd.read_parquet(OH_PARQUET)
    fc = pd.read_parquet(FC_PARQUET)
    return oh, fc


def build_sply_analysis() -> pd.DataFrame:
    """Re-run the same logic as upload_sply_analysis.py and return the DataFrame."""
    sys.path.insert(0, DIR)
    from upload_sply_analysis import build_final

    oh, fc = _load_parquets()
    return build_final(oh, fc)


def build_customer_analysis_df() -> pd.DataFrame:
    sys.path.insert(0, DIR)
    from upload_sply_analysis import build_customer_analysis

    oh, fc = _load_parquets()
    return build_customer_analysis(oh, fc)


def load_sply(client: bigquery.Client):
    print("Building SPLY analysis pivot…")
    df = build_sply_analysis()
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "sply_analysis")


def load_customer_analysis(client: bigquery.Client):
    print("Building customer analysis pivot…")
    df = build_customer_analysis_df()
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "customer_analysis")


def load_adjfc(client: bigquery.Client):
    print(f"Loading {os.path.basename(FC_PARQUET)}…")
    df = pd.read_parquet(FC_PARQUET)
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "adjfc_raw")


def load_order_history(client: bigquery.Client):
    print(f"Loading {os.path.basename(OH_PARQUET)}…")
    df = pd.read_parquet(OH_PARQUET)
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "order_history_raw")


def load_pmcf(client: bigquery.Client):
    if not os.path.exists(PMCF_PARQUET):
        print("PMCF parquet not found — run fetch_pmcf.py first. Skipping.")
        return
    print(f"Loading {os.path.basename(PMCF_PARQUET)}…")
    df = pd.read_parquet(PMCF_PARQUET)
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "pmcf_reference")


def build_stat_3pd_forecast_df() -> pd.DataFrame:
    """Pivot sf_9lc_grain.parquet into wide format.

    Grain: Material Number × Country Name × Sub-Segments
    Columns: SF / 3PD / SrcFC for Jan 2026–Dec 2027 (24 months each = 72 forecast cols).
    """
    sys.path.insert(0, DIR)
    from upload_sply_analysis import GRAIN_9LC_PARQUET, MONTHS_SF, SF_JOIN_KEYS

    grain = pd.read_parquet(GRAIN_9LC_PARQUET)
    for c in ["Statistical Forecast", "3PD Forecast", "Source Forecast"]:
        grain[c] = pd.to_numeric(grain[c], errors="coerce").fillna(0.0)

    def _pivot(col, prefix, months):
        p = grain[grain["Month Year"].isin(months)].pivot_table(
            index=SF_JOIN_KEYS, columns="Month Year",
            values=col, aggfunc="sum", fill_value=0,
        ).reset_index()
        p.columns.name = None
        for m in months:
            if m not in p.columns:
                p[m] = 0.0
        return p.rename(columns={m: f"{prefix} {m}" for m in months})

    sf  = _pivot("Statistical Forecast", "SF",    MONTHS_SF)
    pd3 = _pivot("3PD Forecast",         "3PD",   MONTHS_SF)
    src = _pivot("Source Forecast",       "SrcFC", MONTHS_SF)

    result = (sf.merge(pd3, on=SF_JOIN_KEYS, how="outer")
                .merge(src, on=SF_JOIN_KEYS, how="outer"))
    for c in [c for c in result.columns if c not in SF_JOIN_KEYS]:
        result[c] = pd.to_numeric(result[c], errors="coerce").fillna(0.0)

    # Explicit column order: dims → SF Jan–Dec 2026/2027 → 3PD → SrcFC
    ordered_cols = (SF_JOIN_KEYS
                    + [f"SF {m}"    for m in MONTHS_SF]
                    + [f"3PD {m}"   for m in MONTHS_SF]
                    + [f"SrcFC {m}" for m in MONTHS_SF])
    return result[[c for c in ordered_cols if c in result.columns]]


def load_stat_3pd_forecast(client: bigquery.Client):
    print("Building stat_3pd_forecast pivot (Material × Country × Sub-Segments)…")
    df = build_stat_3pd_forecast_df()
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "stat_3pd_forecast")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", choices=["sply", "adjfc", "oh", "customer", "pmcf", "stat", "all"], default="all")
    args = parser.parse_args()

    print(f"Authenticating with BigQuery ({GCP_PROJECT})…")
    client = _client()
    print(f"  ✓ Connected — dataset: {DATASET}\n")

    if args.table in ("sply", "all"):
        load_sply(client)
        print()
    if args.table in ("adjfc", "all"):
        load_adjfc(client)
        print()
    if args.table in ("oh", "all"):
        load_order_history(client)
        print()
    if args.table in ("customer", "all"):
        load_customer_analysis(client)
        print()
    if args.table in ("pmcf", "all"):
        load_pmcf(client)
        print()
    if args.table in ("stat", "all"):
        load_stat_3pd_forecast(client)
        print()

    print("✓ All done.")
    print(f"\nLooker Studio: Add data source → BigQuery → {GCP_PROJECT} → {DATASET}")


if __name__ == "__main__":
    main()
