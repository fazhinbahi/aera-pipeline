"""
fetch_consensus_snapshots.py — Consensus Forecast snapshots by approval date, from Aera.

The "Consensus Forecast EA" subject area (fact F22F579C) stores every approved
consensus cycle: for each Approval Date it holds the Source, Statistical and
Consensus forecast per customer x material x forecast month. This is the data
behind Enrique's datos_reducidos.csv (FC rows) and the Forecast Accuracy
dashboards; his export came from the Aera report "Consensus Forecast Report -
EMEA & APAC". This script replays the shared report of the same family
("… - Steph", id 158111DE) through ExecuteBIObjectData, one approval date at a
time, and aggregates the customer-level rows to

    approval_date x forecast_month x sub_segment x customer_country x material

keeping Source FC, Statistical FC, Consensus FC, Approved Delta, Consensus Phy
Cases and Actual Order Qty (9L cases, except Phy Cases).

Verified 6 Nov 2026 against datos_reducidos (2).csv: Consensus and Statistical
totals per approval / group / month window agree to the cent (approval 09 Oct
2026, IMC, Oct-26..Jan-27: 561,687.91 / 555,216.99).

Aera constraints found while building this:
  * the `row` layout of ExecuteBIObjectData is locked to the saved report —
    any change returns HTTP 500; measures and filters are free. The grain is
    therefore reduced here, not server-side.
  * 2,000 rows per call whatever `plimit` says.
  * one approval is ~290k customer rows over the full horizon; by default the
    fetch keeps forecast months from the approval month -1 to +HORIZON (12).
  * approval dates are not listable (row layout locked), so new ones are
    found by probing one call per calendar day after the last known approval.

Usage:
  python3 fetch_consensus_snapshots.py                  # daily: find approvals after the last known one, fetch them,
                                                        #        re-fetch approvals younger than --refresh-days
  python3 fetch_consensus_snapshots.py --backfill       # every approval in consensus_calendar.csv missing from the parquet
  python3 fetch_consensus_snapshots.py --approval 2026-10-09 --approval 2026-09-14
  python3 fetch_consensus_snapshots.py --scan 2025-01-01 2025-12-31   # probe every day of a range for approvals
  python3 fetch_consensus_snapshots.py --horizon 24     # longer forecast horizon per approval

Output: consensus_snapshots.parquet (rows replaced per approval date), then
        load_to_bq.py --table consensus → BigQuery consensus_snapshots (+ views).
"""
import argparse
import datetime as dt
import os
import sys
import time

import pandas as pd
import requests

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from aera_auth import ensure_token, login as _aera_login  # noqa: E402
from consensus_cycle import load_calendar, update_calendar  # noqa: E402

BASE_URL = "https://becleproximo.aeratechnology.com"
DATA_URL = (f"{BASE_URL}/ispring/awc?v=3&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7"
            "&ServiceName=ExecuteBIObjectData")
OUTPUT_PARQUET = os.path.join(DIR, "consensus_snapshots.parquet")

PAGE_SIZE = 2000          # server cap
HORIZON_MONTHS = 12       # forecast months after the approval month kept by default
REFRESH_DAYS = 21         # approvals younger than this are re-fetched daily (Aera keeps adding grains)
MAX_PROBE_DAYS = 120      # safety cap on the daily probe loop

# ── Report replayed (shared, owned by Stephanie Jensen; layout locked) ──────────
REPORT = {
    "sheetid": "E8D86297-C7BF-485F-BDBD-7B34B2AE8672",
    "bioid":   "158111DE_A303_4103_82B0_BB9E1B7127B1",   # Consensus Forecast Report - EMEA & APAC - Steph
    "fid":     "F22F579C_43F9_4899_831E_D86D20051229",   # Consensus Forecast EA
    # exact row layout of the saved report (912 chars, copied from the browser request; the sheet
    # appends Customer Name, Forecast Date, Approval Date and Category to the eight row dims)
    "row": 'EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_728D5675-9D28-4868-B20E-73E32F4FFE77|,28A39CB6-90E0-483C-B214-831A96B8566A_0C6DEBBD-03D8-40E1-8A32-780C8541AEBB|,FA492511-1A35-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9|,B8D541CE-1EDB-11ED-A548-0A617A24E20D_728D5675-9D28-4868-B20E-73E32F4FFE77|,0F80F189-0F93-4274-8B84-2F1DE0A612FB_E0CE764D-31EF-4781-8FDB-80282351ABE9|,126FD4B7-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9|,04E2EDB1-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9|,09CF6C61-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9|,E57FE042-E779-46F1-B6B0-B774361BA676_6828F498-A3F7-4FEF-B7E5-48623AA55CF6|Customer Name,EE629938-1EDF-11ED-A548-0A617A24E20D_E819D0F7-A4CF-457E-9977-98CE4F146B89|,EE629938-1EDF-11ED-A548-0A617A24E20D_3CD5CA67-E920-4697-A283-9B37DD6492A4|,DC111F6A-6C39-4684-891B-3096FECB0F20_E0CE764D-31EF-4781-8FDB-80282351ABE9|',
}

