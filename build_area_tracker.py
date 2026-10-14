"""
build_area_tracker.py — one consolidated tracker for an area manager's markets.

EMEA ENTERP is run by customer, not by country: an area manager holds several
markets and, in some of them, a single named customer rather than the whole
country. So the scope is a list of (market, country, customer) rules rather
than one country filter, and every output sheet carries Market and Customer so
any of it can be filtered back out.

Modelled on the APAC IMC Australia tracker, with its two defects fixed:

  Units. That file carried 2026 and 2027 in 9L cases and 2028 in physical
  cases, which made 2028 read as a 44% collapse when it was growth. Everything
  here is 9L cases, taken from one source, and the reconciliation block states
  the unit once.

  Partial years. It hardcoded "2028 is Jan-Sep". Aera's horizon rolls forward
  every month, so that goes stale silently. Coverage is measured from the data
  and written into the caveats, so the file always describes itself.

Usage:  .venv/bin/python build_area_tracker.py [--area patrick]
"""
import argparse
import datetime
import os
import sys

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
sys.path.insert(0, os.path.join(DIR, "chatbot"))

import prework_queries as Q

P, D = Q.GCP_PROJECT, Q.DATASET
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# An area is a list of rules, because a manager's patch is not a country list:
# in UAE and Germany they hold one named customer while other customers in the
# same country belong to someone else.
AREAS = {
    "patrick": {
        "manager": "Patrick",
        "sub_segment": "EMEA ENTERP",
        "scope": [
            # market label        country in Aera    customer filter (None = whole market)
            ("United Arab Emirates", "Utd.Arab Emir.", "%INDU WORLD%"),
            ("Netherlands",          "Netherlands",    None),
            ("Germany",              "Germany",        "%XORTA%"),
        ],
    },
}

ACT_YEAR, FC_YEARS = 2025, (2026, 2027, 2028)


def _where(area):
    legs = []
    for _, country, cust in area["scope"]:
        leg = f"Country_Name = '{country}'"
        if cust:
            leg += f" AND UPPER(Customer_Name) LIKE '{cust.upper()}'"
        legs.append(f"({leg})")
    return f"Sub_Segments = '{area['sub_segment']}' AND (" + " OR ".join(legs) + ")"


def _market_case(area):
    whens = []
    for market, country, cust in area["scope"]:
        c = f"Country_Name = '{country}'"
        if cust:
            c += f" AND UPPER(Customer_Name) LIKE '{cust.upper()}'"
        whens.append(f"WHEN {c} THEN '{market}'")
    return "CASE " + " ".join(whens) + " ELSE Country_Name END"


def load(area):
    """One row per Market x Customer x Material, every month we hold."""
    client = Q._client()
    cols = {f.name for f in client.get_table(f"{P}.{D}.customer_analysis").schema}

    # Which (prefix, year, month) actually exist. Actuals win for a closed month,
    # AdjFC supplies the open ones; a month never takes both.
    series, have = {}, []
    for m in MONTHS:
        if f"Actual_{m}_{ACT_YEAR}" in cols:
            series[("ACT", ACT_YEAR, m)] = f"Actual_{m}_{ACT_YEAR}"
    for yr in FC_YEARS:
        for m in MONTHS:
            a, f = f"Actual_{m}_{yr}", f"AdjFC_{m}_{yr}"
            if a in cols:
                series[("FC", yr, m)] = a
                have.append((yr, m, "actual"))
            elif f in cols:
                series[("FC", yr, m)] = f
                have.append((yr, m, "forecast"))

    sel = ", ".join(f"ROUND(SUM(COALESCE({c},0)),4) AS `{k[0]}_{k[1]}_{k[2]}`"
                    for k, c in series.items())
    q = f"""
        SELECT {_market_case(area)} AS Market,
               Customer_Name, Country_Name, Material_Number,
               ANY_VALUE(Material_Long_Description)      AS Description,
               ANY_VALUE(Category_Grouper_Description_Z) AS Category,
               ANY_VALUE(Sub_Brand_Description)          AS Subbrand,
               ANY_VALUE(Volume)                         AS Size,
               ANY_VALUE(Brand_Family)                   AS Brand_Family,
               {sel}
        FROM `{P}.{D}.customer_analysis`
        WHERE {_where(area)}
        GROUP BY 1, 2, 3, 4
    """
    df = client.query(q).to_dataframe()
    for c in ["Customer_Name", "Description", "Category", "Subbrand", "Brand_Family"]:
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["Customer_Name"] = df["Customer_Name"].replace("", "(unnamed customer)")
    df["Size"] = pd.to_numeric(df["Size"], errors="coerce")
    return df, have


