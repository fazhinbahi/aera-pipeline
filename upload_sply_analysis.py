"""
Rebuild the Consolidated tab in the Aera SPLY Compare — Analysis Google Sheet.

Sources:
  order_history_emea_apac.parquet  — Order Qty 9LC by material/country/month
  adjfc_nz.parquet                  — Adj FC + Actuals by material/country/month

Target sheet: 1FIVHx69ZeqKyGGL9csEmMfSWcbDN-h0VjCNZnw3OMzg
Target tab  : Consolidated

Clears rows 4+ (preserves header rows 1–3) and rewrites data.

Column layout (56 cols, A–BD):
  A–M   : Dimensions (13 cols, incl. Customer)
  N–Y   : Jan'25–Dec'25 actuals (12 cols)
  Z     : 2025 Total
  AA–AD : Jan'26–Apr'26 actuals (4 cols)
  AE–AL : May'26–Dec'26 FC (8 cols)
  AM    : 2026 Total
  AN    : YTD 2025 (Jan–Apr)
  AO    : YTD 2026 (Jan–Apr)
  AP    : YTD SPLY%
  AQ    : FC vs SPLY%
  AR–AY : Dev% May'26–Dec'26 (8 cols)
  AZ–BC : Q1–Q4 Dev% (4 cols)
  BD    : FC vs Last 6M Avg

Usage:
  python3 upload_sply_analysis.py
"""

import datetime
import json
import os
import subprocess
import time

import pandas as pd
import gspread
import google.oauth2.credentials

DIR             = os.path.dirname(os.path.abspath(__file__))
OH_PARQUET      = os.path.join(DIR, "order_history_emea_apac.parquet")
FC_PARQUET      = os.path.join(DIR, "adjfc_nz.parquet")
FC_PREV_PARQUET  = os.path.join(DIR, "adjfc_nz_prev_month.parquet")
PMCF_PARQUET     = os.path.join(DIR, "pmcf_monthly.parquet")
MAPE_PARQUET     = os.path.join(DIR, "forecast_accuracy_grain.parquet")

SHEET_ID   = "1FIVHx69ZeqKyGGL9csEmMfSWcbDN-h0VjCNZnw3OMzg"
TAB_NAME   = "Consolidated"
GCLOUD_ACC = "jfaizan07@gmail.com"
CHUNK      = 50_000

MONTHS_2024     = [f"{m} 2024" for m in
                   ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]]
MONTHS_2025     = [f"{m} 2025" for m in
                   ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]]

# ── Dynamic actuals / forecast cutoff ─────────────────────────────────────────
# Actuals = all 2026 months that have FULLY completed (i.e. before the current month).
# Source: order_history Order Qty 9LC (matches Aera "Actual Sales Orders" within <0.5%
#         for confirmed months; residual gap = open/pending orders not yet in the BI object).
_ALL_2026_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_TODAY           = datetime.date.today()
_CUR_MONTH_START = _TODAY.replace(day=1)

MONTHS_2026_ACT = [
    f"{m} 2026" for m in _ALL_2026_MONTHS
    if datetime.datetime.strptime(f"{m} 2026", "%b %Y").date() < _CUR_MONTH_START
]
MONTHS_2026_FC  = [f"{m} 2026" for m in _ALL_2026_MONTHS if f"{m} 2026" not in set(MONTHS_2026_ACT)]
MONTHS_2026_SO  = [f"SO - {m}" for m in MONTHS_2026_FC]  # open orders for forecast months

# YTD mirrors the same completed-month count across both years
YTD_2025 = [m.replace("2026", "2025") for m in MONTHS_2026_ACT]
YTD_2026 = MONTHS_2026_ACT[:]

# Previous Month Consensus Forecast columns (last month's Adjusted FC, all 12 months of 2026)
MONTHS_2026_PMCF = [f"PMCF {m} 2026" for m in _ALL_2026_MONTHS]

Q1_2025 = [f"{m} 2025" for m in ["Jan","Feb","Mar"]]
Q2_2025 = [f"{m} 2025" for m in ["Apr","May","Jun"]]
Q3_2025 = [f"{m} 2025" for m in ["Jul","Aug","Sep"]]
Q4_2025 = [f"{m} 2025" for m in ["Oct","Nov","Dec"]]
Q1_2026 = [f"{m} 2026" for m in ["Jan","Feb","Mar"]]
Q2_2026 = [f"{m} 2026" for m in ["Apr","May","Jun"]]
Q3_2026 = [f"{m} 2026" for m in ["Jul","Aug","Sep"]]
Q4_2026 = [f"{m} 2026" for m in ["Oct","Nov","Dec"]]

# 2027 forecast months (full year from AdjFC)
MONTHS_2027 = [f"{m} 2027" for m in _ALL_2026_MONTHS]
Q1_2027 = [f"{m} 2027" for m in ["Jan","Feb","Mar"]]
Q2_2027 = [f"{m} 2027" for m in ["Apr","May","Jun"]]
Q3_2027 = [f"{m} 2027" for m in ["Jul","Aug","Sep"]]
Q4_2027 = [f"{m} 2027" for m in ["Oct","Nov","Dec"]]

# Trailing 6 months of actuals ending at the last completed month (for FC vs Last 6M Avg)
_hist_months = [f"{m} 2025" for m in _ALL_2026_MONTHS] + MONTHS_2026_ACT
LAST_6M_COLS = _hist_months[-6:]

# Forecast Accuracy MAPE columns (Jun 2025–May 2026, from Aera Accuracy SKU page, Lag 1)
MAPE_MONTHS = [
    "Jun 2025", "Jul 2025", "Aug 2025", "Sep 2025",
    "Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026",
    "Feb 2026", "Mar 2026", "Apr 2026", "May 2026",
]
MAPE_COLS = [f"MAPE {m}" for m in MAPE_MONTHS] + ["MAPE Total", "Grain Count"]

# Stat Forecast / 3PD / Source Forecast columns
MONTHS_SF_2026 = [f"{m} 2026" for m in _ALL_2026_MONTHS]
MONTHS_SF_2027 = [f"{m} 2027" for m in _ALL_2026_MONTHS]
MONTHS_SF   = MONTHS_SF_2026 + MONTHS_SF_2027
SF_COLS     = [f"SF {m}" for m in MONTHS_SF]
FCST3PD_COLS = [f"3PD {m}" for m in MONTHS_SF]    # 3PD Forecast (9LC, 2026+2027)
SRCFC_COLS   = [f"SrcFC {m}" for m in MONTHS_SF]   # Source Forecast (9LC, 2026+2027)
SF_JOIN_KEYS = ["Material Number", "Country Name", "Sub-Segments"]
SF_PARQUET   = os.path.join(DIR, "forecast_3yr_full.parquet")
GRAIN_9LC_PARQUET = os.path.join(DIR, "sf_9lc_grain.parquet")