# Dimension ids as they come back in metaData.fields (the sheet adds four columns to the row list)
DIMS = {
    "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_728D5675-9D28-4868-B20E-73E32F4FFE77": "sub_segment",
    "28A39CB6-90E0-483C-B214-831A96B8566A_0C6DEBBD-03D8-40E1-8A32-780C8541AEBB": "customer_country",
    "FA492511-1A35-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9": "ext_material_group",
    "B8D541CE-1EDB-11ED-A548-0A617A24E20D_728D5675-9D28-4868-B20E-73E32F4FFE77": "customer_number",
    "0F80F189-0F93-4274-8B84-2F1DE0A612FB_E0CE764D-31EF-4781-8FDB-80282351ABE9": "sub_brand",
    "126FD4B7-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9": "volume_l",
    "04E2EDB1-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9": "material",
    "09CF6C61-1A36-11ED-A548-0A617A24E20D_E0CE764D-31EF-4781-8FDB-80282351ABE9": "material_desc",
    "E57FE042-E779-46F1-B6B0-B774361BA676_6828F498-A3F7-4FEF-B7E5-48623AA55CF6": "customer_name",
    "EE629938-1EDF-11ED-A548-0A617A24E20D_E819D0F7-A4CF-457E-9977-98CE4F146B89": "forecast_date",
    "EE629938-1EDF-11ED-A548-0A617A24E20D_3CD5CA67-E920-4697-A283-9B37DD6492A4": "approval_date",
    "DC111F6A-6C39-4684-891B-3096FECB0F20_E0CE764D-31EF-4781-8FDB-80282351ABE9": "category",
}
MEASURES = [  # (id, column) — order defines the request
    ("5AF588C0-D4EF-46AB-8B8E-A85221B32751|SUM|||||",        "source_fc"),
    ("26F54639-5C44-4BA5-BAE3-C4F38C084C98|SUM|||||",        "stat_fc"),
    ("2E52EEB9-A19F-4A45-B684-7854E8715361|SUM|||||",        "consensus_fc"),
    ("F68F3371-CD42-43C0-BC2E-04CC560284EC|SUM|||||",        "approved_delta"),
    ("0646CAA0_F9B3_4EE4_820B_DCDE318907A0|EXPRESSION|||||", "consensus_phy_cases"),
    ("A941B8E9-EB55-430F-93B7-274AB3FF8A5E|SUM|||||",        "actual_order_qty"),
]
MEASURE_COLS = [c for _, c in MEASURES]
F_APPROVAL_DATE = "EE629938-1EDF-11ED-A548-0A617A24E20D_3CD5CA67-E920-4697-A283-9B37DD6492A4"
F_FORECAST_DATE = "EE629938-1EDF-11ED-A548-0A617A24E20D_E819D0F7-A4CF-457E-9977-98CE4F146B89"

GRAIN = ["approval_date", "forecast_month", "sub_segment", "customer_country", "material"]
ATTRS = ["ext_material_group", "sub_brand", "volume_l", "category", "material_desc"]


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.tok = ensure_token(min_seconds=300)

    def _cookies(self):
        c = {"JSESSIONID": self.tok["jsessionid"], "accessToken": self.tok["access_token"],
             "token": self.tok["access_token"]}
        if self.tok.get("lb_instance_id"):
            c["lb-instance-id"] = self.tok["lb_instance_id"]
        return c

    def _headers(self):
        h = {"Authorization": self.tok["access_token"], "Content-Type": "application/x-www-form-urlencoded",
             "Origin": BASE_URL, "Referer": BASE_URL}
        if self.tok.get("lb_instance_id"):
            h["lb-instance-id"] = self.tok["lb_instance_id"]
        return h

    def post(self, payload: dict, retries: int = 8) -> dict:
        for attempt in range(retries):
            try:
                if self.tok.get("expires_at", 0) - time.time() < 120:
                    self.tok = ensure_token(min_seconds=300)
                resp = requests.post(DATA_URL, data=payload, headers=self._headers(),
                                     cookies=self._cookies(), timeout=300)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError:
                if resp.status_code in (401, 403) and attempt < retries - 1:
                    print(f"    ⚠ {resp.status_code} — re-logging in (attempt {attempt + 1})…")
                    time.sleep(10)
                    self.tok = _aera_login()
                    continue
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    wait = 10 * (2 ** attempt)
                    print(f"    ⚠ {resp.status_code} — retry in {wait}s…")
                    time.sleep(wait)
                    continue
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt < retries - 1:
                    time.sleep(15)
                    continue
                raise