def month_cols(have):
    """Column labels in calendar order, '(A)' marking a month that is actual."""
    out = []
    for yr in FC_YEARS:
        for m in MONTHS:
            for (y, mm, kind) in have:
                if y == yr and mm == m:
                    out.append((f"FC_{yr}_{m}", f"{m} {yr}" + (" (A)" if kind == "actual" else "")))
    return out


def coverage(have):
    """Per year: which months carry data, so the caveats describe reality."""
    out = {}
    for yr in FC_YEARS:
        ms = [m for (y, m, _) in have if y == yr]
        ms = [m for m in MONTHS if m in ms]
        out[yr] = ms
    return out


def live_coverage(df, have):
    """Months that actually carry volume in this scope.

    A column existing is not the same as the horizon reaching it: Nov and Dec
    2028 are present in the table and empty everywhere, because Aera's horizon
    stops before them. Treating them as in-horizon is what made the Australia
    file call 2028 a nine-month year in a footnote that then went stale.
    """
    live = {}
    for yr in FC_YEARS:
        ms = []
        for m in MONTHS:
            c = f"FC_{yr}_{m}"
            if c in df.columns and df[c].abs().sum() > 0:
                ms.append(m)
        live[yr] = ms
    return live


def _fmt_size(v):
    if pd.isna(v):
        return ""
    return f"{v:g}"


def build(area_key="patrick"):
    area = AREAS[area_key]
    df, have = load(area)
    live = live_coverage(df, have)
    mcols = [(c, lab) for c, lab in month_cols(have)
             if c.split("_")[2] in live[int(c.split("_")[1])]]
    act_cols = [f"ACT_{ACT_YEAR}_{m}" for m in MONTHS if f"ACT_{ACT_YEAR}_{m}" in df.columns]

    df["Size_s"] = df["Size"].apply(_fmt_size)
    for yr in FC_YEARS:
        cs = [c for c, _ in mcols if c.startswith(f"FC_{yr}_")]
        df[f"TOT_{yr}"] = df[cs].sum(axis=1) if cs else 0.0
    df[f"TOT_{ACT_YEAR}"] = df[act_cols].sum(axis=1)
    return area, df, mcols, act_cols, live


# ── Excel ─────────────────────────────────────────────────────────────────────
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HDR   = PatternFill("solid", fgColor="1F3864")
SUB   = PatternFill("solid", fgColor="D9E2F3")
TOTAL = PatternFill("solid", fgColor="BDD7EE")
AREA  = PatternFill("solid", fgColor="8EAADB")
MANUAL= PatternFill("solid", fgColor="FCE4D6")
WHITE = Font(color="FFFFFF", bold=True)
BOLD  = Font(bold=True)
THIN  = Border(*[Side(style="thin", color="BFBFBF")] * 4)


def _sheet(wb, title, heading, blurb, headers, widths=None):
    ws = wb.create_sheet(title)
    ws["A1"] = heading; ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = blurb;   ws["A2"].font = Font(italic=True, size=9, color="595959")
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=j, value=h)
        c.fill, c.font, c.border = HDR, WHITE, THIN
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = ws.cell(row=5, column=1)
    ws.auto_filter.ref = f"A4:{get_column_letter(len(headers))}4"
    for j, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[4].height = 30
    return ws


VOL  = "#,##0.0"
CNT  = "#,##0"                        # a count of SKUs is not a volume
PCT  = "+#,##0.0%;-#,##0.0%"          # takes a FRACTION: 1.234 renders as +123.4%


def _write(ws, r, vals, fill=None, bold=False, numfmt=VOL, fmts=None):
    """fmts maps a 1-based column to its number format, overriding numfmt.

    Percent columns must be handed a fraction, never a number already scaled to
    100: Excel's percent format multiplies again, which is how +123.4% growth
    first printed as +12,336.1%.
    """
    for j, v in enumerate(vals, 1):
        c = ws.cell(row=r, column=j, value=v)
        c.border = THIN
        if fill: c.fill = fill
        if bold: c.font = BOLD
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            c.number_format = (fmts or {}).get(j, numfmt)
    return r + 1


