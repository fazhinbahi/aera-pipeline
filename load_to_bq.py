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
import datetime
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
CONS_PARQUET  = os.path.join(DIR, "consensus_snapshots.parquet")   # fetch_consensus_snapshots.py
CONS_CALENDAR = os.path.join(DIR, "consensus_calendar.csv")        # consensus_cycle.py
UPC_SEED_CSV  = os.path.join(DIR, "material_upc_seed.csv")         # material → UPC from Enrique's datos_reducidos SKU rows

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
    # Local dev fallback: gcloud CLI (absolute-path fallback for launchd's bare PATH)
    token = None
    for gcloud in ("gcloud", "/usr/local/share/google-cloud-sdk/bin/gcloud"):
        try:
            token = subprocess.check_output(
                [gcloud, "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    if not token:
        raise SystemExit("✗ gcloud not found/authed. Set GCP_SA_JSON env var, "
                         "or run: gcloud auth login")
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


def _upload(client: bigquery.Client, df: pd.DataFrame, table_name: str,
            rename_map: dict | None = None):
    table_ref = f"{GCP_PROJECT}.{DATASET}.{table_name}"
    print(f"  Uploading {len(df):,} rows × {df.shape[1]} cols → {table_ref}…")

    df = _clean_df(df)
    df.columns = [_bq_col(c) for c in df.columns]

    # Apply semantic renames after BQ sanitisation
    if rename_map:
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

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


def _customer_analysis_rename_map() -> dict:
    """Build semantic rename map for customer_analysis (applied after _bq_col sanitisation).

    Adds Actual_ prefix to historical months, AdjFC_ to open forecast months,
    YoY_Dev_ to deviation % columns, and clarifies annual totals.
    """
    sys.path.insert(0, DIR)
    from upload_sply_analysis import MONTHS_2026_ACT, MONTHS_2026_FC

    months_all = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    rename: dict = {}

    # 2024 and 2025 — all historical actuals
    for yr in ("2024", "2025"):
        for m in months_all:
            rename[f"{m}_{yr}"] = f"Actual_{m}_{yr}"

    # 2026 — dynamic: closed months = Actual, open months = AdjFC
    for m_yr in MONTHS_2026_ACT:          # e.g. "Jan 2026"
        col = m_yr.replace(" ", "_")       # "Jan_2026"
        rename[col] = f"Actual_{col}"

    for m_yr in MONTHS_2026_FC:
        col = m_yr.replace(" ", "_")
        rename[col] = f"AdjFC_{col}"

    # 2027 — all AdjFC
    for m in months_all:
        rename[f"{m}_2027"] = f"AdjFC_{m}_2027"

    # Annual totals
    rename.update({
        "col_2024_Total": "Actual_Total_2024",
        "col_2025_Total": "Actual_Total_2025",
        "col_2026_Total": "Total_2026",
        "col_2027_Total": "AdjFC_Total_2027",
    })

    # Dev% per open month (AdjFC vs same month prior year)
    for m_yr in MONTHS_2026_FC:
        m = m_yr.split(" ")[0]             # "Jun"
        rename[f"Dev_{m}_2026"] = f"YoY_Dev_{m}_2026"

    # Dev% per quarter
    rename.update({
        "Q1_Dev": "YoY_Dev_Q1",
        "Q2_Dev": "YoY_Dev_Q2",
        "Q3_Dev": "YoY_Dev_Q3",
        "Q4_Dev": "YoY_Dev_Q4",
    })

    # Budget — full 2026 year
    months_all = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    for m in months_all:
        rename[f"Budget_{m}_2026"] = f"Budget_{m}_2026"  # keep as-is, already clear
    rename["Budget_2026_Total"] = "Budget_Total_2026"

    # YTD clarity
    rename["YTD_SPLY"] = "YTD_YoY_Pct"

    return rename


def _lag1_rename_map() -> dict:
    """Carry pre-2026-10 parquets over to the Fcst{k}M_ naming.

    build_lag1_table now writes Fcst{k}M_{Mon}_{YYYY} directly and covers lags
    1-6 across a rolling 13-month window, so this matches nothing on a current
    parquet. It stays only so an older artifact still loads correctly; the years
    are the ones that naming ever applied to.
    """
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return {f"Lag{lag}_{m}_{yr}": f"Fcst{lag}M_{m}_{yr}"
            for yr in ("2025", "2026") for m in months for lag in (1, 3, 4)}


def _stat3pd_rename_map() -> dict:
    """Rename col_3PD_ to ThreePD_ — removes confusing col_ prefix."""
    months_all = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    rename: dict = {}
    for yr in ("2026", "2027"):
        for m in months_all:
            rename[f"col_3PD_{m}_{yr}"] = f"ThreePD_{m}_{yr}"
    return rename


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
    _upload(client, df, "customer_analysis", rename_map=_customer_analysis_rename_map())


def load_adjfc(client: bigquery.Client):
    print(f"Loading {os.path.basename(FC_PARQUET)}…")
    df = pd.read_parquet(FC_PARQUET)
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "adjfc_raw")


def load_cycle_snapshot(client: bigquery.Client):
    """Keep one copy of adjfc_raw per calendar month in adjfc_cycle_snapshots.

    The DR deck's "M-1 Consensus Fcst" row needs the Adjusted Forecast as it
    stood before the current cycle's edits. adjfc_raw is overwritten daily and
    PMCF is only yesterday's copy, so the first pipeline run of each month
    appends a snapshot here (snapshot_month = first day of the month); later
    runs in the same month leave it untouched.
    """
    snap = f"{GCP_PROJECT}.{DATASET}.adjfc_cycle_snapshots"
    src  = f"{GCP_PROJECT}.{DATASET}.adjfc_raw"
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{snap}` AS
        SELECT DATE_TRUNC(CURRENT_DATE(), MONTH) AS snapshot_month, * FROM `{src}` WHERE FALSE
    """).result()
    n = list(client.query(f"""
        SELECT COUNT(*) AS n FROM `{snap}`
        WHERE snapshot_month = DATE_TRUNC(CURRENT_DATE(), MONTH)
    """).result())[0].n
    if n:
        print(f"  Cycle snapshot for this month already stored ({n:,} rows) — skipped")
        return
    # A load job, not INSERT ... SELECT: DML is refused on this project's free tier.
    import datetime as _dt
    df = pd.read_parquet(FC_PARQUET)
    df.insert(0, "snapshot_month", _dt.date.today().replace(day=1))
    df = _clean_df(df)
    df.columns = [_bq_col(c) for c in df.columns]
    job = client.load_table_from_dataframe(
        df, snap, job_config=bigquery.LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_APPEND))
    job.result()
    n = list(client.query(f"SELECT COUNT(*) AS n FROM `{snap}` WHERE snapshot_month = DATE_TRUNC(CURRENT_DATE(), MONTH)").result())[0].n
    print(f"  ✓ Cycle snapshot stored: {n:,} rows → adjfc_cycle_snapshots")


def load_fa_screen(client: bigquery.Client):
    """Forecast Adjustments screen rows (fetch_fa_screen.py) → fa_screen_rows.

    The DR deck's demand-plan table, MoM bridge and month cycle read this
    table, so they show exactly what the screen shows per market.
    """
    path = os.path.join(DIR, "fa_screen_rows.parquet")
    if not os.path.exists(path):
        print("  fa_screen_rows.parquet not found — skipped")
        return
    df = pd.read_parquet(path)
    df["fetch_date"] = pd.to_datetime(df["fetch_date"]).dt.date
    print(f"Loading {os.path.basename(path)}… {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "fa_screen_rows")


def _upload_frame(client: bigquery.Client, df: pd.DataFrame, table_name: str):
    """Like _upload but without _clean_df: keeps code columns (material, UPC) as strings.
    Date columns must already be python date objects (autodetect maps them to DATE)."""
    table_ref = f"{GCP_PROJECT}.{DATASET}.{table_name}"
    df = df.copy()
    df.columns = [_bq_col(c) for c in df.columns]
    print(f"  Uploading {len(df):,} rows × {df.shape[1]} cols → {table_ref}…")
    job = client.load_table_from_dataframe(
        df, table_ref,
        job_config=bigquery.LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE, autodetect=True))
    job.result()
    print(f"  ✓ {client.get_table(table_ref).num_rows:,} rows in {table_ref}")


def build_material_upc_map() -> pd.DataFrame:
    """material → UPC: Aera order history first, Enrique's SKU seed for materials it lacks."""
    parts = []
    if os.path.exists(OH_PARQUET):
        oh = pd.read_parquet(OH_PARQUET, columns=["Material Number", "UPC Code"]).dropna()
        oh = oh.rename(columns={"Material Number": "material", "UPC Code": "upc"})
        oh = oh[(oh["material"] != "") & ~oh["upc"].isin(["", "Not Set"])].drop_duplicates()
        oh["source"] = "order_history"
        parts.append(oh)
    if os.path.exists(UPC_SEED_CSV):
        seed = pd.read_csv(UPC_SEED_CSV, dtype=str)[["material", "upc"]].drop_duplicates()
        seed["source"] = "datos_reducidos_sku"
        parts.append(seed)
    m = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["material", "upc", "source"])
    m["material"] = m["material"].astype(str).str.strip()
    m["upc"] = m["upc"].astype(str).str.strip()
    # one UPC per material: order history wins, then the seed
    m["rank"] = m["source"].map({"order_history": 0, "datos_reducidos_sku": 1})
    m = m.sort_values(["material", "rank"]).drop_duplicates("material", keep="first").drop(columns="rank")
    return m.reset_index(drop=True)


# Materials without a UPC get the same synthetic key on both sides (forecast and orders), the way
# Enrique's "own_upc" rows do ("Not Set · KRAKEN RTD COLA · 0.375 L"), so they still pair up.
_NOT_SET_UPC = "CONCAT('Not Set · ', UPPER({brand}), ' · ', FORMAT('%g', {vol}), ' L')"

CONSENSUS_VIEWS = {
    # Enrique's FC rows: every snapshot row with its cycle, lag and UPC
    "consensus_fc_lag": """
        SELECT s.approval_date, c.snapshot_date, c.cycle_month, c.role, c.group_name,
               DATE_DIFF(s.forecast_month, c.cycle_month, MONTH) AS lag,
               s.forecast_month, s.sub_segment, s.customer_country, s.material,
               COALESCE(m.upc, """ + _NOT_SET_UPC.format(brand="s.sub_brand", vol="s.volume_l") + """) AS upc,
               s.sub_brand, s.category, s.volume_l, s.ext_material_group, s.material_desc,
               s.source_fc, s.stat_fc, s.consensus_fc, s.approved_delta, s.consensus_phy_cases,
               s.actual_order_qty, s.n_customers, s.fetch_date
        FROM (SELECT * FROM `{ds}.consensus_snapshots`
              QUALIFY fetch_date = MAX(fetch_date) OVER (PARTITION BY approval_date)) s
        JOIN `{ds}.consensus_cycle_calendar` c
          ON c.approval_date = s.approval_date
         AND c.group_name = CASE WHEN s.sub_segment LIKE '%GTR%' THEN 'GTR'
                                 WHEN s.sub_segment LIKE '%IMC%' THEN 'IMC' ELSE '3PD' END
        LEFT JOIN `{ds}.material_upc_map` m ON m.material = s.material
    """,
    # Enrique's ACT rows: sales orders (9LC) at group x sub-segment x country x UPC x month
    "consensus_actuals": """
        SELECT CASE WHEN Sub_Segments LIKE '%GTR%' THEN 'GTR'
                    WHEN Sub_Segments LIKE '%IMC%' THEN 'IMC' ELSE '3PD' END AS group_name,
               Sub_Segments AS sub_segment, Country_Name AS customer_country,
               CASE WHEN UPC_Code IS NULL OR UPC_Code IN ('', 'Not Set')
                    THEN """ + _NOT_SET_UPC.format(brand="Sub_Brand_Description", vol="Volume") + """
                    ELSE UPC_Code END AS upc,
               PARSE_DATE('%b %Y', Month_Year) AS month, SUM(Order_Qty_9LC) AS actual_9lc
        FROM `{ds}.order_history_raw`
        WHERE Month_Year IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5
    """,
    # forecast vs actual per lag (0..5), Primary approvals, UPC grain — the base of WMAPE / BIAS by lag:
    #   WMAPE = SUM(ABS(consensus_fc - actual_9lc)) / SUM(actual_9lc), BIAS = (SUM(consensus_fc) - SUM(actual_9lc)) / SUM(actual_9lc)
    # The official grain (UPC x group) sums countries and sub-segments before the ABS; do that in the query.
    "consensus_accuracy_lag": """
        WITH f AS (
          SELECT group_name, sub_segment, customer_country, upc, forecast_month AS month, lag, approval_date,
                 SUM(consensus_fc) AS consensus_fc, SUM(stat_fc) AS stat_fc, SUM(source_fc) AS source_fc
          FROM `{ds}.consensus_fc_lag`
          WHERE role = 'Primary' AND lag BETWEEN 0 AND 5
          GROUP BY 1, 2, 3, 4, 5, 6, 7
        ),
        avail AS (SELECT DISTINCT group_name, month, lag, approval_date FROM f),
        a AS (
          SELECT a.group_name, a.sub_segment, a.customer_country, a.upc, a.month, v.lag, v.approval_date, a.actual_9lc
          FROM `{ds}.consensus_actuals` a
          JOIN avail v ON v.group_name = a.group_name AND v.month = a.month
        )
        SELECT COALESCE(f.group_name, a.group_name) AS group_name,
               COALESCE(f.sub_segment, a.sub_segment) AS sub_segment,
               COALESCE(f.customer_country, a.customer_country) AS customer_country,
               COALESCE(f.upc, a.upc) AS upc,
               COALESCE(f.month, a.month) AS month,
               COALESCE(f.lag, a.lag) AS lag,
               COALESCE(f.approval_date, a.approval_date) AS approval_date,
               IFNULL(f.consensus_fc, 0) AS consensus_fc, IFNULL(f.stat_fc, 0) AS stat_fc,
               IFNULL(f.source_fc, 0) AS source_fc, IFNULL(a.actual_9lc, 0) AS actual_9lc
        FROM f
        FULL OUTER JOIN a
          ON a.group_name = f.group_name AND a.sub_segment = f.sub_segment
         AND a.customer_country = f.customer_country AND a.upc = f.upc
         AND a.month = f.month AND a.lag = f.lag
    """,
}


def load_consensus(client: bigquery.Client):
    """Consensus snapshots by approval date (fetch_consensus_snapshots.py) → consensus_snapshots,
    the cycle calendar → consensus_cycle_calendar, material → UPC map, and the lag / accuracy views."""
    if not os.path.exists(CONS_PARQUET):
        print("  consensus_snapshots.parquet not found — run fetch_consensus_snapshots.py first. Skipping.")
        return
    df = pd.read_parquet(CONS_PARQUET)
    for c in ("approval_date", "forecast_month", "fetch_date"):
        df[c] = pd.to_datetime(df[c]).dt.date
    for c in ("sub_segment", "customer_country", "material", "ext_material_group", "sub_brand", "category", "material_desc"):
        df[c] = df[c].fillna("").astype(str)
    print(f"Loading {os.path.basename(CONS_PARQUET)}… {df.shape[0]:,} rows × {df.shape[1]} cols, "
          f"{df['approval_date'].nunique()} approvals")
    # A full local history replaces the table; a partial parquet (CI only holds the approvals it
    # fetched that day) is APPENDED — the views keep the latest fetch_date per approval.
    table_ref = f"{GCP_PROJECT}.{DATASET}.consensus_snapshots"
    try:
        in_bq = {r.approval_date for r in client.query(f"SELECT DISTINCT approval_date FROM `{table_ref}`").result()}
    except Exception:
        in_bq = set()
    have = set(df["approval_date"].unique())
    if in_bq <= have:
        _upload_frame(client, df, "consensus_snapshots")
    else:
        print(f"  parquet holds {len(have)} approval(s), BigQuery {len(in_bq)} → appending (no truncate)")
        job = client.load_table_from_dataframe(
            df, table_ref,
            job_config=bigquery.LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_APPEND, autodetect=True))
        job.result()
        print(f"  ✓ {client.get_table(table_ref).num_rows:,} rows in {table_ref}")

    cal = pd.read_csv(CONS_CALENDAR, dtype=str).fillna("")
    cal["approval_date"] = pd.to_datetime(cal["approval_date"]).dt.date
    cal["snapshot_date"] = pd.to_datetime(cal["snapshot_date"], errors="coerce").dt.date
    cal["cycle_month"] = pd.to_datetime(cal["cycle_month"] + "-01").dt.date
    _upload_frame(client, cal, "consensus_cycle_calendar")

    _upload_frame(client, build_material_upc_map(), "material_upc_map")

    ds = f"{GCP_PROJECT}.{DATASET}"
    for name, sql in CONSENSUS_VIEWS.items():
        client.query(f"CREATE OR REPLACE VIEW `{ds}.{name}` AS {sql.format(ds=ds)}").result()
        print(f"  ✓ view {name}")


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


def load_lag1(client: bigquery.Client):
    lag1_parquet = os.path.join(DIR, "lag1_vs_actuals.parquet")
    if not os.path.exists(lag1_parquet):
        print("lag1_vs_actuals.parquet not found — skipping.")
        return
    print(f"Loading {os.path.basename(lag1_parquet)}…")
    df = pd.read_parquet(lag1_parquet)
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "lag1_data", rename_map=_lag1_rename_map())


def load_stat_3pd_forecast(client: bigquery.Client):
    sf_parquet = os.path.join(DIR, "sf_9lc_grain.parquet")
    if not os.path.exists(sf_parquet):
        print("sf_9lc_grain.parquet not found — SF fetch likely failed. Skipping stat_3pd_forecast.")
        return
    print("Building stat_3pd_forecast pivot (Material × Country × Sub-Segments)…")
    df = build_stat_3pd_forecast_df()
    print(f"  {df.shape[0]:,} rows × {df.shape[1]} cols")
    _upload(client, df, "stat_3pd_forecast", rename_map=_stat3pd_rename_map())


EXPIRY_REFRESH_DAYS = 45     # recreate anything that would expire sooner than this


def refresh_expiry(client: bigquery.Client, min_days_left: int = EXPIRY_REFRESH_DAYS):
    """Keep every table and view alive in the BigQuery SANDBOX.

    The project has no billing, so the dataset carries the sandbox's fixed
    60-day default table expiration, which cannot be removed or extended
    (PATCH expires → 403 "must be less than 60 days while in sandbox mode").
    A WRITE_TRUNCATE load does NOT reset it (adjfc_raw created 19 Sep 2026
    was 9 days from expiry on 8 Nov despite daily loads), and on 7–8 Nov 2026
    lag1_data and cov_segmentation silently disappeared this way.
    Recreating a table in place (CREATE OR REPLACE TABLE t AS SELECT * FROM t)
    does reset the clock, and so does CREATE OR REPLACE VIEW; this runs at the
    end of every load for anything within `min_days_left` of expiring.
    """
    ds = f"{GCP_PROJECT}.{DATASET}"
    now = datetime.datetime.now(datetime.timezone.utc)
    refreshed, failed = [], []
    for t in client.list_tables(ds):
        tb = client.get_table(f"{ds}.{t.table_id}")
        if tb.expires is None or (tb.expires - now).days >= min_days_left:
            continue
        ref = f"`{ds}.{t.table_id}`"
        try:
            if tb.table_type == "VIEW":
                client.query(f"CREATE OR REPLACE VIEW {ref} AS {tb.view_query}").result()
            else:
                client.query(f"CREATE OR REPLACE TABLE {ref} AS SELECT * FROM {ref}").result()
            new = client.get_table(f"{ds}.{t.table_id}")
            refreshed.append(f"{t.table_id} ({(tb.expires - now).days}d → {(new.expires - now).days}d)")
        except Exception as e:
            failed.append(f"{t.table_id}: {str(e)[:120]}")
    if refreshed:
        print("  ✓ expiry refreshed: " + ", ".join(refreshed))
    else:
        print(f"  expiry: nothing within {min_days_left} days of expiring")
    for f in failed:
        print(f"  ⚠ expiry refresh failed — {f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", choices=["sply", "adjfc", "oh", "customer", "pmcf", "lag1", "stat", "fa", "consensus", "expiry", "all"], default="all")
    args = parser.parse_args()

    print(f"Authenticating with BigQuery ({GCP_PROJECT})…")
    client = _client()
    print(f"  ✓ Connected — dataset: {DATASET}\n")

    if args.table in ("sply", "all"):
        load_sply(client)
        print()
    if args.table in ("adjfc", "all"):
        load_adjfc(client)
        try:
            load_cycle_snapshot(client)
        except Exception as e:
            print(f"  ⚠ Cycle snapshot skipped: {e}")
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
    if args.table in ("lag1", "all"):
        load_lag1(client)
        print()
    if args.table in ("stat", "all"):
        load_stat_3pd_forecast(client)
        print()
    if args.table in ("fa", "all"):
        load_fa_screen(client)
        print()
    if args.table in ("consensus", "all"):
        try:
            load_consensus(client)
        except Exception as e:
            print(f"  ⚠ consensus load skipped: {e}")
        print()
    # always, whatever was loaded: the sandbox deletes tables 60 days after creation
    try:
        refresh_expiry(client)
    except Exception as e:
        print(f"  ⚠ expiry refresh skipped: {e}")

    print("✓ All done.")
    print(f"\nLooker Studio: Add data source → BigQuery → {GCP_PROJECT} → {DATASET}")


if __name__ == "__main__":
    main()
