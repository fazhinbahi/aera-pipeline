"""
build_lag1_table.py — Lag-1 and Lag-3 forecast vs Actuals table (all closed 2026 months).

Pairs are computed dynamically from today's date — no manual updates needed each month.
"""

import datetime
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aera_auth import ensure_token, login as _aera_login

DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = "https://becleproximo.aeratechnology.com"
DATA_URL = (
    f"{BASE_URL}/ispring/awc?v=3"
    "&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7"
    "&ServiceName=ExecuteBIObjectData"
)

BIOID   = "B4D7A7D4_94EC_4DED_8A0F_CE4291E8CA7A"
SHEETID = "AEAC976F-1BE2-453F-83F6-33428B763610"
FID     = "683F21F6_1495_4835_8474_1E09C8BDFB24"

SNAPSHOT_DIM = "F2C3C017-1EDF-11ED-A548-0A617A24E20D_4DC610B6-B7C1-4D4C-A1C6-22E9FE11F662"
FORECAST_DIM = "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371"

ROW = (
    "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47|,"  # Country Name
    "50191AB8-1EDB-11ED-A548-0A617A24E20D_9EB3B832-5F1A-4FF1-814B-CC82933E9F14|,"  # Customer Number
    "04E2EDB1-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|"   # Material Number
)
MEA = "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|||||"  # Adjusted FC only

PAGE_LIMIT = 2000

_TODAY    = datetime.date.today()
_CUR_START = _TODAY.replace(day=1)

# A rolling window, so September can be read against September a year earlier and
# so the file never needs editing at a year boundary. 13 months, not 12: the
# window has to include both ends of the year-on-year comparison.
WINDOW_MONTHS = 13
LAGS = [1, 2, 3, 4, 5, 6]


def _shift(month_str: str, delta: int) -> str:
    """Return the month string `delta` months earlier (negative) or later."""
    dt = datetime.datetime.strptime(month_str, "%b %Y")
    m, y = dt.month + delta, dt.year
    while m <= 0:  m += 12; y -= 1
    while m > 12: m -= 12; y += 1
    return datetime.date(y, m, 1).strftime("%b %Y")


def _closed_months(n=WINDOW_MONTHS):
    """The n most recently completed months, oldest first."""
    last = _shift(_CUR_START.strftime("%b %Y"), -1)      # last fully closed month
    return [_shift(last, -(n - 1 - i)) for i in range(n)]


FORECAST_MONTHS = _closed_months()

def col_for(lag, month):
    return f"Fcst{lag}M_{month.replace(' ', '_')}"

# Lags 1-6 across the window. A snapshot is frozen once taken, so a pair already
# in the parquet is not re-fetched — see REFRESH_TARGETS for the exception.
WANTED_PAIRS = [(lag, _shift(m, -lag), m) for m in FORECAST_MONTHS for lag in LAGS]

# The one thing that does still move: Aera keeps adding grains to recent
# snapshots after we first read them (measured drift +0.29% at n-2 over a day),
# so the most recently closed month is re-fetched in full every run.
REFRESH_TARGETS = set(FORECAST_MONTHS[-1:])

# ── Corrupted source snapshots ────────────────────────────────────────────────
# Aera's snapshot dimension returns each grain EXACTLY TWICE for some snapshot
# months, and the BIO sums them before we see the rows (one row per grain, value
# doubled). Confirmed 23 Sep and still present 5 Oct 2026 for May and Jun 2026.
# Evidence: holding one target month fixed, every other snapshot sits in a tight
# band while these read ~2x, and the per-grain ratio against clean neighbours has
# median exactly 2.00. Raised with Aera; correct while it persists.
#
# Detection is per target month rather than against a hardcoded list of suspects,
# because the window now reaches back into 2025, where no snapshot has ever been
# checked. Each target is forecast by six snapshots; a doubled one stands out
# against the median of the others and needs no extra fetch. This also means the
# correction disappears by itself the day Aera fixes the feed.
DOUBLE_LO, DOUBLE_HI = 1.6, 2.4
MAX_DOUBLED_PER_TARGET = 2   # beyond this the median is no longer a clean ruler