def sheet_market(wb, df, mcols, act_cols, live):
    """Market x Category x Subbrand x Size, three metrics, four levels."""
    hdr = (["Market", "Category", "Subbrand", "L", "Metric", "Level"]
           + [f"{y} TOTAL" for y in FC_YEARS] + [f"Actuals {ACT_YEAR}"]
           + [lab for _, lab in mcols])
    ws = _sheet(wb, "MARKET",
                "MARKET TRACKER — volumes in 9L cases",
                "One block per Market / Category / Subbrand / Size. "
                "Adjustments is yours to fill in; the build never writes to it.",
                hdr, [22, 20, 30, 7, 14, 13] + [13] * 4 + [12] * len(mcols))
    keys = ["Market", "Category", "Subbrand", "Size_s"]
    r = 5

    def block(label, sub, level, fill=None, bold=False):
        nonlocal r
        for metric in ("Adj Forecast", f"Actuals {ACT_YEAR}", "Adjustments"):
            head = list(label) + [metric, level]
            if metric == "Adj Forecast":
                tot = [sub[f"TOT_{y}"].sum() for y in FC_YEARS] + [None]
                mv = [sub[c].sum() for c, _ in mcols]
            elif metric.startswith("Actuals"):
                tot = [None] * len(FC_YEARS) + [sub[f"TOT_{ACT_YEAR}"].sum()]
                # 2025 sits under the 2026 columns so the same month lines up
                mv = []
                for c, _ in mcols:
                    _, yr, m = c.split("_")
                    a = f"ACT_{ACT_YEAR}_{m}"
                    mv.append(sub[a].sum() if int(yr) == FC_YEARS[0] and a in sub else None)
            else:
                tot, mv = [None] * (len(FC_YEARS) + 1), [None] * len(mcols)
            r = _write(ws, r, head + tot + mv,
                       fill=MANUAL if metric == "Adjustments" else fill, bold=bold)

    for market in sorted(df.Market.unique()):
        dm = df[df.Market == market]
        for (cat, sb, sz), sub in dm.groupby(["Category", "Subbrand", "Size_s"], dropna=False):
            block([market, cat, sb, sz], sub, "Product")
        block([market, "", "", ""], dm, "MARKET TOTAL", TOTAL, True)
    block(["AREA TOTAL", "", "", ""], df, "AREA TOTAL", AREA, True)
    return ws


def sheet_by_sku(wb, df, mcols, act_cols, review=False):
    name = "BY SKU REVIEW" if review else "BY SKU"
    extra = ["Review", "Why"] if review else []
    hdr = (["Market", "Customer", "Category", "Subbrand", "L",
            "Material Number", "Description"] + extra
           + [f"{y} TOTAL" for y in FC_YEARS] + [f"Actuals {ACT_YEAR}"]
           + [lab for _, lab in mcols])
    ws = _sheet(wb, name,
                f"ADJ FORECAST BY SKU{' — REVIEW' if review else ''} — 9L cases",
                ("Same rows as BY SKU with two empty columns for your call and the reason."
                 if review else
                 "One row per Market / Customer / SKU. Sorted by 2026 volume."),
                hdr, [22, 30, 18, 26, 7, 16, 38] + [14] * len(extra)
                     + [13] * 4 + [12] * len(mcols))
    d = df.sort_values(["Market", "Customer_Name", f"TOT_{FC_YEARS[0]}"],
                       ascending=[True, True, False])
    r = 5
    for _, x in d.iterrows():
        vals = ([x.Market, x.Customer_Name, x.Category, x.Subbrand, x.Size_s,
                 str(x.Material_Number), x.Description] + [None] * len(extra)
                + [x[f"TOT_{y}"] for y in FC_YEARS] + [x[f"TOT_{ACT_YEAR}"]]
                + [x[c] for c, _ in mcols])
        r = _write(ws, r, vals)
    if review:
        for row in range(5, r):
            for col in (8, 9):
                ws.cell(row=row, column=col).fill = MANUAL
    return ws