def _payload(flt: str, page: int, limit: int) -> dict:
    return {
        "sheetid": REPORT["sheetid"], "bioid": REPORT["bioid"], "fid": REPORT["fid"],
        "row": REPORT["row"], "col": "", "mea": ",".join(m for m, _ in MEASURES),
        "filter": flt, "pot": "0", "sort": "", "dir": "", "rptaggfn": "total", "totcolp": "1",
        "in_val": "[]", "source": "report", "uom": "", "currency": "", "rate": "", "currencyDate": "T",
        "page": str(page), "pstart": str((page - 1) * limit), "plimit": str(limit),
        "requestID": "PYTHON-CONSENSUS-SNAPSHOTS",
    }


def _aera_date(d: dt.date) -> str:
    return d.strftime("%d %b %Y")          # '09 Oct 2026' — the format the fact uses


def _month_bounds(approval: dt.date, horizon: int) -> tuple[dt.date, dt.date]:
    """Forecast months kept: approval month - 1 … approval month + horizon."""
    first = approval.replace(day=1)
    lo = (first - dt.timedelta(days=1)).replace(day=1)
    y, m = first.year, first.month + horizon + 1
    while m > 12:
        m -= 12
        y += 1
    hi = dt.date(y, m, 1)                   # exclusive
    return lo, hi


def approval_filter(approval: dt.date, horizon: int | None) -> str:
    flt = f"{F_APPROVAL_DATE}~=|{_aera_date(approval)}~EN"
    if horizon is not None:
        lo, hi = _month_bounds(approval, horizon)
        flt += (f"^{F_FORECAST_DATE}~d>|{_aera_date(lo - dt.timedelta(days=1))}~EN"
                f"^{F_FORECAST_DATE}~d<|{_aera_date(hi)}~EN")
    return flt


# ── Fetch ────────────────────────────────────────────────────────────────────

def probe_approval(s: Session, day: dt.date) -> int:
    """Rows stored for an approval on `day` (0 = no approval that day)."""
    j = s.post(_payload(approval_filter(day, None), 1, 1))
    return int(j.get("totalRows") or 0)


def fetch_approval(s: Session, approval: dt.date, horizon: int = HORIZON_MONTHS) -> pd.DataFrame:
    flt = approval_filter(approval, horizon)
    t0 = time.time()
    first = s.post(_payload(flt, 1, PAGE_SIZE))
    total = int(first.get("totalRows") or 0)
    fields = [f["name"] for f in first["metaData"]["fields"]]
    cols = []
    for f in fields:
        if f in DIMS:
            cols.append(DIMS[f])
        else:
            mid = f.split("|")[0]
            cols.append(next((c for m, c in MEASURES if m.split("|")[0] == mid), f))
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  {approval}: {total:,} customer rows → {pages} pages")
    data = list(first["data"])
    for p in range(2, pages + 1):
        if p % 10 == 0 or p == pages:
            print(f"    page {p}/{pages}  ({len(data):,} rows, {time.time() - t0:.0f}s)")
        time.sleep(0.3)
        data.extend(s.post(_payload(flt, p, PAGE_SIZE))["data"])
    raw = pd.DataFrame(data, columns=cols)
    if raw.empty:
        return raw
    return reduce_rows(raw, approval)


def reduce_rows(raw: pd.DataFrame, approval: dt.date) -> pd.DataFrame:
    df = raw.copy()
    df["approval_date"] = approval
    df["forecast_month"] = pd.to_datetime(df["forecast_date"], format="%d %b %Y").dt.to_period("M").dt.to_timestamp().dt.date
    df["volume_l"] = pd.to_numeric(df["volume_l"], errors="coerce")
    for c in MEASURE_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ("sub_segment", "customer_country", "material", "ext_material_group", "sub_brand",
              "category", "material_desc"):
        df[c] = df[c].fillna("").astype(str).str.strip()
    # all-zero customer rows carry nothing
    df = df[(df[MEASURE_COLS].abs().sum(axis=1) > 0)]
    agg = {c: "sum" for c in MEASURE_COLS}
    agg.update({c: "first" for c in ATTRS})
    agg["customer_number"] = "nunique"
    out = df.groupby(GRAIN, as_index=False, sort=False).agg(agg).rename(columns={"customer_number": "n_customers"})
    out["fetch_date"] = dt.date.today()
    return out[GRAIN + ATTRS + MEASURE_COLS + ["n_customers", "fetch_date"]]