def correct_doubling(df, month, fresh_cols):
    """Halve any snapshot of `month` that reads ~2x the others forecasting it.

    `fresh_cols` are the columns fetched this run. Columns already in the
    parquet were corrected when they were written, so they serve as the clean
    ruler here and must never be corrected twice.
    """
    present = [col_for(l, month) for l in LAGS if col_for(l, month) in df.columns]
    totals = {c: df[c].sum() for c in present}
    ruler = [v for c, v in totals.items() if v > 0]
    if len(ruler) < 3:
        return df, []                      # too few snapshots to judge against
    med = float(np.median(ruler))
    hits = [c for c in fresh_cols
            if totals.get(c, 0) > 0 and DOUBLE_LO <= totals[c] / med <= DOUBLE_HI]
    if len(hits) > MAX_DOUBLED_PER_TARGET:
        print(f"    ⚠ {month}: {len(hits)} of {len(present)} snapshots read ~2x — "
              f"that is too many for the median to be trusted. NOT correcting; "
              f"check the feed by hand.")
        return df, []
    for c in hits:
        print(f"    ⚠ {month}: {c} reads {totals[c] / med:.2f}x the median of the "
              f"other snapshots — halving")
        df[c] = df[c] / 2.0
    return df, hits


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(token, jsessionid, payload, lb="", retries=8):
    def _c(tok, sid, lb): c = {"JSESSIONID": sid, "accessToken": tok, "token": tok}; c.update({"lb-instance-id": lb} if lb else {}); return c
    def _h(tok, lb): h = {"Authorization": tok, "Content-Type": "application/x-www-form-urlencoded", "Origin": BASE_URL, "Referer": BASE_URL}; h.update({"lb-instance-id": lb} if lb else {}); return h
    cookies, headers = _c(token, jsessionid, lb), _h(token, lb)
    for attempt in range(retries):
        try:
            resp = requests.post(DATA_URL, data=payload, headers=headers, cookies=cookies, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            if resp.status_code == 403 and attempt < retries - 1:
                print(f"    ⚠ 403 — re-logging in..."); time.sleep(10)
                fresh = _aera_login(); token = fresh["access_token"]
                jsessionid = fresh.get("jsessionid", jsessionid); lb = fresh.get("lb_instance_id", lb)
                cookies, headers = _c(token, jsessionid, lb), _h(token, lb); continue
            raise
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1: time.sleep(10); continue
            raise


# ── Fetch one Lag-1 pair ──────────────────────────────────────────────────────

def fetch_lag_pair(snapshot_month, forecast_month, token, jsessionid, lb):
    """One snapshot's view of one month, per customer x material x country.

    The customer grain matters: lag1_data is keyed on it, so a coarser return
    here would repeat each forecast across a customer's rows and inflate every
    total downstream.
    """
    print(f"  Fetching Lag-1 {forecast_month} (snapshot={snapshot_month})...", end=" ", flush=True)
    t0 = time.time()

    base = {
        "sheetid": SHEETID, "bioid": BIOID, "fid": FID,
        "row": ROW, "col": "", "mea": MEA,
        "filter": f"{SNAPSHOT_DIM}~cnt|{snapshot_month}~EN^{FORECAST_DIM}~cnt|{forecast_month}~EN",
        "pot": "0", "sort": FORECAST_DIM, "dir": "ASC",
        "in_val": "[]", "source": "report",
        "uom": "", "currency": "", "rate": "", "currencyDate": "T",
        "plimit": str(PAGE_LIMIT),
    }

    raw = _post(token, jsessionid, {**base, "pstart": "0", "page": "1"}, lb)
    total = raw.get("totalRows", 0)
    if total == 0:
        print(f"0 rows")
        return pd.DataFrame(columns=["Country_Name", "Material_Number", "Adjusted_FC"])

    pages = max(1, math.ceil(total / PAGE_LIMIT))
    all_rows = list(raw["data"])
    for p in range(1, pages):
        r = _post(token, jsessionid, {**base, "pstart": str(p * PAGE_LIMIT), "page": str(p + 1)}, lb)
        all_rows.extend(r["data"])

    col_ids = [f["name"] for f in raw["metaData"]["fields"]]
    df = pd.DataFrame(all_rows, columns=col_ids)

    # Rename to friendly names
    country_id   = "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47"
    customer_id  = "50191AB8-1EDB-11ED-A548-0A617A24E20D_9EB3B832-5F1A-4FF1-814B-CC82933E9F14"
    material_id  = "04E2EDB1-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC"
    adjfc_id     = "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|"
    df = df.rename(columns={
        country_id: "Country_Name", customer_id: "Customer_Number",
        material_id: "Material_Number", adjfc_id: "Adjusted_FC"
    })
    df["Adjusted_FC"] = pd.to_numeric(df["Adjusted_FC"], errors="coerce").fillna(0)

    # Group by Material + Country + Customer
    agg = df.groupby(["Material_Number", "Country_Name", "Customer_Number"], as_index=False)["Adjusted_FC"].sum()
    agg = agg[agg["Adjusted_FC"] != 0]

    # Drop the un-countried block before anything measures this snapshot.
    #
    # Two different source defects look identical in a total. May and Jun 2026
    # are genuinely doubled: every grain is 2x, Australia included, and halving
    # the column is right. Sep 2026 is not doubled at all — Australia reads
    # 934,691 against 924,504 in Aug and 943,212 in Oct — but it carries a
    # second, un-countried copy of the book under Country = "Not Set", 8.7m
    # cases, 49.7% of the snapshot. A total cannot tell the two apart, so a
    # doubling check run on one would halve Sep's correctly-keyed grains in half.
    #
    # Dropping Not Set separates them: Sep then reads 1.0x and is left alone,
    # while May and Jun still read 2.0x and are still corrected. Nothing real is
    # lost — a clean snapshot carries 300-900 cases here against millions.
    ns = agg["Country_Name"].astype(str).str.strip().eq("Not Set")
    if ns.any():
        lost = agg.loc[ns, "Adjusted_FC"].sum()
        share = lost / agg["Adjusted_FC"].sum() * 100
        print(f"    dropped {ns.sum():,} 'Not Set' rows ({lost:,.0f} cases, {share:.1f}%)"
              + ("  ← phantom block, not real volume" if share > 5 else ""))
        agg = agg[~ns]

    print(f"{total:,} rows → {len(agg):,} customer×material×country grains "
          f"({time.time()-t0:.0f}s)")
    return agg


# ── Booked Sales Orders from BQ (always current) ─────────────────────────────

GCP_PROJECT = "euphoric-hull-442815-n8"
DATASET     = "aera_demand_planning"

def load_sales_orders_bq() -> pd.DataFrame:
    """Booked SOs from BQ order_history_raw — updated daily by the pipeline."""
    from load_to_bq import _client
    client = _client()
    months_list = ", ".join(f"'{m}'" for m in FORECAST_MONTHS)
    q = f"""
        SELECT
            Material_Number,
            Country_Name,
            Customer_Bill_to_Number AS Customer_Number,
            Month_Year,
            SUM(Order_Qty_9LC) AS SO_9LC
        FROM `{GCP_PROJECT}.{DATASET}.order_history_raw`
        WHERE Month_Year IN ({months_list})
        GROUP BY 1, 2, 3, 4
        HAVING SUM(Order_Qty_9LC) > 0
    """
    df = client.query(q).to_dataframe()
    df["Customer_Number"] = df["Customer_Number"].astype(str)
    print(f"  {len(df):,} SO rows from BQ order_history_raw")
    return df


# ── Actuals: MAX(invoiced, booked SO) per grain ───────────────────────────────

def load_actuals():
    """Best actual = MAX(invoiced actuals from AdjFC, booked SOs from order_history)."""
    path = os.path.join(DIR, "adjfc_nz.parquet")
    df = pd.read_parquet(path)

    act = df[df["Month Year"].isin(FORECAST_MONTHS)][
        ["Material Number", "Country Name", "Customer Number", "Month Year", "Actuals"]
    ].copy()
    act.columns = ["Material_Number", "Country_Name", "Customer_Number", "Month_Year", "Actuals"]
    act["Actuals"] = pd.to_numeric(act["Actuals"], errors="coerce").fillna(0)
    act["Customer_Number"] = act["Customer_Number"].astype(str)
    act = act.groupby(["Material_Number", "Country_Name", "Customer_Number", "Month_Year"],
                      as_index=False)["Actuals"].sum()

    print("Loading Sales Orders from BQ...")
    so = load_sales_orders_bq()

    # Outer join — keep rows from either source, take the higher value
    KEY = ["Material_Number", "Country_Name", "Customer_Number", "Month_Year"]
    merged = act.merge(so, on=KEY, how="outer")
    merged["Actuals"] = merged["Actuals"].fillna(0)
    merged["SO_9LC"]  = merged["SO_9LC"].fillna(0)
    merged["Best_Actual"] = merged[["Actuals", "SO_9LC"]].max(axis=1)
    merged = merged[merged["Best_Actual"] > 0]

    # Pivot wide: one Actual_{m}_2026 column per closed month
    pivot = merged.pivot_table(
        index=["Material_Number", "Country_Name", "Customer_Number"],
        columns="Month_Year",
        values="Best_Actual",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    pivot.columns.name = None

    for m in FORECAST_MONTHS:
        if m not in pivot.columns:
            pivot[m] = 0.0

    rename = {m: f"Actual_{m.replace(' ', '_')}" for m in FORECAST_MONTHS}
    pivot = pivot.rename(columns=rename)
    return pivot


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.time()
    tok = ensure_token(min_seconds=300)
    token, jsessionid, lb = tok["access_token"], tok.get("jsessionid", ""), tok.get("lb_instance_id", "")

    KEY = ["Material_Number", "Country_Name", "Customer_Number"]

    # ── What is already on disk ───────────────────────────────────────────────
    # Snapshots are frozen once taken, so re-fetching all 78 pairs every day
    # would be 25 minutes spent re-reading immutable numbers. Keep what we have
    # and fetch only what is missing.
    prior_path = os.path.join(DIR, "lag1_vs_actuals.parquet")
    prior = pd.read_parquet(prior_path) if os.path.exists(prior_path) else None
    if prior is not None:
        for k in KEY:
            prior[k] = prior[k].astype(str)
        print(f"Existing parquet: {len(prior):,} rows, "
              f"{sum(c.startswith('Fcst') for c in prior.columns)} forecast columns")

    have = set(prior.columns) if prior is not None else set()
    todo = [(lag, snap, tgt) for lag, snap, tgt in WANTED_PAIRS
            if col_for(lag, tgt) not in have or tgt in REFRESH_TARGETS]
    print(f"\nWindow {FORECAST_MONTHS[0]} – {FORECAST_MONTHS[-1]} "
          f"({len(FORECAST_MONTHS)} months x {len(LAGS)} lags = {len(WANTED_PAIRS)} pairs)")
    print(f"To fetch: {len(todo)}  ({len(WANTED_PAIRS) - len(todo)} already held)\n")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    fetched, t_tok = {}, time.time()
    for i, (lag, snapshot, target) in enumerate(todo, 1):
        if time.time() - t_tok > 1800:        # token lives ~45 min
            tok = ensure_token(min_seconds=300)
            token, jsessionid, lb = (tok["access_token"], tok.get("jsessionid", ""),
                                     tok.get("lb_instance_id", ""))
            t_tok = time.time()
        col = col_for(lag, target)
        print(f"[{i}/{len(todo)}] n-{lag}  {target}  (snapshot {snapshot})", flush=True)
        df = fetch_lag_pair(snapshot, target, token, jsessionid, lb)
        fetched[col] = df.rename(columns={"Adjusted_FC": col})

    # ── Merge, starting from what we already had ──────────────────────────────
    print("\nMerging lag frames...")
    lag_wide = prior.drop(columns=[c for c in prior.columns
                                   if c.startswith("Actual_")], errors="ignore") \
        if prior is not None else None
    if lag_wide is not None:
        lag_wide = lag_wide.drop(columns=list(fetched), errors="ignore")
    for col, frame in fetched.items():
        for k in KEY:
            frame[k] = frame[k].astype(str)
        lag_wide = frame if lag_wide is None else lag_wide.merge(frame, on=KEY, how="outer")

    lag_col_names = [col_for(l, m) for m in FORECAST_MONTHS for l in LAGS
                     if col_for(l, m) in lag_wide.columns]
    for c in lag_col_names:
        lag_wide[c] = lag_wide[c].fillna(0)

    # ── Correct the source duplication, per target month ──────────────────────
    print("\nChecking each target month's snapshots for the duplication defect...")
    for m in FORECAST_MONTHS:
        fresh = [c for c in fetched if c in {col_for(l, m) for l in LAGS}]
        if fresh:
            lag_wide, _ = correct_doubling(lag_wide, m, fresh)

    # ── Load actuals ──────────────────────────────────────────────────────────
    print("Loading actuals from adjfc parquet...")
    actuals = load_actuals()
    actual_col_names = [f"Actual_{m.replace(' ', '_')}" for m in FORECAST_MONTHS]

    # ── Join ──────────────────────────────────────────────────────────────────
    # Ensure Customer_Number is string in both before joining
    lag_wide["Customer_Number"] = lag_wide["Customer_Number"].astype(str)
    actuals["Customer_Number"] = actuals["Customer_Number"].astype(str)
    result = lag_wide.merge(actuals, on=KEY, how="outer")
    for c in lag_col_names + actual_col_names:
        result[c] = result[c].fillna(0)

    # Keep only rows with any non-zero value
    value_cols = lag_col_names + actual_col_names
    result = result[(result[value_cols] != 0).any(axis=1)]

    # Sort
    result = result.sort_values(["Country_Name", "Customer_Number", "Material_Number"]).reset_index(drop=True)

    col_order = ["Material_Number", "Country_Name", "Customer_Number"] + lag_col_names + actual_col_names
    result = result[col_order]

    # ── Save ──────────────────────────────────────────────────────────────────
    out_xlsx = os.path.join(DIR, "lag1_vs_actuals.xlsx")
    out_parquet = os.path.join(DIR, "lag1_vs_actuals.parquet")

    result.to_parquet(out_parquet, index=False)

    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        result.to_excel(writer, index=False, sheet_name="Lag1 vs Actuals")
        ws = writer.sheets["Lag1 vs Actuals"]

        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        # Header formatting
        lag_fill    = PatternFill("solid", fgColor="C6EFCE")   # light green — lag
        actual_fill = PatternFill("solid", fgColor="BDD7EE")   # light blue — actual
        dim_fill    = PatternFill("solid", fgColor="D9D9D9")   # grey — dimension
        bold        = Font(bold=True)
        center      = Alignment(horizontal="center")
        thin        = Side(style="thin")
        border      = Border(left=thin, right=thin, top=thin, bottom=thin)

        for col_idx, col_name in enumerate(result.columns, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = bold
            cell.alignment = center
            cell.border = border
            if col_name.startswith("Lag"):
                cell.fill = lag_fill
            elif col_name.startswith("Actual_"):
                cell.fill = actual_fill
            else:
                cell.fill = dim_fill

        # Number format for value columns
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for cell in row:
                col_name = result.columns[cell.column - 1]
                if col_name.startswith(("Lag", "Actual_")):
                    cell.number_format = "#,##0.0"
                cell.border = border

        # Column widths
        ws.column_dimensions["A"].width = 18  # Material_Number
        ws.column_dimensions["B"].width = 20  # Country_Name
        ws.column_dimensions["C"].width = 18  # Customer_Number
        for i in range(4, len(result.columns) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 16

        ws.freeze_panes = "C2"

    elapsed = (time.time() - t_start) / 60
    print(f"\n✓ Done in {elapsed:.1f} min")
    print(f"  Rows: {len(result):,} at customer×material×country")
    print(f"  Saved → {out_xlsx}")
    print("\nTotals by target month (9LC):")
    print(f"  {'month':10s}" + "".join(f"{'n-'+str(l):>12s}" for l in LAGS) + f"{'actual':>12s}")
    for m in FORECAST_MONTHS:
        cells = "".join(
            f"{result[col_for(l, m)].sum():>12,.0f}" if col_for(l, m) in result else f"{'-':>12s}"
            for l in LAGS)
        a = f"Actual_{m.replace(' ', '_')}"
        print(f"  {m:10s}{cells}" + (f"{result[a].sum():>12,.0f}" if a in result else f"{'-':>12s}"))


if __name__ == "__main__":
    main()