def sheet_summary(wb, df, live):
    """The one page to read first: where the area stands, per market and customer.

    Every forward year is also shown on the months 2028 actually covers, because
    a 10-month year set beside a 12-month one invents a decline that is not there.
    """
    lf = live[FC_YEARS[-1]]                      # the shortest horizon, e.g. Jan-Oct
    hdr = ["Market", "Customer", "SKUs",
           f"Actuals {ACT_YEAR}", "2026 (act+fc)", "2027", "2028",
           "2026 vs 2025", "2027 vs 2026",
           f"2027 {lf[0]}-{lf[-1]}", f"2028 {lf[0]}-{lf[-1]}", "2028 vs 2027 like-for-like"]
    ws = _sheet(wb, "AREA SUMMARY", "AREA SUMMARY — 9L cases",
                f"2028 reaches only {lf[0]}-{lf[-1]} ({len(lf)} months), so the last three "
                f"columns compare 2027 and 2028 on those months alone.",
                hdr, [22, 34, 8] + [14] * 9)
    r = 5

    def row(market, cust, sub, fill=None, bold=False):
        nonlocal r
        a25, t26, t27, t28 = (sub[f"TOT_{ACT_YEAR}"].sum(), sub["TOT_2026"].sum(),
                              sub["TOT_2027"].sum(), sub["TOT_2028"].sum())
        p27 = sum(sub[f"FC_2027_{m}"].sum() for m in lf if f"FC_2027_{m}" in sub)
        p28 = sum(sub[f"FC_2028_{m}"].sum() for m in lf if f"FC_2028_{m}" in sub)
        pct = lambda n, d: (n / d - 1) if d else None        # fraction; PCT scales it
        vals = [market, cust, int(sub.Material_Number.nunique()), a25, t26, t27, t28,
                pct(t26, a25), pct(t27, t26), p27, p28, pct(p28, p27)]
        r = _write(ws, r, vals, fill=fill, bold=bold,
                   fmts={3: CNT, 8: PCT, 9: PCT, 12: PCT})

    for market in sorted(df.Market.unique()):
        dm = df[df.Market == market]
        for cust in sorted(dm.Customer_Name.unique()):
            row(market, cust, dm[dm.Customer_Name == cust])
        if dm.Customer_Name.nunique() > 1:
            row(market, "— market total —", dm, TOTAL, True)
    row("AREA TOTAL", "", df, AREA, True)
    return ws


def sheet_top(wb, df):
    hdr = ["Rank", "Market", "Category", "Subbrand", "L",
           "2026 (act+fc)", f"Actuals {ACT_YEAR}", "Delta vs 2025", "% vs 2025", "Status"]
    ws = _sheet(wb, "TOP PRODUCTS", "TOP PRODUCTS — ranked on 2026, 9L cases",
                "Grains carrying volume in 2026 or 2025. Status flags the movers.",
                hdr, [7, 22, 20, 30, 7, 14, 14, 14, 12, 18])
    g = (df.groupby(["Market", "Category", "Subbrand", "Size_s"], dropna=False)
           [[f"TOT_{FC_YEARS[0]}", f"TOT_{ACT_YEAR}"]].sum().reset_index())
    g = g[(g[f"TOT_{FC_YEARS[0]}"] != 0) | (g[f"TOT_{ACT_YEAR}"] != 0)]
    g["d"] = g[f"TOT_{FC_YEARS[0]}"] - g[f"TOT_{ACT_YEAR}"]
    g = g.sort_values(f"TOT_{FC_YEARS[0]}", ascending=False).reset_index(drop=True)
    r = 5
    for i, x in g.iterrows():
        base, cur = x[f"TOT_{ACT_YEAR}"], x[f"TOT_{FC_YEARS[0]}"]
        pc = (cur / base - 1) if base else None              # fraction; PCT scales it
        if base == 0 and cur:        status = "NEW — no 2025 base"
        elif cur == 0 and base:      status = "DISCONTINUED?"
        elif pc is not None and pc >= 0.25:  status = "growing >25%"
        elif pc is not None and pc <= -0.25: status = "declining >25%"
        else:                        status = ""
        r = _write(ws, r, [i + 1, x.Market, x.Category, x.Subbrand, x.Size_s,
                           cur, base, x.d, pc, status], fmts={1: CNT, 9: PCT})
    return ws