# ── Store ────────────────────────────────────────────────────────────────────

def load_existing() -> pd.DataFrame:
    if os.path.exists(OUTPUT_PARQUET):
        df = pd.read_parquet(OUTPUT_PARQUET)
        df["approval_date"] = pd.to_datetime(df["approval_date"]).dt.date
        return df
    return pd.DataFrame()


def save(existing: pd.DataFrame, fresh: list[pd.DataFrame]):
    new = pd.concat([f for f in fresh if f is not None and not f.empty], ignore_index=True) if fresh else pd.DataFrame()
    if new.empty:
        print("  nothing new to save")
        return existing
    done = set(new["approval_date"].unique())
    keep = existing[~existing["approval_date"].isin(done)] if not existing.empty else existing
    out = pd.concat([keep, new], ignore_index=True)
    out = out.sort_values(["approval_date", "sub_segment", "customer_country", "material", "forecast_month"])
    out.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"  ✓ saved {len(out):,} rows, {out['approval_date'].nunique()} approvals → {os.path.basename(OUTPUT_PARQUET)}")
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fetch consensus forecast snapshots by approval date from Aera")
    ap.add_argument("--approval", action="append", help="YYYY-MM-DD, repeatable")
    ap.add_argument("--backfill", action="store_true", help="fetch every calendar approval missing from the parquet")
    ap.add_argument("--scan", nargs=2, metavar=("FROM", "TO"), help="probe each day of the range for approvals")
    ap.add_argument("--horizon", type=int, default=HORIZON_MONTHS, help="forecast months after the approval month to keep")
    ap.add_argument("--refresh-days", type=int, default=REFRESH_DAYS, help="re-fetch approvals younger than this (daily mode)")
    ap.add_argument("--no-probe", action="store_true", help="daily mode: skip probing for new approvals")
    args = ap.parse_args()

    s = Session()
    existing = load_existing()
    have = set(existing["approval_date"].unique()) if not existing.empty else set()
    cal = load_calendar()
    known = sorted(set(cal["approval_date"]) | have)
    print(f"Known approvals: {len(known)} (calendar) · in parquet: {len(have)}")

    todo: list[dt.date] = []
    today = dt.date.today()

    if args.approval:
        todo += [dt.date.fromisoformat(a) for a in args.approval]
    if args.backfill:
        todo += [a for a in known if a not in have]
    if args.scan:
        d, end = dt.date.fromisoformat(args.scan[0]), dt.date.fromisoformat(args.scan[1])
        print(f"Scanning {d} → {end} for approvals…")
        while d <= end:
            n = probe_approval(s, d)
            if n:
                print(f"  approval found: {d} ({n:,} rows)")
                if d not in have:
                    todo.append(d)
                known = sorted(set(known) | {d})
            d += dt.timedelta(days=1)
    if not (args.approval or args.backfill or args.scan):
        # daily mode
        if not args.no_probe:
            start = (max(known) + dt.timedelta(days=1)) if known else today - dt.timedelta(days=60)
            d = max(start, today - dt.timedelta(days=MAX_PROBE_DAYS))
            print(f"Probing {d} → {today} for new approvals…")
            while d <= today:
                n = probe_approval(s, d)
                if n:
                    print(f"  new approval: {d} ({n:,} rows)")
                    todo.append(d)
                    known = sorted(set(known) | {d})
                d += dt.timedelta(days=1)
        todo += [a for a in have if (today - a).days <= args.refresh_days]

    todo = sorted(set(todo))
    if not todo:
        print("Nothing to fetch.")
    else:
        print(f"Fetching {len(todo)} approval(s): {', '.join(str(a) for a in todo)}")
    fresh = []
    for a in todo:
        try:
            df = fetch_approval(s, a, args.horizon)
            print(f"    → {len(df):,} rows at material grain · consensus {df['consensus_fc'].sum():,.0f}")
            fresh.append(df)
            existing = save(existing, [df])     # checkpoint after every approval
        except Exception as e:                   # keep going; the next run retries
            print(f"  ✗ {a}: {e}")

    cal = update_calendar(cal, known)
    print(f"Calendar: {len(cal)} rows, {cal['approval_date'].nunique()} approvals")


if __name__ == "__main__":
    main()