# Aera 9LC SF monthly totals (no filter, extracted from FA page source 24 Jun 2026).
# Used as scaling targets: raw parquet SF values are in product units (~16-18x 9LC).
# Applying monthly scale = target_9lc / raw_sum brings values into correct 9LC units.
SF_9LC_TARGETS = {
    "Jan 2026": 269366, "Feb 2026": 277036, "Mar 2026": 271373, "Apr 2026": 268160,
    "May 2026": 333644, "Jun 2026": 346943, "Jul 2026": 317495, "Aug 2026": 362516,
    "Sep 2026": 356878, "Oct 2026": 381836, "Nov 2026": 385971, "Dec 2026": 444197,
    "Jan 2027": 284246, "Feb 2027": 229867, "Mar 2027": 271992, "Apr 2027": 267046,
    "May 2027": 273583, "Jun 2027": 339132, "Jul 2027": 299945, "Aug 2027": 327817,
    "Sep 2027": 331802, "Oct 2027": 346806, "Nov 2027": 355210, "Dec 2027": 423926,
}

DIM_COLS = [
    "Customer Number",
    "Customer Name",
    "Material Number",
    "Country Name",
    "Sub-Segments",
    "Material Long Description",
    "UPC Code",
    "Brand Family",
    "Sub-Brand Description",
    "Volume",
    "Sales Organisation",
    "Region",
    "Business Segment",
    "Category Grouper Description (Z)",
]

JOIN_KEYS    = ["Material Number", "Country Name", "Sub-Segments", "Customer Number"]
OH_DIM_EXTRA = [c for c in DIM_COLS if c not in JOIN_KEYS and c != "Customer Name"]

# ── Customer analysis ─────────────────────────────────────────────────────────
CUSTOMER_JOIN_KEYS = JOIN_KEYS
CUSTOMER_DIM_COLS  = DIM_COLS

FC_DIM_MAP = {
    "Organisation":                       "Sales Organisation",
    "Region":                             "Region",
    "Business Segment":                   "Business Segment",
    "Category Grouper Description (Z)":   "Category Grouper Description (Z)",
    "Sub-Brand Long Description":         "Sub-Brand Description",
    "Volume":                             "Volume",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _gc() -> gspread.Client:
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        raise SystemExit("\n✗ Run: gcloud auth login --enable-gdrive-access\nThen re-run.")
    creds = google.oauth2.credentials.Credentials(token=token)
    return gspread.authorize(creds)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mode(s: pd.Series) -> str:
    vals = s.dropna().astype(str)
    vals = vals[~vals.str.strip().isin({"Not Set", "nan", ""})]
    return vals.value_counts().index[0] if not vals.empty else ""


def _safe_pct(num: float, denom: float) -> object:
    """Signed % change: (num - denom) / |denom| × 100. Returns '' when denom is 0."""
    if denom == 0:
        return ""
    return round((num - denom) / abs(denom) * 100, 2)


def _dev_pct(py: float, fc: float) -> object:
    """Absolute deviation between FC and prior year, matching the dashboard formula."""
    if py == 0 and fc == 0:
        return ""
    if fc != 0:
        return round(abs((fc - py) / fc * 100), 2)
    return round(abs((py - fc) / py * 100), 2)


# ── Build pivots ──────────────────────────────────────────────────────────────

def build_pmcf_pivot(keys: list) -> pd.DataFrame:
    """Build a grain-level PMCF pivot from the previous month's AdjFC backup.

    Each row in the result has the same keys as the main table plus PMCF month
    columns (one per 2026 month) sourced from last month's Adjusted FC.
    Falls back to zeros if the backup parquet doesn't exist.
    """
    if not os.path.exists(FC_PREV_PARQUET):
        return pd.DataFrame(columns=keys + MONTHS_2026_PMCF)

    prev = pd.read_parquet(FC_PREV_PARQUET)
    if "Adj FC 9LC" in prev.columns:
        prev["Adjusted FC"] = prev["Adj FC 9LC"]
    prev["Adjusted FC"] = pd.to_numeric(prev["Adjusted FC"], errors="coerce").fillna(0)

    # Align column names to match main table keys
    if "Customer Number" not in prev.columns and "Customer Sold-to Number" in prev.columns:
        prev = prev.rename(columns={"Customer Sold-to Number": "Customer Number"})

    all_2026 = [f"{m} 2026" for m in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]]
    prev = prev[prev["Month Year"].isin(set(all_2026))]

    valid_keys = [k for k in keys if k in prev.columns]
    if not valid_keys:
        return pd.DataFrame(columns=keys + MONTHS_2026_PMCF)

    agg = (
        prev.groupby(valid_keys + ["Month Year"], dropna=False)
        ["Adjusted FC"].sum().reset_index()
    )
    piv = agg.pivot_table(
        index=valid_keys,
        columns="Month Year",
        values="Adjusted FC",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    piv.columns.name = None

    rename_map = {f"{m} 2026": f"PMCF {m} 2026"
                  for m in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]}
    piv = piv.rename(columns=rename_map)

    for col in MONTHS_2026_PMCF:
        if col not in piv.columns:
            piv[col] = 0.0

    return piv


def build_sf_pivot() -> pd.DataFrame:
    """Stat Forecast / 3PD / Source Forecast pivot at Material × Country × Sub-Segments.

    Reads sf_9lc_grain.parquet which contains exact 9LC values for Jan 2026–Dec 2027
    (validated against Aera Forecast Adjustments page totals per country).
    Returns SF, 3PD Forecast, and Source Forecast columns for all 24 months.

    Joins to customer_analysis on SF_JOIN_KEYS (Material × Country × Sub-Segments).
    """
    if not os.path.exists(GRAIN_9LC_PARQUET):
        print("  ⚠ sf_9lc_grain.parquet not found — SF/3PD/SrcFC zeroed")
        return pd.DataFrame(columns=SF_JOIN_KEYS + SF_COLS + FCST3PD_COLS + SRCFC_COLS)

    grain = pd.read_parquet(GRAIN_9LC_PARQUET)
    for c in ["Statistical Forecast", "3PD Forecast", "Source Forecast"]:
        grain[c] = pd.to_numeric(grain[c], errors="coerce").fillna(0.0)

    def _pivot(col: str, prefix: str, months: list) -> pd.DataFrame:
        p = grain[grain["Month Year"].isin(months)].pivot_table(
            index=SF_JOIN_KEYS, columns="Month Year",
            values=col, aggfunc="sum", fill_value=0,
        ).reset_index()
        p.columns.name = None
        for m in months:
            if m not in p.columns:
                p[m] = 0.0
        return p.rename(columns={m: f"{prefix} {m}" for m in months})

    sf_all  = _pivot("Statistical Forecast", "SF",    MONTHS_SF)
    pd3_all = _pivot("3PD Forecast",         "3PD",   MONTHS_SF)
    src_all = _pivot("Source Forecast",       "SrcFC", MONTHS_SF)

    result = (sf_all
              .merge(pd3_all, on=SF_JOIN_KEYS, how="outer")
              .merge(src_all, on=SF_JOIN_KEYS, how="outer"))

    # Spot-check totals
    for m, target in SF_9LC_TARGETS.items():
        col = f"SF {m}"
        s = result[col].sum() if col in result.columns else 0
        print(f"  SF {m}: {s:,.0f}  (target {target:,})")

    return result


