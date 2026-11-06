"""
Replay Aera's Forecast Adjustments screen and keep its summary rows.

The screen (process 65FDAE33) computes rows the reports do not expose as
measures — Revenue Rec. Actuals + Adj. FC, Previous Month Consensus Forecast,
Budget incl. Q1, LE — and it is what planners look at, so the DR deck is fed
from it. One handler=Start call opens a session (navigatorId + filter variable
ids), then handler=Next calls apply filters (Sub-Segment, Customer Country,
Category) and return the 17 summary rows for the current and next calendar
year. Closed months are the ones the screen shades grey.

Output (long format, one row per line x period):
  fetch_date, Sub_Segments, Country_Name, Category, Line, Period, Month_Year, Value
  Period = JAN..DECE | YTD | YTG | CURRENT_YEAR_TOTAL | <MON>_NEXT_YEAR | NEXT_YEAR_TOTAL
  Month_Year = 'Sep 2026' for month periods, '' otherwise.
  Country_Name / Category = '' when not filtered (totals).
  A '__META__' line carries CLOSED_THROUGH (month number) and CURRENT_YEAR.

Usage:
  python3 fetch_fa_screen.py --probe                       # filter semantics check, no save
  python3 fetch_fa_screen.py --scope "APAC IMC" Australia  # one market (+ its categories)
  python3 fetch_fa_screen.py                               # everything with volume → fa_screen_rows.parquet
"""
import argparse
import base64
import datetime
import os
import re
import sys
import time
from typing import Optional

import pandas as pd
import requests

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from aera_auth import ensure_token

BASE       = "https://becleproximo.aeratechnology.com"
PROCESS_ID = "65FDAE33_AD16_45D5_B0DB_EB7ACC5B9201"      # Forecast Adjustments
OUTPUT     = os.path.join(DIR, "fa_screen_rows.parquet")
MONTHS     = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DECE"]
MON_ABBR   = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LINE_RE   = re.compile(r'"fieldName":"LINE_DESC","value":"((?:[^"\\]|\\.)*)"')
FIELD_RE  = re.compile(r'"fieldName":"([A-Z_]+)","value":"([^"]*)"')
SHADE_RE  = re.compile(r'"backgroundColor":\{"value":"#E6E6E6"\}\},"fieldId":\d+,"fieldName":"([A-Z]+)"')
LINES_KEEP = {
    "Stat Forecast**", "3PD Forecast*", "Source Forecast*", 'Adj. Forecast ("FC")*',
    "Revenue Rec. Actuals + Adj. FC", "Previous Month Consensus Forecast*", "Last Year Actuals",
    "Actual Sales Orders", "Budget", "LE",
}


def _b64(v: str) -> str:
    return "[" + base64.b64encode(v.encode()).decode() + "]"