def sheet_instructions(wb, area, df, live, notes):
    ws = wb.create_sheet("INSTRUCTIONS", 0)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 120
    r = 1

    def head(t):
        nonlocal r
        c = ws.cell(row=r, column=1, value=t); c.font = Font(bold=True, size=12); r += 2

    def line(k, v, warn=False):
        nonlocal r
        a = ws.cell(row=r, column=1, value=k); a.font = BOLD
        b = ws.cell(row=r, column=2, value=v)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        if warn: b.fill = MANUAL
        r += 1

    today = datetime.date.today()
    head(f"AREA TRACKER — {area['manager']} · {area['sub_segment']}")
    line("Generated", today.strftime("%d %b %Y"))
    line("Unit", "9L cases throughout, every year and every sheet. One source, one unit.")
    line("Source", f"BigQuery {P}.{D}.customer_analysis, refreshed by the daily pipeline.")
    r += 1

    head("SCOPE — this is a patch, not a country list")
    for market, country, cust in area["scope"]:
        line(market, f"Aera country '{country}'" +
             (f", customer matching '{cust}' only" if cust else ", all customers"))
    r += 1

    head("READ THESE BEFORE QUOTING A NUMBER")
    for n in notes:
        line(n[0], n[1], warn=n[2])
    r += 1

    head("WHAT EACH YEAR IS")
    acts = [m for m in MONTHS if f"FC_2026_{m}" in df.columns
            and f"ACT_2026_{m}" not in df.columns]
    line("2026", "Actuals for the closed months and Adjusted Forecast for the rest — "
                 "the columns marked (A) are actual.")
    line("2027", f"Adjusted Forecast, {len(live[2027])} months.")
    line("2028", f"Adjusted Forecast, {len(live[2028])} months "
                 f"({live[2028][0]}–{live[2028][-1]}).")
    line(f"Actuals {ACT_YEAR}", "Full year, laid out under the 2026 month columns so the "
                                "same month lines up.")
    r += 1

    head("RECONCILIATION")
    for y in (ACT_YEAR,) + FC_YEARS:
        line(f"{'Actuals' if y == ACT_YEAR else 'Volume'} {y}", f"{df[f'TOT_{y}'].sum():,.1f}")
    line("Markets / customers / SKUs",
         f"{df.Market.nunique()} / {df.Customer_Name.nunique()} / {df.Material_Number.nunique()}")
    line("Rows", f"{len(df):,} at Market x Customer x SKU")
    return ws


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="patrick", choices=sorted(AREAS))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    area, df, mcols, act_cols, live = build(args.area)
    lf = live[FC_YEARS[-1]]

    notes = [
        ("2028 is a part year",
         f"Aera's horizon reaches {lf[-1]} 2028, so the 2028 total covers {len(lf)} months "
         f"({lf[0]}–{lf[-1]}), not twelve. Set beside a full 2027 it will look like a fall "
         f"that is not there. AREA SUMMARY carries a like-for-like pair on those months.", True),
        ("Units",
         "Every figure is 9L cases. The APAC IMC Australia tracker carried 2028 in physical "
         "cases while 2026 and 2027 were in 9L, which turned growth into an apparent 44% "
         "collapse. Nothing here mixes units.", True),
    ]
    # Customer successions: volume leaving one customer and arriving at another
    # inside the same market reads as collapse plus a new account unless flagged.
    for market in sorted(df.Market.unique()):
        dm = df[df.Market == market]
        gone = [c for c in dm.Customer_Name.unique()
                if dm[dm.Customer_Name == c][f"TOT_{ACT_YEAR}"].sum() > 0
                and dm[dm.Customer_Name == c]["TOT_2026"].sum() == 0]
        new = [c for c in dm.Customer_Name.unique()
               if dm[dm.Customer_Name == c][f"TOT_{ACT_YEAR}"].sum() == 0
               and dm[dm.Customer_Name == c]["TOT_2026"].sum() > 0]
        if gone and new:
            notes.append((f"{market} — customer handover",
                          f"{', '.join(gone)} carried {ACT_YEAR} volume and none in 2026; "
                          f"{', '.join(new)} is the reverse. Year on year by customer will "
                          f"read as a collapse plus a new account. Compare at market level.", True))

    wb = Workbook(); wb.remove(wb.active)
    sheet_summary(wb, df, live)
    sheet_market(wb, df, mcols, act_cols, live)
    sheet_by_sku(wb, df, mcols, act_cols, review=False)
    sheet_by_sku(wb, df, mcols, act_cols, review=True)
    sheet_top(wb, df)
    sheet_instructions(wb, area, df, live, notes)
    wb._sheets = [wb["INSTRUCTIONS"], wb["AREA SUMMARY"], wb["MARKET"],
                  wb["BY SKU"], wb["BY SKU REVIEW"], wb["TOP PRODUCTS"]]

    out = args.out or os.path.join(
        os.path.expanduser("~/Downloads"),
        f"Area_Tracker_{area['manager']}_{area['sub_segment'].replace(' ', '_')}"
        f"_{datetime.date.today():%Y-%m}.xlsx")
    wb.save(out)
    print(f"✓ {out}")
    for y in (ACT_YEAR,) + FC_YEARS:
        print(f"    {y}: {df[f'TOT_{y}'].sum():>12,.1f}")


if __name__ == "__main__":
    main()