def build_order_pivot(oh: pd.DataFrame) -> pd.DataFrame:
    oh = oh.copy()
    if "Customer Sold-to Number" in oh.columns:
        oh = oh.rename(columns={"Customer Sold-to Number": "Customer Number"})
    oh["Order Qty 9LC"] = pd.to_numeric(oh["Order Qty 9LC"], errors="coerce").fillna(0)
    oh = oh[pd.to_datetime(oh["Month Year"], format="%b %Y", errors="coerce").notna()]

    # Raw month names for May-Dec 2026 (before SO- prefix rename)
    _so_raw = [m.replace("SO - ", "") for m in MONTHS_2026_SO]

    keep = set(MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + _so_raw)
    oh   = oh[oh["Month Year"].isin(keep)]

    qty = (
        oh.groupby(JOIN_KEYS + ["Month Year"], dropna=False)
        ["Order Qty 9LC"].sum().reset_index()
    )
    piv = qty.pivot_table(
        index=JOIN_KEYS,
        columns="Month Year",
        values="Order Qty 9LC",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    piv.columns.name = None

    for m in MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + _so_raw:
        if m not in piv.columns:
            piv[m] = 0.0

    # Rename May-Dec 2026 columns to SO - prefix
    piv = piv.rename(columns={raw: so for raw, so in zip(_so_raw, MONTHS_2026_SO)})

    dim_avail = [c for c in OH_DIM_EXTRA if c in oh.columns]
    if dim_avail:
        dim_lkp = (
            oh.groupby(JOIN_KEYS, dropna=False)[dim_avail]
            .agg(_mode).reset_index()
        )
        piv = piv.merge(dim_lkp, on=JOIN_KEYS, how="left")

    return piv


def build_adjfc_pivot(fc: pd.DataFrame) -> pd.DataFrame:
    fc = fc.copy()
    fc["Adjusted FC"] = pd.to_numeric(fc["Adjusted FC"], errors="coerce").fillna(0)
    fc = fc[fc["Month Year"].isin(set(MONTHS_2026_FC) | set(MONTHS_2027))]

    agg = (
        fc.groupby(JOIN_KEYS + ["Month Year"], dropna=False)
        ["Adjusted FC"].sum().reset_index()
    )
    piv = agg.pivot_table(
        index=JOIN_KEYS,
        columns="Month Year",
        values="Adjusted FC",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    piv.columns.name = None

    for m in MONTHS_2026_FC + MONTHS_2027:
        if m not in piv.columns:
            piv[m] = 0.0

    fc_dim_avail = {k: v for k, v in FC_DIM_MAP.items() if k in fc.columns}
    if fc_dim_avail:
        fc_dim = (
            fc.groupby(JOIN_KEYS, dropna=False)[list(fc_dim_avail)]
            .agg(_mode).reset_index()
            .rename(columns=fc_dim_avail)
        )
        piv = piv.merge(fc_dim, on=JOIN_KEYS, how="left")

    return piv


# ── Join + compute metrics ────────────────────────────────────────────────────

def build_final(oh: pd.DataFrame, fc: pd.DataFrame) -> pd.DataFrame:
    print("Building global customer name lookup...")
    name_lookup = build_global_customer_name_lookup(oh, fc)
    print(f"  {len(name_lookup):,} named customers")

    _act_range = f"Jan–{MONTHS_2026_ACT[-1].split()[0]} 2026" if MONTHS_2026_ACT else "none"
    print(f"Building Order History pivot (2024 + 2025 + {_act_range})...")
    oh_piv = build_order_pivot(oh)
    print(f"  {oh_piv.shape[0]:,} rows")

    _fc_range = f"{MONTHS_2026_FC[0].split()[0]}–Dec 2026" if MONTHS_2026_FC else "none"
    print(f"Building AdjFC pivot ({_fc_range})...")
    fc_piv = build_adjfc_pivot(fc)
    print(f"  {fc_piv.shape[0]:,} rows")

    print("Building Previous Month Consensus Forecast pivot (grain level)...")
    pmcf_piv = build_pmcf_pivot(JOIN_KEYS)
    print(f"  {pmcf_piv.shape[0]:,} rows")

    print("Outer joining on Material × Country × Sub-Segments × Customer...")
    joined = oh_piv.merge(fc_piv, on=JOIN_KEYS, how="outer", suffixes=("", "_fc"))
    if not pmcf_piv.empty and len(pmcf_piv) > 1:
        joined = joined.merge(pmcf_piv, on=JOIN_KEYS, how="left")
    print(f"  {joined.shape[0]:,} rows after join")

    # Backfill dimension columns from AdjFC where Order History has no data
    for col in OH_DIM_EXTRA:
        fc_col = f"{col}_fc"
        if fc_col in joined.columns:
            joined[col] = joined[col].fillna(joined[fc_col])
            joined.drop(columns=[fc_col], inplace=True)

    # Ensure all month columns are numeric
    for m in MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + MONTHS_2026_FC + MONTHS_2027:
        if m not in joined.columns:
            joined[m] = 0.0
        joined[m] = pd.to_numeric(joined[m], errors="coerce").fillna(0.0)

    # Apply global customer name lookup
    joined["Customer Number"] = joined["Customer Number"].fillna("").astype(str).replace({"nan": "", "None": ""})
    joined["Customer Name"] = joined["Customer Number"].map(name_lookup).fillna("")

    # Clean dimension columns
    for col in DIM_COLS:
        if col not in joined.columns:
            joined[col] = ""
        else:
            joined[col] = (
                joined[col].fillna("").astype(str)
                .replace({"nan": "", "None": ""})
            )

    # ── Totals ────────────────────────────────────────────────────────────────
    joined["2024 Total"] = joined[MONTHS_2024].sum(axis=1)
    joined["2025 Total"] = joined[MONTHS_2025].sum(axis=1)
    joined["2026 Total"] = joined[MONTHS_2026_ACT + MONTHS_2026_FC].sum(axis=1)
    joined["2027 Total"] = joined[MONTHS_2027].sum(axis=1)

    # ── YTD (Jan–Apr of each year) ────────────────────────────────────────────
    joined["YTD 2025"] = joined[YTD_2025].sum(axis=1)
    joined["YTD 2026"] = joined[YTD_2026].sum(axis=1)
    joined["YTD SPLY%"]  = joined.apply(lambda r: _safe_pct(r["YTD 2026"],  r["YTD 2025"]),  axis=1)
    joined["FC vs SPLY%"] = joined.apply(lambda r: _safe_pct(r["2026 Total"], r["2025 Total"]), axis=1)

    # ── Dev% by month (May–Dec 2026) ──────────────────────────────────────────
    dev_month_cols = []
    for m in MONTHS_2026_FC:
        py_m  = m.replace("2026", "2025")
        dcol  = f"Dev% {m}"
        joined[dcol] = joined.apply(
            lambda r, fc_m=m, py=py_m: _dev_pct(r.get(py, 0), r[fc_m]), axis=1
        )
        dev_month_cols.append(dcol)

    # ── Dev% by quarter (Q1–Q4 2026 vs 2025) ─────────────────────────────────
    q_specs = [
        ("Q1 Dev%", Q1_2026, Q1_2025),
        ("Q2 Dev%", Q2_2026, Q2_2025),
        ("Q3 Dev%", Q3_2026, Q3_2025),
        ("Q4 Dev%", Q4_2026, Q4_2025),
    ]
    q_dev_cols = []
    for qcol, q26, q25 in q_specs:
        c26 = [c for c in q26 if c in joined.columns]
        c25 = [c for c in q25 if c in joined.columns]
        joined[qcol] = joined.apply(
            lambda r, cols26=c26, cols25=c25: _dev_pct(
                r[cols25].sum() if cols25 else 0.0,
                r[cols26].sum() if cols26 else 0.0,
            ), axis=1
        )
        q_dev_cols.append(qcol)

    # ── FC vs Last 6M Avg ─────────────────────────────────────────────────────
    # Avg monthly volume of Nov'25–Apr'26 × 8 months vs sum of May–Dec'26 FC
    valid_l6 = [c for c in LAST_6M_COLS if c in joined.columns]
    n_l6     = len(valid_l6)
    if n_l6 > 0:
        joined["FC vs Last 6M Avg"] = joined.apply(
            lambda r: _dev_pct(
                r[valid_l6].sum() / n_l6 * len(MONTHS_2026_FC),  # annualised-FC-period avg
                r[MONTHS_2026_FC].sum(),
            ), axis=1
        )
    else:
        joined["FC vs Last 6M Avg"] = ""

    # Ensure SO columns are numeric
    for col in MONTHS_2026_SO:
        if col not in joined.columns:
            joined[col] = 0.0
        joined[col] = pd.to_numeric(joined[col], errors="coerce").fillna(0.0)

    # Ensure PMCF columns are numeric (0 if not matched via the pivot)
    for col in MONTHS_2026_PMCF:
        if col not in joined.columns:
            joined[col] = 0.0
        joined[col] = pd.to_numeric(joined[col], errors="coerce").fillna(0.0)

    # ── Assemble final column order ───────────────────────────────────────────
    final_cols = (
        DIM_COLS
        + MONTHS_2024
        + ["2024 Total"]
        + MONTHS_2025
        + ["2025 Total"]
        + MONTHS_2026_ACT
        + MONTHS_2026_FC
        + MONTHS_2026_SO
        + MONTHS_2026_PMCF
        + ["2026 Total", "YTD 2025", "YTD 2026", "YTD SPLY%", "FC vs SPLY%"]
        + dev_month_cols
        + q_dev_cols
        + ["FC vs Last 6M Avg"]
        + MONTHS_2027
        + ["2027 Total"]
    )
    print(f"  Final: {joined.shape[0]:,} rows × {len(final_cols)} cols")
    return joined[final_cols].copy()


# ── Customer analysis build ───────────────────────────────────────────────────

def build_global_customer_name_lookup(oh: pd.DataFrame, fc: pd.DataFrame) -> pd.Series:
    """Return a Series mapping Customer Number → best available name.

    Priority: customer_names.json (scraped from Aera) > OH > AdjFC.
    """
    # 1. Aera-scraped names (highest priority)
    json_path = os.path.join(DIR, "customer_names.json")
    if os.path.exists(json_path):
        import json
        with open(json_path) as f:
            scraped = json.load(f)
        json_series = pd.Series(scraped, name="Customer Name")
        json_series.index.name = "Customer Number"
    else:
        json_series = pd.Series(dtype=str)

    # 2. From OH: Customer Sold-to Number → Sold-to Name
    oh_names = (
        oh[["Customer Sold-to Number", "Customer Sold-to Name"]]
        .rename(columns={"Customer Sold-to Number": "Customer Number",
                         "Customer Sold-to Name":   "Customer Name"})
    )
    # 3. From AdjFC: Customer Number → Distributor Name
    fc_names = (
        fc[["Customer Number", "Distributor Name"]]
        .rename(columns={"Distributor Name": "Customer Name"})
    )
    combined = pd.concat([oh_names, fc_names], ignore_index=True)
    combined["Customer Name"] = combined["Customer Name"].fillna("").astype(str)
    combined = combined[~combined["Customer Name"].str.strip().isin({"Not Set", "nan", ""})]
    fallback = (
        combined.groupby("Customer Number")["Customer Name"]
        .agg(lambda s: s.value_counts().index[0])
    )

    # JSON names take priority; fallback fills gaps
    valid_json = json_series[json_series.str.strip().ne("")]
    lookup = valid_json.combine_first(fallback)
    return lookup


def build_customer_order_pivot(oh: pd.DataFrame) -> pd.DataFrame:
    oh = oh.copy()
    oh = oh.rename(columns={
        "Customer Sold-to Number": "Customer Number",
        "Customer Sold-to Name":   "Customer Name",
    })
    oh["Order Qty 9LC"] = pd.to_numeric(oh["Order Qty 9LC"], errors="coerce").fillna(0)
    oh = oh[pd.to_datetime(oh["Month Year"], format="%b %Y", errors="coerce").notna()]

    _so_raw = [m.replace("SO - ", "") for m in MONTHS_2026_SO]
    keep = set(MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + _so_raw)
    oh   = oh[oh["Month Year"].isin(keep)]

    qty = (
        oh.groupby(CUSTOMER_JOIN_KEYS + ["Month Year"], dropna=False)
        ["Order Qty 9LC"].sum().reset_index()
    )
    piv = qty.pivot_table(
        index=CUSTOMER_JOIN_KEYS,
        columns="Month Year",
        values="Order Qty 9LC",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    piv.columns.name = None

    for m in MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + _so_raw:
        if m not in piv.columns:
            piv[m] = 0.0
    piv = piv.rename(columns={raw: so for raw, so in zip(_so_raw, MONTHS_2026_SO)})

    # Dimension columns (no Customer Name here — applied globally later)
    dim_avail = [c for c in OH_DIM_EXTRA if c in oh.columns]
    if dim_avail:
        dim_lkp = (
            oh.groupby(CUSTOMER_JOIN_KEYS, dropna=False)[dim_avail]
            .agg(_mode).reset_index()
        )
        piv = piv.merge(dim_lkp, on=CUSTOMER_JOIN_KEYS, how="left")

    return piv


def build_customer_adjfc_pivot(fc: pd.DataFrame) -> pd.DataFrame:
    fc = fc.copy()
    fc["Adjusted FC"] = pd.to_numeric(fc["Adjusted FC"], errors="coerce").fillna(0)
    fc = fc[fc["Month Year"].isin(set(MONTHS_2026_FC) | set(MONTHS_2027))]

    agg = (
        fc.groupby(CUSTOMER_JOIN_KEYS + ["Month Year"], dropna=False)
        ["Adjusted FC"].sum().reset_index()
    )
    piv = agg.pivot_table(
        index=CUSTOMER_JOIN_KEYS,
        columns="Month Year",
        values="Adjusted FC",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    piv.columns.name = None

    for m in MONTHS_2026_FC + MONTHS_2027:
        if m not in piv.columns:
            piv[m] = 0.0

    fc_dim_avail = {k: v for k, v in FC_DIM_MAP.items() if k in fc.columns}
    if fc_dim_avail:
        fc_dim = (
            fc.groupby(CUSTOMER_JOIN_KEYS, dropna=False)[list(fc_dim_avail)]
            .agg(_mode).reset_index()
            .rename(columns=fc_dim_avail)
        )
        piv = piv.merge(fc_dim, on=CUSTOMER_JOIN_KEYS, how="left")

    return piv


def build_customer_analysis(oh: pd.DataFrame, fc: pd.DataFrame) -> pd.DataFrame:
    """Same as build_final() but grain = Material × Country × Sub-Segments × Customer."""
    print("Building global customer name lookup...")
    name_lookup = build_global_customer_name_lookup(oh, fc)
    print(f"  {len(name_lookup):,} named customers")

    _act_range = f"Jan–{MONTHS_2026_ACT[-1].split()[0]} 2026" if MONTHS_2026_ACT else "none"
    print(f"Building Customer Order History pivot (2024 + 2025 + {_act_range})...")
    oh_piv = build_customer_order_pivot(oh)
    print(f"  {oh_piv.shape[0]:,} rows")

    _fc_range = f"{MONTHS_2026_FC[0].split()[0]}–Dec 2026" if MONTHS_2026_FC else "none"
    print(f"Building Customer AdjFC pivot ({_fc_range})...")
    fc_piv = build_customer_adjfc_pivot(fc)
    print(f"  {fc_piv.shape[0]:,} rows")

    print("Building Customer PMCF pivot (grain level)...")
    pmcf_piv = build_pmcf_pivot(CUSTOMER_JOIN_KEYS)
    print(f"  {pmcf_piv.shape[0]:,} rows")

    print("Outer joining on Material + Country + Sub-Segments + Customer...")
    joined = oh_piv.merge(fc_piv, on=CUSTOMER_JOIN_KEYS, how="outer", suffixes=("", "_fc"))
    if not pmcf_piv.empty and len(pmcf_piv) > 1:
        joined = joined.merge(pmcf_piv, on=CUSTOMER_JOIN_KEYS, how="left")
    print(f"  {joined.shape[0]:,} rows after join")

    # Backfill dimension columns from AdjFC
    for col in OH_DIM_EXTRA:
        fc_col = f"{col}_fc"
        if fc_col in joined.columns:
            joined[col] = joined[col].fillna(joined[fc_col])
            joined.drop(columns=[fc_col], inplace=True)

    for m in MONTHS_2024 + MONTHS_2025 + MONTHS_2026_ACT + MONTHS_2026_FC + MONTHS_2027:
        if m not in joined.columns:
            joined[m] = 0.0
        joined[m] = pd.to_numeric(joined[m], errors="coerce").fillna(0.0)

    # Apply global customer name lookup (covers all customers regardless of which material they appear on)
    joined["Customer Number"] = joined["Customer Number"].fillna("").astype(str).replace({"nan": "", "None": ""})
    joined["Customer Name"] = joined["Customer Number"].map(name_lookup).fillna("")

    for col in CUSTOMER_DIM_COLS:
        if col not in joined.columns:
            joined[col] = ""
        else:
            joined[col] = (
                joined[col].fillna("").astype(str)
                .replace({"nan": "", "None": ""})
            )

    joined["2024 Total"] = joined[MONTHS_2024].sum(axis=1)
    joined["2025 Total"] = joined[MONTHS_2025].sum(axis=1)
    joined["2026 Total"] = joined[MONTHS_2026_ACT + MONTHS_2026_FC].sum(axis=1)
    joined["2027 Total"] = joined[MONTHS_2027].sum(axis=1)
    joined["YTD 2025"]   = joined[YTD_2025].sum(axis=1)
    joined["YTD 2026"]   = joined[YTD_2026].sum(axis=1)
    joined["YTD SPLY%"]   = joined.apply(lambda r: _safe_pct(r["YTD 2026"],   r["YTD 2025"]),   axis=1)
    joined["FC vs SPLY%"] = joined.apply(lambda r: _safe_pct(r["2026 Total"], r["2025 Total"]), axis=1)

    dev_month_cols = []
    for m in MONTHS_2026_FC:
        py_m = m.replace("2026", "2025")
        dcol = f"Dev% {m}"
        joined[dcol] = joined.apply(
            lambda r, fc_m=m, py=py_m: _dev_pct(r.get(py, 0), r[fc_m]), axis=1
        )
        dev_month_cols.append(dcol)

    q_specs = [
        ("Q1 Dev%", Q1_2026, Q1_2025),
        ("Q2 Dev%", Q2_2026, Q2_2025),
        ("Q3 Dev%", Q3_2026, Q3_2025),
        ("Q4 Dev%", Q4_2026, Q4_2025),
    ]
    q_dev_cols = []
    for qcol, q26, q25 in q_specs:
        c26 = [c for c in q26 if c in joined.columns]
        c25 = [c for c in q25 if c in joined.columns]
        joined[qcol] = joined.apply(
            lambda r, cols26=c26, cols25=c25: _dev_pct(
                r[cols25].sum() if cols25 else 0.0,
                r[cols26].sum() if cols26 else 0.0,
            ), axis=1
        )
        q_dev_cols.append(qcol)

    valid_l6 = [c for c in LAST_6M_COLS if c in joined.columns]
    n_l6     = len(valid_l6)
    if n_l6 > 0:
        joined["FC vs Last 6M Avg"] = joined.apply(
            lambda r: _dev_pct(
                r[valid_l6].sum() / n_l6 * len(MONTHS_2026_FC),
                r[MONTHS_2026_FC].sum(),
            ), axis=1
        )
    else:
        joined["FC vs Last 6M Avg"] = ""

    for col in MONTHS_2026_SO:
        if col not in joined.columns:
            joined[col] = 0.0
        joined[col] = pd.to_numeric(joined[col], errors="coerce").fillna(0.0)

    for col in MONTHS_2026_PMCF:
        if col not in joined.columns:
            joined[col] = 0.0
        joined[col] = pd.to_numeric(joined[col], errors="coerce").fillna(0.0)

    # Join Forecast Accuracy MAPE (Lag 1, Jun 2025–May 2026) at SKU × Country grain
    mape_cols_added = []
    if os.path.exists(MAPE_PARQUET):
        print("Joining Forecast Accuracy MAPE (grain level)...")
        mape_df = pd.read_parquet(MAPE_PARQUET)
        num_mape = [c for c in MAPE_COLS if c in mape_df.columns]
        mape_agg = (
            mape_df.groupby(["Material Number", "Country Name"])[num_mape]
            .mean()
            .reset_index()
        )
        joined = joined.merge(mape_agg, on=["Material Number", "Country Name"], how="left")
        for col in num_mape:
            joined[col] = pd.to_numeric(joined[col], errors="coerce")
        mape_cols_added = num_mape
        matched = joined[mape_cols_added[0]].notna().sum()
        print(f"  {matched:,} / {len(joined):,} rows matched MAPE data")
    else:
        print("  ⚠ forecast_accuracy_grain.parquet not found — skipping MAPE join")

    final_cols = (
        CUSTOMER_DIM_COLS
        + MONTHS_2024
        + ["2024 Total"]
        + MONTHS_2025
        + ["2025 Total"]
        + MONTHS_2026_ACT
        + MONTHS_2026_FC
        + MONTHS_2026_SO
        + MONTHS_2026_PMCF
        + ["2026 Total", "YTD 2025", "YTD 2026", "YTD SPLY%", "FC vs SPLY%"]
        + dev_month_cols
        + q_dev_cols
        + ["FC vs Last 6M Avg"]
        + mape_cols_added
        + MONTHS_2027
        + ["2027 Total"]
    )
    print(f"  Final: {joined.shape[0]:,} rows × {len(final_cols)} cols")
    return joined[final_cols].copy()


# ── Upload ────────────────────────────────────────────────────────────────────

def write_tab(gc: gspread.Client, df: pd.DataFrame, tab_name: str, header_rows: int = 2):
    """Write df to a tab in the sheet.

    header_rows: number of rows at the top to preserve (default 2 for Consolidated).
                 Set to 0 to start column headers + data from row 1.
    """
    rows, cols = df.shape
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet(tab_name)
        tab_exists = True
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=rows + header_rows + 2, cols=cols)
        tab_exists = False
        print(f"  Created new tab '{tab_name}'")

    hdr_row  = header_rows + 1   # row where column headers go
    data_row = header_rows + 2   # row where data starts

    if tab_exists:
        total_rows = ws.row_count
        if total_rows >= hdr_row:
            last_col_letter = gspread.utils.rowcol_to_a1(1, cols).rstrip("0123456789")
            ws.batch_clear([f"A{hdr_row}:{last_col_letter}{total_rows}"])
            print(f"  Cleared rows {hdr_row}–{total_rows}")

    ws.update(
        values=[df.columns.tolist()],
        range_name=f"A{hdr_row}",
        value_input_option="USER_ENTERED",
    )
    print(f"  Wrote column headers to row {hdr_row}")

    ws.resize(rows=max(rows + data_row, ws.row_count), cols=max(cols, ws.col_count))

    df_out = df.copy()
    for col in df_out.select_dtypes(include=["number"]).columns:
        df_out[col] = df_out[col].fillna(0).round(2)
    df_out = df_out.astype(str).replace({"nan": "", "None": ""})
    data = df_out.values.tolist()

    print(f"  Writing {len(data):,} rows × {cols} cols starting at row {data_row}...")
    for i in range(0, len(data), CHUNK):
        chunk   = data[i : i + CHUNK]
        start_r = i + data_row
        end_r   = start_r + len(chunk) - 1
        ws.update(
            values=chunk,
            range_name=f"A{start_r}:{gspread.utils.rowcol_to_a1(end_r, cols)}",
            value_input_option="USER_ENTERED",
        )
        print(f"  Written {end_r - data_row + 1:,}/{len(data):,} rows...")
        if i + CHUNK < len(data):
            time.sleep(1)

    ws.freeze(rows=hdr_row)
    print(f"\n  ✓ Done — '{tab_name}' tab updated")
    print(f"    https://docs.google.com/spreadsheets/d/{SHEET_ID}")


def _apply_formatting(sheet_id_str: str, tab_id: int, n_rows: int, n_cols: int):
    """Apply Consolidated-style formatting to a tab via the Sheets API."""
    import subprocess
    from google.oauth2 import credentials as goog_creds
    from googleapiclient.discovery import build

    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        creds   = goog_creds.Credentials(token=token)
        service = build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"  ⚠ Could not apply formatting: {e}")
        return

    CONS_ID = 0  # Consolidated tab is always sheetId=0
    requests = [
        # Clear any active filters on both source and target before copyPaste
        # (Sheets API rejects paste operations on ranges with filtered-out rows)
        {"clearBasicFilter": {"sheetId": CONS_ID}},
        {"clearBasicFilter": {"sheetId": tab_id}},
        # Copy header rows 1-3 formatting from Consolidated
        {"copyPaste": {
            "source":      {"sheetId": CONS_ID,  "startRowIndex": 0, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "destination": {"sheetId": tab_id,   "startRowIndex": 0, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "pasteType": "PASTE_FORMAT",
        }},
        # Copy data row formatting pattern (rows 4-5) across all data rows
        {"copyPaste": {
            "source":      {"sheetId": CONS_ID, "startRowIndex": 3, "endRowIndex": 5, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "destination": {"sheetId": tab_id,  "startRowIndex": 3, "endRowIndex": n_rows + 3, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "pasteType": "PASTE_FORMAT",
        }},
        # Freeze 3 header rows
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 3}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id_str, body={"requests": requests}
    ).execute()
    print("  ✓ Formatting applied")


Q1_2026 = ["Jan 2026", "Feb 2026", "Mar 2026"]
Q1_2027 = ["Jan 2027", "Feb 2027", "Mar 2027"]

DEV_TAB_NAME = "Top 10 items by dev — sales based"
# Columns G–N in that tab correspond to Dev% May–Dec 2026
DEV_TAB_MONTHS = [
    ("Dev% May", "Dev% May 2026"),
    ("Dev% Jun", "Dev% Jun 2026"),
    ("Dev% Jul", "Dev% Jul 2026"),
    ("Dev% Aug", "Dev% Aug 2026"),
    ("Dev% Sep", "Dev% Sep 2026"),
    ("Dev% Oct", "Dev% Oct 2026"),
    ("Dev% Nov", "Dev% Nov 2026"),
    ("Dev% Dec", "Dev% Dec 2026"),
]


def write_dev_tab(gc: gspread.Client, final_df: pd.DataFrame):
    """Refresh Dev% May–Dec columns in the Top 10 dev-sales tab from the latest pipeline data.

    Preserves existing row order, YTD formulas, and Top-10 formulas.
    Only overwrites columns G–N (the 8 Dev% month columns).
    """
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(DEV_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  ⚠ Tab '{DEV_TAB_NAME}' not found — skipping Dev% refresh")
        return

    print(f"  Reading existing row keys from '{DEV_TAB_NAME}'...")
    # Read columns A–E (keys: Material #, Desc, Country, Business Segment, Sub-Segment)
    all_vals = ws.get_all_values()
    header_row_idx = 2  # row index 2 = row 3 (0-based)
    data_rows = all_vals[header_row_idx + 1:]  # rows 4+ (0-based index 3+)

    # Build lookup from final_df: (Material Number, Country Name, Business Segment, Sub-Segments) → dev%
    # Aggregate across customers since the dev tab is at material/country/segment grain
    # DEV_TAB_MONTHS always lists May–Dec, but some may now be actuals (not forecast dev% columns).
    all_dev_tab_cols = [df_col for _, df_col in DEV_TAB_MONTHS]
    present_dev_cols = [c for c in all_dev_tab_cols if c in final_df.columns]
    n_dev_tab        = len(DEV_TAB_MONTHS)  # always 8 (sheet columns G–N)

    agg_keys = ["Material Number", "Country Name", "Business Segment", "Sub-Segments"]
    num_cols  = present_dev_cols + ["2025 Total"]
    agg_df = final_df[agg_keys + num_cols].copy()
    for c in num_cols:
        agg_df[c] = pd.to_numeric(agg_df[c], errors="coerce").fillna(0.0)
    agg_df = agg_df.groupby(agg_keys, dropna=False)[num_cols].sum().reset_index()

    lookup = {}
    for _, row in agg_df[agg_keys + present_dev_cols].iterrows():
        key = (str(row["Material Number"]), str(row["Country Name"]),
               str(row["Business Segment"]), str(row["Sub-Segments"]))
        # Pad months that are now actuals (not forecast) with 0 so the sheet layout stays intact
        lookup[key] = [
            round(float(row[c]), 2) if c in present_dev_cols and row[c] != "" else 0.0
            for c in all_dev_tab_cols
        ]

    # Build 2025 Total lookup for column F — better coverage than YTD 2026 (full year vs 4 months)
    ytd_lookup = {}
    for _, row in agg_df[agg_keys + ["2025 Total"]].iterrows():
        key = (str(row["Material Number"]), str(row["Country Name"]),
               str(row["Business Segment"]), str(row["Sub-Segments"]))
        ytd_lookup[key] = round(float(row["2025 Total"]), 4) if row["2025 Total"] != "" else 0.0

    # Build updated values for columns F (YTD) and G–N (Dev% May–Dec, 8 cols always)
    updated_f   = []   # column F: YTD 2026
    updated_gn  = []   # columns G–N: Dev% months (n_dev_tab = 8)
    matched = 0
    for r in data_rows:
        if len(r) < 5:
            updated_f.append([0.0])
            updated_gn.append([""] * n_dev_tab)
            continue
        key = (str(r[0]).strip(), str(r[2]).strip(), str(r[3]).strip(), str(r[4]).strip())
        if key in lookup:
            updated_f.append([ytd_lookup.get(key, 0.0)])
            updated_gn.append(lookup[key])
            matched += 1
        else:
            updated_f.append([0.0])
            updated_gn.append([0.0] * n_dev_tab)  # 0 not "" so chart formulas return 0

    print(f"  Matched {matched:,}/{len(data_rows):,} rows — writing YTD (col F) + Dev% (cols G–N)...")
    data_start_row = header_row_idx + 2  # 1-based row 4

    # Write column F (YTD 2026)
    for i in range(0, len(updated_f), CHUNK):
        start_r = data_start_row + i
        end_r   = start_r + len(updated_f[i:i+CHUNK]) - 1
        ws.update(values=updated_f[i:i+CHUNK], range_name=f"F{start_r}:F{end_r}",
                  value_input_option="USER_ENTERED")
        if i + CHUNK < len(updated_f):
            time.sleep(1)

    # Write columns G–N (Dev% months)
    for i in range(0, len(updated_gn), CHUNK):
        chunk   = updated_gn[i:i+CHUNK]
        start_r = data_start_row + i
        end_r   = start_r + len(chunk) - 1
        ws.update(values=chunk, range_name=f"G{start_r}:N{end_r}",
                  value_input_option="USER_ENTERED")
        if i + CHUNK < len(updated_gn):
            time.sleep(1)
    print(f"  ✓ YTD + Dev% columns refreshed in '{DEV_TAB_NAME}'")


def build_q1_2027_analysis(oh: pd.DataFrame, fc: pd.DataFrame) -> pd.DataFrame:
    """Build Q1 2027 AdjFC vs Q1 2026 Actuals comparison.

    Grain: Material × Country × Sub-Segments (same as sply_analysis).
    Columns: dimensions + Q1 2026 actuals + Q1 2026 Total +
             Q1 2027 AdjFC + Q1 2027 Total + Dev% per month + Q1 Dev%.
    """
    # ── Q1 2026 actuals from Order History ───────────────────────────────────
    oh26 = oh.copy()
    if "Customer Sold-to Number" in oh26.columns:
        oh26 = oh26.rename(columns={"Customer Sold-to Number": "Customer Number"})
    oh26["Order Qty 9LC"] = pd.to_numeric(oh26["Order Qty 9LC"], errors="coerce").fillna(0)
    oh26 = oh26[oh26["Month Year"].isin(set(Q1_2026))]

    qty26 = (
        oh26.groupby(JOIN_KEYS + ["Month Year"], dropna=False)
        ["Order Qty 9LC"].sum().reset_index()
    )
    piv26 = qty26.pivot_table(
        index=JOIN_KEYS, columns="Month Year",
        values="Order Qty 9LC", aggfunc="sum", fill_value=0,
    ).reset_index()
    piv26.columns.name = None
    for m in Q1_2026:
        if m not in piv26.columns:
            piv26[m] = 0.0

    # Carry dimension columns from OH
    dim_avail = [c for c in OH_DIM_EXTRA if c in oh26.columns]
    if dim_avail:
        dim_lkp = oh26.groupby(JOIN_KEYS, dropna=False)[dim_avail].agg(_mode).reset_index()
        piv26 = piv26.merge(dim_lkp, on=JOIN_KEYS, how="left")

    # ── Q1 2027 AdjFC ────────────────────────────────────────────────────────
    fc27 = fc.copy()
    fc27["Adjusted FC"] = pd.to_numeric(fc27["Adjusted FC"], errors="coerce").fillna(0)
    fc27 = fc27[fc27["Month Year"].isin(set(Q1_2027))]

    agg27 = (
        fc27.groupby(JOIN_KEYS + ["Month Year"], dropna=False)
        ["Adjusted FC"].sum().reset_index()
    )
    piv27 = agg27.pivot_table(
        index=JOIN_KEYS, columns="Month Year",
        values="Adjusted FC", aggfunc="sum", fill_value=0,
    ).reset_index()
    piv27.columns.name = None
    for m in Q1_2027:
        if m not in piv27.columns:
            piv27[m] = 0.0

    # Carry dimension columns from FC where OH has none
    fc_dim_avail = {k: v for k, v in FC_DIM_MAP.items() if k in fc27.columns}
    if fc_dim_avail:
        fc_dim = (
            fc27.groupby(JOIN_KEYS, dropna=False)[list(fc_dim_avail)]
            .agg(_mode).reset_index().rename(columns=fc_dim_avail)
        )
        piv27 = piv27.merge(fc_dim, on=JOIN_KEYS, how="left")

    # ── Join ─────────────────────────────────────────────────────────────────
    joined = piv26.merge(piv27, on=JOIN_KEYS, how="outer", suffixes=("", "_fc"))

    for col in OH_DIM_EXTRA:
        fc_col = f"{col}_fc"
        if fc_col in joined.columns:
            joined[col] = joined[col].fillna(joined[fc_col])
            joined.drop(columns=[fc_col], inplace=True)

    for m in Q1_2026 + Q1_2027:
        if m not in joined.columns:
            joined[m] = 0.0
        joined[m] = pd.to_numeric(joined[m], errors="coerce").fillna(0.0)

    # Apply customer name lookup
    name_lookup = build_global_customer_name_lookup(oh, fc)
    joined["Customer Number"] = joined["Customer Number"].fillna("").astype(str).replace({"nan": "", "None": ""})
    joined["Customer Name"] = joined["Customer Number"].map(name_lookup).fillna("")

    for col in DIM_COLS:
        if col not in joined.columns:
            joined[col] = ""
        else:
            joined[col] = joined[col].fillna("").astype(str).replace({"nan": "", "None": ""})

    # ── Totals ────────────────────────────────────────────────────────────────
    joined["Q1 2026 Total"] = joined[Q1_2026].sum(axis=1)
    joined["Q1 2027 Total"] = joined[Q1_2027].sum(axis=1)

    # ── Dev% per month (Q1 2027 AdjFC vs same month Q1 2026 Actuals) ─────────
    dev_cols = []
    for m26, m27 in zip(Q1_2026, Q1_2027):
        month = m26.split()[0]          # "Jan", "Feb", "Mar"
        dcol  = f"Dev% {month}"
        joined[dcol] = joined.apply(
            lambda r, a=m26, f=m27: _dev_pct(r[a], r[f]), axis=1
        )
        dev_cols.append(dcol)

    joined["Q1 Dev%"] = joined.apply(
        lambda r: _dev_pct(r["Q1 2026 Total"], r["Q1 2027 Total"]), axis=1
    )

    final_cols = (
        DIM_COLS
        + Q1_2026 + ["Q1 2026 Total"]
        + Q1_2027 + ["Q1 2027 Total"]
        + dev_cols + ["Q1 Dev%"]
    )
    print(f"  Q1 comparison: {joined.shape[0]:,} rows × {len(final_cols)} cols")
    return joined[final_cols].copy()


def write_sheet(gc: gspread.Client, df: pd.DataFrame):
    write_tab(gc, df, TAB_NAME, header_rows=2)


def write_customer_sheet(gc: gspread.Client, df: pd.DataFrame):
    tab_name = "Customer Analysis"
    write_tab(gc, df, tab_name, header_rows=2)
    # Write title to row 1
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(tab_name)
    ws.update(
        values=[["Aera EMEA & APAC — Customer Analysis  |  2024/2025 Order History vs 2026 AdjFC  |  All values in 9LC"]],
        range_name="A1",
        value_input_option="USER_ENTERED",
    )
    try:
        _apply_formatting(SHEET_ID, ws.id, len(df), df.shape[1])
    except Exception as e:
        print(f"  ⚠ Formatting skipped (data upload succeeded): {e}")


def main():
    for path in [OH_PARQUET, FC_PARQUET]:
        if not os.path.exists(path):
            raise SystemExit(f"✗ Missing: {path}")

    print(f"Loading {os.path.basename(OH_PARQUET)}...")
    oh = pd.read_parquet(OH_PARQUET)
    print(f"  {len(oh):,} rows × {oh.shape[1]} cols")

    print(f"Loading {os.path.basename(FC_PARQUET)}...")
    fc = pd.read_parquet(FC_PARQUET)
    # Aera zeroes out Adjusted FC for past (actualized) months but preserves the forecast
    # value in Adj FC 9LC. For future months, Adjusted FC holds the correct 9LC value
    # (Adj FC 9LC has broken conversion factors for some SKUs, e.g. 2x for Añejo).
    # Rule: use Adjusted FC when > 0 (future months), else Adj FC 9LC (past months).
    fc["Adjusted FC"] = pd.to_numeric(fc["Adjusted FC"], errors="coerce").fillna(0)
    if "Adj FC 9LC" in fc.columns:
        adj9lc = pd.to_numeric(fc["Adj FC 9LC"], errors="coerce").fillna(0)
        fc["Adjusted FC"] = fc["Adjusted FC"].where(fc["Adjusted FC"] > 0, adj9lc)
    print(f"  {len(fc):,} rows × {fc.shape[1]} cols")

    print("\nAuthenticating with Google Sheets...")
    gc = _gc()

    print("\nBuilding Consolidated data (Material × Country × Sub-Segments)...")
    final = build_final(oh, fc)
    write_sheet(gc, final)

    print("\nRefreshing Dev% in Top 10 dev tab...")
    write_dev_tab(gc, final)

    print("\nBuilding Q1 2027 vs Q1 2026 analysis...")
    q1 = build_q1_2027_analysis(oh, fc)
    write_tab(gc, q1, "Q1 2027 vs Q1 2026", header_rows=2)

    print("\nBuilding Customer Analysis data (Material × Country × Sub-Segments × Customer)...")
    customer = build_customer_analysis(oh, fc)
    write_customer_sheet(gc, customer)


if __name__ == "__main__":
    main()