class FAScreen:
    """One Aera session on the Forecast Adjustments screen."""

    def __init__(self):
        self.token = None
        self._connect()

    def _connect(self):
        """(Re)build the HTTP session from a token with at least 10 min left
        (ensure_token logs in again when needed) and open a screen session."""
        tok = ensure_token(min_seconds=600)
        self.token = tok["access_token"]
        self.s = requests.Session()
        for k, v in {"JSESSIONID": tok.get("jsessionid", ""), "accessToken": tok["access_token"],
                     "token": tok["access_token"]}.items():
            self.s.cookies.set(k, v)
        if tok.get("lb_instance_id"):
            self.s.cookies.set("lb-instance-id", tok["lb_instance_id"])
            self.s.headers["lb-instance-id"] = tok["lb_instance_id"]
        self.s.headers.update({"Authorization": tok["access_token"], "Origin": BASE,
                               "Referer": f"{BASE}/ispring/Default?handler=Start&processID={PROCESS_ID}"})
        self.start()

    def _keep_alive(self):
        """Before a call: if the cached token is about to expire, log in again
        and open a fresh screen session (filters are re-sent on every call)."""
        tok = ensure_token(min_seconds=600)
        if tok["access_token"] != self.token:
            self._connect()

    def start(self):
        r = self.s.get(f"{BASE}/ispring/Default",
                       params={"handler": "Start", "processID": PROCESS_ID, "__requestjson__": "true"},
                       timeout=180)
        r.raise_for_status()
        txt = r.text
        nav = re.search(r'navigatorId":"([A-F0-9_]+)"', txt)
        if not nav:
            raise RuntimeError("navigatorId not found — session/token problem")
        self.navigator = nav.group(1)
        nodes = sorted(set(re.findall(r'"nodeId":"([A-F0-9_]+)"', txt)))
        if not nodes:
            raise RuntimeError("nodeId not found in Start response")
        self.node = nodes[0]
        self.vars = {}
        for label in ("Sub-Segment", "Customer Country", "Category", "Business Segment"):
            m = re.search(r'"targetVar":\{"value":\[[^\]]*\],"varId":(\d+),"type":"String"\},'
                          r'"label":\{"value":"' + re.escape(label) + r'"\}', txt)
            if m:
                self.vars[label] = m.group(1)
        self.iso_by_country = {name: iso for iso, name in re.findall(
            r'"fieldName":"customer_country","value":"([^"]+)","type":"String"\},'
            r'\{"fieldId":\d+,"fieldName":"country_name","value":"([^"]+)"', txt)}
        self.categories = sorted(set(re.findall(r'"fieldName":"category","value":"([^"]+)"', txt)))
        self.sub_segments = sorted(set(re.findall(r'"fieldName":"sub_segment","value":"([^"]+)"', txt)))
        self.portfolio = parse_rows(txt)

    def fetch(self, sub_segment: Optional[str] = None, country: Optional[str] = None,
              category: Optional[str] = None, retries: int = 3) -> tuple[dict, list]:
        """Rows for the filter combination. Filters are sticky within a session,
        so every filter variable is sent on every call; an empty string clears
        it (an empty list "[]" or an encoded "" makes the screen return zeros)."""
        fields = {
            self.vars["Sub-Segment"]:      _b64(sub_segment) if sub_segment else "",
            self.vars["Customer Country"]: _b64(self.iso_by_country[country]) if country else "",
            self.vars["Category"]:         _b64(category) if category else "",
        }
        for attempt in range(retries):
            try:
                self._keep_alive()
                url = (f"{BASE}/ispring/Default?handler=Next&nodeID={self.node}"
                       f"&navigatorID={self.navigator}&__requestjson__=true")
                r = self.s.post(url, files={k: (None, v) for k, v in fields.items()}, timeout=180)
                if r.status_code in (401, 403):
                    raise RuntimeError(f"status {r.status_code}")
                r.raise_for_status()
                rows, closed = parse_rows(r.text)
                if rows:
                    return rows, closed
                raise RuntimeError("no rows in response")
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(5 * (attempt + 1))
                self._connect()                    # fresh login + screen session, then retry


def parse_rows(txt: str) -> tuple[dict, list]:
    """{line: {field: value}}, [closed month fields] from a screen response."""
    out: dict = {}
    for m in LINE_RE.finditer(txt):
        line = m.group(1).replace('\\"', '"')
        seg = txt[m.end(): m.end() + 8000]
        row = {}
        for f, v in FIELD_RE.findall(seg):
            if f == "LINE_DESC":
                break
            try:
                row[f] = float(v.replace(",", ""))
            except ValueError:
                row[f] = v
        if line in LINES_KEEP:
            out[line] = row
    closed = sorted(set(SHADE_RE.findall(txt)), key=lambda f: MONTHS.index(f) if f in MONTHS else 99)
    return out, [c for c in closed if c in MONTHS]


def rows_to_frame(rows: dict, closed: list, sub_segment: str, country: str, category: str,
                  fetch_date: datetime.date, year: int) -> pd.DataFrame:
    recs = []
    for line, fields in rows.items():
        for f, v in fields.items():
            if not isinstance(v, float):
                continue
            if f in MONTHS:
                my = f"{MON_ABBR[MONTHS.index(f)]} {year}"
            elif f.endswith("_NEXT_YEAR") and f[:-10] in MONTHS:
                my = f"{MON_ABBR[MONTHS.index(f[:-10])]} {year + 1}"
            else:
                my = ""
            recs.append((fetch_date, sub_segment, country, category, line, f, my, v))
    closed_through = max((MONTHS.index(c) + 1 for c in closed), default=0)
    recs.append((fetch_date, sub_segment, country, category, "__META__", "CLOSED_THROUGH", "", float(closed_through)))
    recs.append((fetch_date, sub_segment, country, category, "__META__", "CURRENT_YEAR", "", float(year)))
    return pd.DataFrame(recs, columns=["fetch_date", "Sub_Segments", "Country_Name", "Category",
                                       "Line", "Period", "Month_Year", "Value"])


def combos_with_volume() -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[str]]]:
    """(sub_segment, country) pairs and their categories that carry any volume,
    read from the AdjFC parquet the pipeline fetched just before this step —
    so the screen is not called for empty cells. (No BigQuery dependency: the
    client call stalled for 35 minutes in the first background run.)"""
    path = os.path.join(DIR, "adjfc_nz.parquet")
    df = pd.read_parquet(path, columns=["Sub-Segments", "Country Name", "Category Grouper Description (Z)",
                                        "Adjusted FC", "Actuals"])
    df.columns = ["sub", "country", "cat", "adjfc", "act"]
    df["vol"] = pd.to_numeric(df["adjfc"], errors="coerce").abs().fillna(0) + \
                pd.to_numeric(df["act"], errors="coerce").abs().fillna(0)
    g = df.groupby(["sub", "country", "cat"], as_index=False)["vol"].sum()
    g = g[(g["vol"] > 0) & ~g["sub"].isin(["", "Not Set"]) & ~g["country"].isin(["", "Not Set"])]
    pairs = sorted({(r.sub, r.country) for r in g.itertuples()})
    cats: dict = {}
    for r in g.itertuples():
        cats.setdefault((r.sub, r.country), []).append(r.cat)
    print(f"  {len(pairs)} markets, {len(g)} market x category cells with volume (from adjfc_nz.parquet)")
    return pairs, cats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="check filter semantics on Australia, no save")
    ap.add_argument("--scope", nargs=2, metavar=("SUB_SEGMENT", "COUNTRY"), help="one market only")
    ap.add_argument("--no-categories", action="store_true")
    ap.add_argument("--out", default=OUTPUT)
    args = ap.parse_args()

    fa = FAScreen()
    year = datetime.date.today().year
    today = datetime.date.today()
    print(f"session {fa.navigator} | node {fa.node} | {len(fa.iso_by_country)} countries | "
          f"{len(fa.categories)} categories | sub-segments {fa.sub_segments}")

    if args.probe:
        def cy(rows): return rows['Adj. Forecast ("FC")*'].get("CURRENT_YEAR_TOTAL")
        r1, c1 = fa.fetch("APAC IMC", "Australia");           print("AU total        :", cy(r1), "closed", c1)
        r2, _  = fa.fetch("APAC IMC", "Australia", "RTDS");   print("AU RTDS         :", cy(r2))
        r3, _  = fa.fetch("APAC IMC", "Australia");           print("AU after RTDS   :", cy(r3), "(must equal AU total)")
        r4, _  = fa.fetch("EMEA DEVELOP");                    print("EMEA DEVELOP    :", cy(r4))
        r5, _  = fa.fetch("EMEA DEVELOP", "Poland");          print("EMEA DEV Poland :", cy(r5))
        print("portfolio PMCF CY:", fa.portfolio[0]["Previous Month Consensus Forecast*"].get("CURRENT_YEAR_TOTAL"))
        return

    frames = []
    t0 = time.time()
    if args.scope:
        sub, country = args.scope
        pairs, cats = [(sub, country)], {(sub, country): fa.categories}
        subs = [sub]
    else:
        pairs, cats = combos_with_volume()
        subs = sorted({p[0] for p in pairs})
    # sub-segment totals and their category split
    for sub in subs:
        if sub in ("", "Not Set"):
            continue
        rows, closed = fa.fetch(sub)
        frames.append(rows_to_frame(rows, closed, sub, "", "", today, year))
        if not args.no_categories:
            for cat in fa.categories:
                if cat == "Not Set":
                    continue
                rows, closed = fa.fetch(sub, None, cat)
                frames.append(rows_to_frame(rows, closed, sub, "", cat, today, year))
        print(f"  {sub:14s} totals + categories done ({time.time() - t0:.0f}s)")
    # markets and their category split
    for i, (sub, country) in enumerate(pairs, 1):
        if country not in fa.iso_by_country:
            print(f"  ! {country} not in the screen's country list — skipped")
            continue
        rows, closed = fa.fetch(sub, country)
        frames.append(rows_to_frame(rows, closed, sub, country, "", today, year))
        if not args.no_categories:
            for cat in cats.get((sub, country), []):
                if cat in ("", "Not Set") or cat not in fa.categories:
                    continue
                rows, closed = fa.fetch(sub, country, cat)
                frames.append(rows_to_frame(rows, closed, sub, country, cat, today, year))
        print(f"  {i}/{len(pairs)} {sub} / {country} ({len(cats.get((sub, country), []))} categories) "
              f"({time.time() - t0:.0f}s)")
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(args.out, index=False)
    print(f"✓ {len(df):,} rows → {args.out} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
