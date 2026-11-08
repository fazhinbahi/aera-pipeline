"""
Data layer for the Demand Review (DR) deck builder.

Builds the three tables that the monthly DR deck carries per market, from the
BigQuery tables the daily pipeline refreshes:

  1. Unconstrained Demand Plan   adjfc_raw, order_history_raw, customer_analysis,
                                 stat_3pd_forecast, adjfc_cycle_snapshots
  2. NPD / NMI tracking          order_history_raw, adjfc_raw
  3. N-3 drivers (top 10)        lag1_data via prework_queries.fetch_accuracy,
                                 same maths as the pre-work PDF section 3.1

Everything is in 9L cases. The cycle follows the live calendar: the last closed
month is the month before the current one, exactly like the pre-work.
"""
import datetime
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from google.cloud import bigquery

from prework_queries import GCP_PROJECT, DATASET, _client, fetch_accuracy, lag_months
from prework_pdf import _chain, _attach_grain, _canon_subbrand_map, _upc_key

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _t(name: str) -> str:
    return f"`{GCP_PROJECT}.{DATASET}.{name}`"


def _my(m: int, y: int) -> str:
    """(month, year) -> Aera 'Mon YYYY' label."""
    return f"{MONTHS[m - 1]} {y}"


# ── Cycle context ─────────────────────────────────────────────────────────────
@dataclass
class Cycle:
    today: datetime.date
    first_open: datetime.date          # first day of the current (open) month
    last_closed: datetime.date         # first day of the last closed month
    year: int                          # plan year shown in the first block
    next_year: int

    @classmethod
    def from_today(cls, today: Optional[datetime.date] = None) -> "Cycle":
        today = today or datetime.date.today()
        first_open = today.replace(day=1)
        last_closed = (first_open - datetime.timedelta(days=1)).replace(day=1)
        return cls(today, first_open, last_closed, first_open.year, first_open.year + 1)

    @classmethod
    def from_aera(cls, closed_through: int, year: int) -> "Cycle":
        """Aera's own cycle: the screen shades closed months grey; the first
        unshaded month is the open one (October while the Oct RunStat is live,
        even in early November)."""
        today = datetime.date.today()
        if closed_through >= 12:
            first_open = datetime.date(year + 1, 1, 1)
        else:
            first_open = datetime.date(year, closed_through + 1, 1)
        last_closed = (first_open - datetime.timedelta(days=1)).replace(day=1)
        return cls(today, first_open, last_closed, year, year + 1)

    def is_closed(self, m: int, y: int) -> bool:
        return datetime.date(y, m, 1) < self.first_open

    @property
    def closed_months(self) -> list[int]:
        return [m for m in range(1, 13) if self.is_closed(m, self.year)]

    @property
    def open_months(self) -> list[int]:
        return [m for m in range(1, 13) if not self.is_closed(m, self.year)]

    @property
    def label(self) -> str:
        """Deck month label, e.g. 'November, 2026'."""
        return self.first_open.strftime("%B, %Y")

    @property
    def last_closed_label(self) -> str:
        return self.last_closed.strftime("%b %Y")


@dataclass
class Period:
    key: str
    label: str
    months: list[tuple[int, int]]      # [(month, year), ...]
    kind: str                          # 'm' | 'q' | 'ytd' | 'ytg' | 'fy'


def period_columns(cycle: Cycle) -> list[Period]:
    """Column layout of the demand-plan table.

    Current year: fully closed quarters collapse to one column, the quarter
    holding the first open month is expanded into its months plus the quarter,
    later quarters show as totals; then YTD / YTG / FY. Next year: the first two
    quarters expanded, Q3 and Q4 as totals, then FY — the layout of the deck.
    """
    y, ny = cycle.year, cycle.next_year
    cols: list[Period] = []
    yy = str(y)[2:]
    for q in range(1, 5):
        qm = [3 * (q - 1) + i for i in (1, 2, 3)]
        all_closed = all(cycle.is_closed(m, y) for m in qm)
        has_open_edge = any(cycle.is_closed(m, y) for m in qm) and not all_closed
        first_open_in_q = cycle.first_open.year == y and cycle.first_open.month in qm
        if (has_open_edge or first_open_in_q) and not all_closed:
            for m in qm:
                cols.append(Period(f"m{m}_{y}", f"{MONTHS[m-1]}-{yy}", [(m, y)], "m"))
        cols.append(Period(f"q{q}_{y}", f"Q{q}-{yy}", [(m, y) for m in qm], "q"))
    cols.append(Period(f"ytd_{y}", "YTD", [(m, y) for m in cycle.closed_months], "ytd"))
    cols.append(Period(f"ytg_{y}", "YTG", [(m, y) for m in cycle.open_months], "ytg"))
    cols.append(Period(f"fy_{y}", f"{y} FY", [(m, y) for m in range(1, 13)], "fy"))
    nyy = str(ny)[2:]
    for q in (1, 2):
        qm = [3 * (q - 1) + i for i in (1, 2, 3)]
        for m in qm:
            cols.append(Period(f"m{m}_{ny}", f"{MONTHS[m-1]}-{nyy}", [(m, ny)], "m"))
        cols.append(Period(f"q{q}_{ny}", f"Q{q}-{nyy}", [(m, ny) for m in qm], "q"))
    for q in (3, 4):
        qm = [3 * (q - 1) + i for i in (1, 2, 3)]
        cols.append(Period(f"q{q}_{ny}", f"Q{q}-{nyy}", [(m, ny) for m in qm], "q"))
    cols.append(Period(f"fy_{ny}", f"{ny} FY", [(m, ny) for m in range(1, 13)], "fy"))
    return cols


# ── Scope options for the filters ─────────────────────────────────────────────
def fetch_scope_map() -> dict[str, dict[str, list[str]]]:
    """{business_segment: {sub_segment: [countries]}} from customer_analysis.

    'Not Set' segments and countries are dropped: they are mapping gaps, not
    markets anyone reviews.
    """
    q = f"""
        SELECT Business_Segment, Sub_Segments, Country_Name
        FROM {_t('customer_analysis')}
        WHERE Business_Segment IS NOT NULL AND Business_Segment NOT IN ('', 'Not Set')
          AND Sub_Segments IS NOT NULL AND Sub_Segments NOT IN ('', 'Not Set')
          AND Country_Name IS NOT NULL AND Country_Name NOT IN ('', 'Not Set')
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """
    df = _client().query(q).to_dataframe()
    out: dict[str, dict[str, list[str]]] = {}
    for _, r in df.iterrows():
        out.setdefault(r["Business_Segment"], {}).setdefault(r["Sub_Segments"], []).append(r["Country_Name"])
    return out


def data_freshness() -> Optional[datetime.datetime]:
    """When adjfc_raw was last loaded, i.e. the Aera data date of the deck."""
    try:
        tbl = _client().get_table(f"{GCP_PROJECT}.{DATASET}.adjfc_raw")
        return tbl.modified
    except Exception:
        return None


def fetch_aera_cycle() -> Optional[tuple["Cycle", datetime.date]]:
    """Aera's live cycle (closed months shaded on the Forecast Adjustments
    screen) from the latest fa_screen_rows fetch, with the fetch date."""
    try:
        df = _client().query(f"""
            SELECT Period, MAX(Value) AS v, MAX(fetch_date) AS fetch_date
            FROM {_t('fa_screen_rows')}
            WHERE fetch_date = (SELECT MAX(fetch_date) FROM {_t('fa_screen_rows')})
              AND Line = '__META__' AND Country_Name = '' AND Category = ''
            GROUP BY 1
        """).to_dataframe()
    except Exception:
        return None
    if df.empty:
        return None
    meta = {r["Period"]: float(r["v"]) for _, r in df.iterrows()}
    if "CLOSED_THROUGH" not in meta or "CURRENT_YEAR" not in meta:
        return None
    return Cycle.from_aera(int(meta["CLOSED_THROUGH"]), int(meta["CURRENT_YEAR"])), \
        pd.to_datetime(df["fetch_date"].iloc[0]).date()


def fetch_snapshot_months() -> list[datetime.date]:
    """Baselines available for the M-1 row and the MoM bridge, newest first."""
    try:
        df = _client().query(f"SELECT DISTINCT snapshot_month FROM {_t('adjfc_cycle_snapshots')} "
                             "ORDER BY snapshot_month DESC").to_dataframe()
        return [pd.to_datetime(d).date() for d in df["snapshot_month"]]
    except Exception:
        return []


def resolve_baseline(cycle: "Cycle", baseline: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """The snapshot that plays 'previous cycle': the one asked for, else the
    latest taken on or before the start of the current month."""
    months = fetch_snapshot_months()
    if baseline is not None:
        return baseline if baseline in months else None
    eligible = [m for m in months if m <= cycle.first_open]
    return eligible[0] if eligible else None


def pick_baseline(sub_segment: str, countries: list[str], cycle: "Cycle",
                  requested: Optional[datetime.date] = None) -> tuple[Optional[datetime.date], str]:
    """Baseline for a section: the requested snapshot, else the latest on or
    before the start of this month. Early in a cycle that snapshot is still
    identical to the live forecast (nothing has moved yet), so if the plan-year
    total differs by less than 0.05% the previous snapshot is used instead and
    the reason is returned for the slide footnote."""
    months = fetch_snapshot_months()
    if not months:
        return None, "no snapshot available"
    if requested is not None:
        return (requested, "") if requested in months else (None, "requested snapshot not found")
    eligible = [m for m in months if m <= cycle.first_open]
    if not eligible:
        return None, "no snapshot on or before this month"
    chosen = eligible[0]
    older = [m for m in months if m < chosen]
    if not older:
        return chosen, ""
    like = f"% {cycle.year}"
    params = _scope_params(sub_segment, countries) + [bigquery.ScalarQueryParameter("snap", "DATE", chosen)]
    cur = _run(f"SELECT SUM(Adjusted_FC) AS v FROM {_t('adjfc_raw')} WHERE Sub_Segments = @sub_segment "
               f"AND Country_Name IN UNNEST(@countries) AND Month_Year LIKE '{like}'", params)
    prev = _run(f"SELECT SUM(Adjusted_FC) AS v FROM {_t('adjfc_cycle_snapshots')} WHERE snapshot_month = @snap "
                f"AND Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries) AND Month_Year LIKE '{like}'",
                params)
    a = float(cur["v"].iloc[0] or 0); b = float(prev["v"].iloc[0] or 0)
    if a and abs(a - b) / abs(a) < 0.0005:
        return older[0], (f"The {chosen:%d %b %Y} snapshot is still identical to the live forecast, "
                          f"so the bridge compares against the previous snapshot ({older[0]:%d %b %Y}).")
    return chosen, ""


def actualise(series: dict, so_cur: dict, cycle: "Cycle") -> dict:
    """Aera zeroes a month's forecast once it has actualised it (with a lag of
    a month or two). Where a closed month's forecast is zero, carry the booked
    sales orders instead, as the deck does; a closed month Aera still carries
    as forecast is left as Aera shows it."""
    y = cycle.year
    out = dict(series)
    for m in range(1, 13):
        if cycle.is_closed(m, y) and not (out.get((m, y)) or 0) > 0:
            v = so_cur.get((m, y))
            if v is not None:
                out[(m, y)] = v
    return out


# ── Query helpers ─────────────────────────────────────────────────────────────
def _scope_params(sub_segment: str, countries: list[str]) -> list:
    return [
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
        bigquery.ArrayQueryParameter("countries", "STRING", list(countries)),
    ]


def _run(q: str, params: list) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    return _client().query(q, job_config=cfg).to_dataframe()


def _monthly_dict(df: pd.DataFrame, col: str) -> dict[tuple[int, int], float]:
    """{(month, year): value} from a frame with a 'Month_Year' column."""
    out: dict[tuple[int, int], float] = {}
    for _, r in df.iterrows():
        try:
            d = datetime.datetime.strptime(str(r["Month_Year"]), "%b %Y")
        except ValueError:
            continue
        v = r[col]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        out[(d.month, d.year)] = out.get((d.month, d.year), 0.0) + float(v)
    return out


def _existing_columns(table: str) -> set[str]:
    tbl = _client().get_table(f"{GCP_PROJECT}.{DATASET}.{table}")
    return {f.name for f in tbl.schema}


def _wide_sums(table: str, prefix: str, months: list[tuple[int, int]],
               sub_segment: str, countries: list[str],
               per_material: bool = False) -> dict[tuple[int, int], float]:
    """Sum <prefix>_<Mon>_<Year> columns of a wide table for the scope.

    per_material=True takes one value per Material x Country before summing:
    customer_analysis is customer-level but Budget is loaded per material, so
    every customer row of a material repeats the same budget and a plain SUM
    counts it once per customer (5x too high for Australia).
    """
    existing = _existing_columns(table)
    wanted = {(m, y): f"{prefix}_{MONTHS[m-1]}_{y}" for (m, y) in months}
    cols = {k: c for k, c in wanted.items() if c in existing}
    if not cols:
        return {}
    if per_material:
        inner = ", ".join(f"ANY_VALUE({c}) AS {c}" for c in cols.values())
        sel = ", ".join(f"SUM({c}) AS {c}" for c in cols.values())
        q = f"""
            SELECT {sel} FROM (
                SELECT Material_Number, Country_Name, {inner}
                FROM {_t(table)}
                WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
                GROUP BY 1, 2
            )
        """
    else:
        sel = ", ".join(f"SUM({c}) AS {c}" for c in cols.values())
        q = f"""
            SELECT {sel}
            FROM {_t(table)}
            WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
        """
    df = _run(q, _scope_params(sub_segment, countries))
    if df.empty:
        return {}
    row = df.iloc[0]
    out = {}
    for k, c in cols.items():
        v = row[c]
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            out[k] = float(v)
    return out


# ── 1. Unconstrained Demand Plan ──────────────────────────────────────────────
@dataclass
class DemandPlan:
    table: pd.DataFrame                    # rows x period labels, numeric (NaN = blank)
    pct_rows: list[str]                    # row labels that hold percentages
    highlights: list[str]                  # auto-filled KEY HIGHLIGHTS bullets
    footnotes: list[str] = field(default_factory=list)


ROW_SOURCE = "Source Forecast*"
ROW_ADJ    = 'Adj. Forecast ("FC")*'
ROW_RR     = "RR Actuals + Adj. FC"
ROW_M1     = "M-1 Consensus Fcst*"
ROW_LY     = "Last Year Actuals"
ROW_SO     = "Actual Sales Orders"
ROW_BUD    = "Budget"
ROW_VSBUD  = "Adj. FC vs Budget (%)"
ROW_VSLY   = "Adj. FC vs LY (%)"


def fetch_demand_plan(sub_segment: str, countries: list[str],
                      cycle: Optional[Cycle] = None,
                      baseline: Optional[datetime.date] = None) -> DemandPlan:
    cycle = cycle or Cycle.from_today()
    y, ny = cycle.year, cycle.next_year
    params = _scope_params(sub_segment, countries)
    footnotes: list[str] = []
    snap_date = resolve_baseline(cycle, baseline)

    # Adjusted forecast, invoiced actuals and the Aera "Adj FC + Actuals" measure
    adj = _run(f"""
        SELECT Month_Year,
               SUM(Adjusted_FC)    AS adjfc,
               SUM(Actuals)        AS rr,
               SUM(Adj_FC_Actuals) AS rr_adj
        FROM {_t('adjfc_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
        GROUP BY 1
    """, params)
    adjfc  = _monthly_dict(adj, "adjfc")
    rr_adj = _monthly_dict(adj, "rr_adj")

    # Booked sales orders, all history (prior year, current year incl. open months)
    oh = _run(f"""
        SELECT Month_Year, SUM(Order_Qty_9LC) AS so
        FROM {_t('order_history_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
        GROUP BY 1
    """, params)
    so = _monthly_dict(oh, "so")

    # Source forecast (SF / 3PD) and Budget from the wide tables
    all_months = [(m, yr) for yr in (y, ny) for m in range(1, 13)]
    src = _wide_sums("stat_3pd_forecast", "SrcFC", all_months, sub_segment, countries)
    bud = _wide_sums("customer_analysis", "Budget", all_months, sub_segment, countries,
                     per_material=True)
    if not any(k[1] == ny for k in bud):
        footnotes.append(f"Budget {ny} not loaded in Aera yet.")

    # M-1 consensus: the Adjusted Forecast as it stood in the baseline snapshot
    m1: dict[tuple[int, int], float] = {}
    if snap_date is not None:
        snap = _run(f"""
            SELECT Month_Year, SUM(Adjusted_FC) AS adjfc
            FROM {_t('adjfc_cycle_snapshots')}
            WHERE snapshot_month = @snap
              AND Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
            GROUP BY 1
        """, params + [bigquery.ScalarQueryParameter("snap", "DATE", snap_date)])
        m1 = _monthly_dict(snap, "adjfc")
    if m1:
        footnotes.append(f"M-1 Consensus = Adjusted Forecast snapshot of {snap_date:%d %b %Y}.")
    else:
        footnotes.append("M-1 Consensus unavailable: no month-start snapshot yet (fills from the next cycle).")

    so_cur = {k: v for k, v in so.items() if k[1] == y}
    src   = actualise(src, so_cur, cycle)
    adjfc = actualise(adjfc, so_cur, cycle)
    if m1:
        m1 = actualise(m1, so_cur, cycle)
    footnotes.append("Closed months that Aera has actualised show booked sales orders in the forecast rows.")

    # Last year: prior-year orders for the plan year; for next year, the plan
    # year's closed-month orders plus its open-month forecast (the deck's rule)
    ly: dict[tuple[int, int], float] = {}
    for m in range(1, 13):
        if (m, y - 1) in so:
            ly[(m, y)] = so[(m, y - 1)]
        base = so.get((m, y)) if cycle.is_closed(m, y) else adjfc.get((m, y))
        if base is not None:
            ly[(m, ny)] = base

    cols = period_columns(cycle)
    rows = {
        ROW_SOURCE: src,
        ROW_ADJ:    adjfc,
        ROW_RR:     rr_adj,
        ROW_M1:     m1,
        ROW_LY:     ly,
        ROW_SO:     so_cur,
        ROW_BUD:    bud,
    }

    def agg(series: dict, p: Period) -> float:
        vals = [series[k] for k in p.months
                if k in series and series[k] is not None and not np.isnan(series[k])]
        return float(sum(vals)) if vals else np.nan

    data = {r: [agg(s, p) for p in cols] for r, s in rows.items()}
    tbl = pd.DataFrame(data, index=[p.label for p in cols]).T

    def pct(a, b):
        if a is None or b is None or np.isnan(a) or np.isnan(b) or b == 0:
            return np.nan
        return (a - b) / b * 100.0

    tbl.loc[ROW_VSBUD] = [pct(tbl.at[ROW_ADJ, c], tbl.at[ROW_BUD, c]) for c in tbl.columns]
    tbl.loc[ROW_VSLY]  = [pct(tbl.at[ROW_ADJ, c], tbl.at[ROW_LY, c])  for c in tbl.columns]

    fy, nfy = f"{y} FY", f"{ny} FY"
    yy, nyy = str(y)[2:], str(ny)[2:]

    def _sgn(v):
        return "n/a" if np.isnan(v) else f"{v:+.1f}%"

    highlights = [
        f"FY{yy} {_sgn(tbl.at[ROW_VSBUD, fy])} vs Budget\nFY{yy} {_sgn(tbl.at[ROW_VSLY, fy])} vs LY",
        "Risks / opportunities: to be added",
        f"FY {ny} {_sgn(tbl.at[ROW_VSLY, nfy])} vs FY{yy} projection",
    ]
    return DemandPlan(tbl, [ROW_VSBUD, ROW_VSLY], highlights, footnotes)


# ── 1a. Forecast Adjustments screen rows (preferred source) ───────────────────
FA_LINE = {
    "Stat Forecast**":                    "stat",
    "3PD Forecast*":                      "pd3",
    "Source Forecast*":                   "source",
    'Adj. Forecast ("FC")*':              "adjfc",
    "Revenue Rec. Actuals + Adj. FC":     "rr_adj",
    "Previous Month Consensus Forecast*": "pmcf",
    "Last Year Actuals":                  "ly",
    "Actual Sales Orders":                "so",
    "Budget":                             "budget",
    "LE":                                 "le",
}


@dataclass
class FaScope:
    """Screen rows for one scope, summed over the selected countries."""
    monthly: dict                      # key -> {(month, year): value}
    cy_total: dict                     # key -> current-year total
    by_country: dict                   # country -> {key: cy_total}
    by_category: dict                  # category -> {key: cy_total}
    closed_through: int
    year: int
    fetch_date: datetime.date
    n_countries: int
    totals: dict = field(default_factory=dict)   # key -> {YTD, YTG, CURRENT_YEAR_TOTAL, NEXT_YEAR_TOTAL}
    by_country_ny: dict = field(default_factory=dict)    # country -> {key: next-year total}
    by_category_ny: dict = field(default_factory=dict)   # category -> {key: next-year total}


def fetch_fa_rows(sub_segment: str, countries: list[str], all_countries: list[str],
                  category: str = "") -> Optional[FaScope]:
    """Latest fa_screen_rows for the scope. The sub-segment total rows
    (Country = '') are used when every country is selected, otherwise the
    per-country rows are summed (all kept lines are additive).
    `category` ('' = whole scope, or a screen category such as 'RTDS') picks
    the rows fetched with that Category filter; None when the scope has none."""
    params = _scope_params(sub_segment, countries)
    try:
        # latest fetch per row key, so a market re-fetched on its own (appended
        # with a newer fetch_date) wins without hiding the other markets
        df = _run(f"""
            SELECT fetch_date, Country_Name, Category, Line, Period, Month_Year, Value
            FROM {_t('fa_screen_rows')}
            WHERE Sub_Segments = @sub_segment
              AND (Country_Name IN UNNEST(@countries) OR Country_Name = '')
            QUALIFY fetch_date = MAX(fetch_date) OVER (PARTITION BY Sub_Segments, Country_Name, Category)
        """, params)
    except Exception:
        return None
    if df.empty:
        return None
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce").fillna(0.0)
    whole = set(countries) >= set(all_countries)
    meta = df[df["Line"] == "__META__"]
    if meta.empty:
        return None
    closed_through = int(meta[meta["Period"] == "CLOSED_THROUGH"]["Value"].iloc[0])
    year = int(meta[meta["Period"] == "CURRENT_YEAR"]["Value"].iloc[0])
    fetch_date = pd.to_datetime(df["fetch_date"].iloc[0]).date()

    def _rows(country_filter, category: str) -> pd.DataFrame:
        d = df[(df["Category"] == category) & (df["Line"] != "__META__")]
        return d[d["Country_Name"].isin(country_filter)] if isinstance(country_filter, (list, set)) \
            else d[d["Country_Name"] == country_filter]

    scope_rows = _rows("", category) if whole else _rows(list(countries), category)
    if scope_rows.empty:
        return None
    monthly: dict = {}
    cy_total: dict = {}
    totals: dict = {}
    for line, key in FA_LINE.items():
        sub = scope_rows[scope_rows["Line"] == line]
        monthly[key] = {}
        for _, r in sub[sub["Month_Year"] != ""].iterrows():
            d = datetime.datetime.strptime(r["Month_Year"], "%b %Y")
            monthly[key][(d.month, d.year)] = monthly[key].get((d.month, d.year), 0.0) + float(r["Value"])
        totals[key] = {p: float(sub[sub["Period"] == p]["Value"].sum())
                       for p in ("YTD", "YTG", "CURRENT_YEAR_TOTAL", "NEXT_YEAR_TOTAL")}
        cy_total[key] = totals[key]["CURRENT_YEAR_TOTAL"]

    def _totals(rows: pd.DataFrame, period: str = "CURRENT_YEAR_TOTAL") -> dict:
        t = rows[rows["Period"] == period]
        return {FA_LINE[l]: float(t[t["Line"] == l]["Value"].sum()) for l in FA_LINE}

    by_country, by_country_ny = {}, {}
    for c in countries:
        rows = _rows(c, category)
        if not rows.empty:
            by_country[c] = _totals(rows)
            by_country_ny[c] = _totals(rows, "NEXT_YEAR_TOTAL")
    cats = sorted({c for c in df["Category"].unique() if c}) if not category else []
    by_category, by_category_ny = {}, {}
    for cat in cats:
        rows = _rows("", cat) if whole else _rows(list(countries), cat)
        if not rows.empty:
            by_category[cat] = _totals(rows)
            by_category_ny[cat] = _totals(rows, "NEXT_YEAR_TOTAL")
    return FaScope(monthly, cy_total, by_country, by_category, closed_through, year, fetch_date,
                   len(countries), totals, by_country_ny, by_category_ny)


def demand_plan_from_fa(fa: FaScope, cycle: Cycle, sub_segment: str, category: str = "") -> DemandPlan:
    """The deck's demand-plan table straight from the screen rows
    (`category` only labels the source note, e.g. 'RTDS')."""
    y, ny = cycle.year, cycle.next_year
    cols = period_columns(cycle)
    # Next year's "Last Year" is this year's Adj. Forecast row (orders for
    # closed months, forecast for open ones), the deck's convention; the
    # screen only carries this year's closed-month actuals there.
    ly = {k: v for k, v in fa.monthly["ly"].items() if k[1] == y}
    for (m, yr), v in fa.monthly["adjfc"].items():
        if yr == y:
            ly[(m, ny)] = v
    rows = {
        ROW_SOURCE: fa.monthly["source"],
        ROW_ADJ:    fa.monthly["adjfc"],
        ROW_RR:     fa.monthly["rr_adj"],
        ROW_M1:     fa.monthly["pmcf"],
        ROW_LY:     ly,
        ROW_SO:     fa.monthly["so"],
        ROW_BUD:    fa.monthly["budget"],
    }
    if any(abs(v) > 0 for v in fa.monthly["le"].values()):
        rows["LE 8+4"] = fa.monthly["le"]

    def agg(series: dict, p: Period) -> float:
        vals = [series[k] for k in p.months if k in series]
        return float(sum(vals)) if vals else np.nan

    tbl = pd.DataFrame({r: [agg(s, p) for p in cols] for r, s in rows.items()},
                       index=[p.label for p in cols]).T
    # YTD / YTG / full-year columns: the screen's own totals, so they match it
    # to the case instead of carrying twelve months of display rounding
    fy, nfy = f"{y} FY", f"{ny} FY"
    screen_key = {ROW_SOURCE: "source", ROW_ADJ: "adjfc", ROW_RR: "rr_adj", ROW_M1: "pmcf",
                  ROW_LY: "ly", ROW_SO: "so", ROW_BUD: "budget", "LE 8+4": "le"}
    for r, key in screen_key.items():
        if r not in tbl.index or key not in fa.totals:
            continue
        t = fa.totals[key]
        for col, period in (("YTD", "YTD"), ("YTG", "YTG"), (fy, "CURRENT_YEAR_TOTAL")):
            if col in tbl.columns and period in t:
                tbl.at[r, col] = t[period]
        if nfy in tbl.columns and "NEXT_YEAR_TOTAL" in t and r != ROW_LY:
            tbl.at[r, nfy] = t["NEXT_YEAR_TOTAL"]
    if nfy in tbl.columns and "adjfc" in fa.totals:
        tbl.at[ROW_LY, nfy] = fa.totals["adjfc"]["CURRENT_YEAR_TOTAL"]
    # next-year columns only make sense for forecast rows; blank the rest
    for r in (ROW_SO, ROW_BUD):
        for p in cols:
            if p.months and p.months[0][1] == ny and (r == ROW_SO or abs(tbl.at[r, p.label] or 0) < 0.5):
                tbl.at[r, p.label] = np.nan

    def pct(a, b):
        if a is None or b is None or np.isnan(a) or np.isnan(b) or b == 0:
            return np.nan
        return (a - b) / b * 100.0

    tbl.loc[ROW_VSBUD] = [pct(tbl.at[ROW_ADJ, c], tbl.at[ROW_BUD, c]) for c in tbl.columns]
    tbl.loc[ROW_VSLY]  = [pct(tbl.at[ROW_ADJ, c], tbl.at[ROW_LY, c])  for c in tbl.columns]
    yy = str(y)[2:]

    def _sgn(v):
        return "n/a" if np.isnan(v) else f"{v:+.1f}%"

    highlights = [
        f"FY{yy} {_sgn(tbl.at[ROW_VSBUD, fy])} vs Budget\nFY{yy} {_sgn(tbl.at[ROW_VSLY, fy])} vs LY",
        "Risks / opportunities: to be added",
        f"FY {ny} {_sgn(tbl.at[ROW_VSLY, nfy])} vs FY{yy} projection",
    ]
    notes = [f"Source: Aera Forecast Adjustments screen, {sub_segment}"
             + (f", category {category}" if category else "")
             + f", fetched {fa.fetch_date:%d %b %Y}; closed through {cycle.last_closed:%b %Y}."]
    if fa.n_countries > 1:
        notes.append(f"Sum of {fa.n_countries} countries' screen rows.")
    return DemandPlan(tbl, [ROW_VSBUD, ROW_VSLY], highlights, notes)


def bridge_from_fa(fa: FaScope, dim: str, cycle: Cycle, max_steps: Optional[int] = None,
                   next_year: bool = False) -> "Optional[Bridge]":
    """Fcst N-1 (Previous Month Consensus) -> steps -> Forecast N (Adj. FC),
    both the screen's full-year totals (current year, or next year when
    `next_year`); the balance that the shown steps do not explain (unfiltered
    or 'Not Set' rows) is the last step."""
    y = cycle.next_year if next_year else cycle.year
    if next_year:
        parts = fa.by_country_ny if dim == "country" else fa.by_category_ny
        start = fa.totals.get("pmcf", {}).get("NEXT_YEAR_TOTAL", 0.0)
        end = fa.totals.get("adjfc", {}).get("NEXT_YEAR_TOTAL", 0.0)
    else:
        parts = fa.by_country if dim == "country" else fa.by_category
        start, end = fa.cy_total["pmcf"], fa.cy_total["adjfc"]
    if not parts or (abs(start) < 0.5 and abs(end) < 0.5):
        return None
    if max_steps is None:
        max_steps = 8 if dim == "country" else 12
    deltas = sorted(((d, v["adjfc"] - v["pmcf"]) for d, v in parts.items()
                     if abs(v["adjfc"]) + abs(v["pmcf"]) >= 0.5 and d != "Not Set"),
                    key=lambda t: -abs(t[1]))
    steps = deltas[:max_steps]
    rest = deltas[max_steps:]
    if rest:
        names = ", ".join(d for d, _ in rest[:3]) + (f" +{len(rest) - 3} more" if len(rest) > 3 else "")
        steps.append((f"Others ({names})", sum(v for _, v in rest)))
    balance = (end - start) - sum(v for _, v in steps)
    if abs(balance) >= 5:                      # below that it is month-level rounding
        steps.append(("Others / adjustments (balance)", balance))
    dim_label = "by Country" if dim == "country" else "by Category"
    note = (f"Fcst N-1 = Previous Month Consensus Forecast and Forecast N = Adj. Forecast, {y} full year, "
            f"from the Aera Forecast Adjustments screen (fetched {fa.fetch_date:%d %b %Y}).")
    return Bridge(f"Bridge {y} FY Month on Month – {dim_label}", "Fcst N-1", start, steps, "Forecast N", end, note,
                  dim=dim, year=y)


# ── 1b. Month-on-month bridge (waterfall, snapshot fallback) ──────────────────
@dataclass
class Bridge:
    title: str
    start_label: str
    start: float
    steps: list[tuple[str, float]]        # (label, delta) in display order
    end_label: str
    end: float
    note: str
    dim: str = ""                         # 'country' | 'category' — the slide stacks the years of one dim
    year: int = 0


@dataclass
class Composition:
    """A split waterfall: the total first, then the parts that make it up."""
    title: str
    total_label: str
    total: float
    parts: list[tuple[str, float]]        # (label, value) largest first; 'Other / Not Set' last
    note: str
    dim: str = ""
    year: int = 0


def composition_from_fa(fa: FaScope, dim: str, cycle: Cycle, key: str = "adjfc",
                        next_year: bool = False) -> "Optional[Composition]":
    """Faizan, 8 Nov 2026: after the MoM bridge, a waterfall whose first bar is the
    current Adj. Forecast full-year total and whose following bars are the amount
    each category (or country) holds, so they add back to the total. Values are
    the screen's CURRENT_YEAR_TOTAL for the Adj. Forecast line; rows the screen
    does not attribute to a category end up in 'Other / Not Set'."""
    if next_year:
        y = cycle.next_year
        parts_src = fa.by_country_ny if dim == "country" else fa.by_category_ny
        total = float(fa.totals.get(key, {}).get("NEXT_YEAR_TOTAL", 0.0))
    else:
        y = cycle.year
        parts_src = fa.by_country if dim == "country" else fa.by_category
        total = float(fa.cy_total.get(key, 0.0))
    if not parts_src or abs(total) < 0.5:
        return None
    parts = sorted(((name, float(v.get(key, 0.0))) for name, v in parts_src.items()),
                   key=lambda kv: -abs(kv[1]))
    parts = [(n, v) for n, v in parts if abs(v) >= 0.5]
    rest = total - sum(v for _, v in parts)
    if abs(rest) >= max(1.0, abs(total) * 0.0005):      # ignore the screen's own rounding
        parts.append(("Other / Not Set", rest))
    what = "Category" if dim == "category" else "Country"
    return Composition(
        title=f"Adj. Forecast {y} FY split – by {what}",
        total_label=f"Forecast N\n{y} FY", total=total, parts=parts,
        note=(f"Forecast N = Adj. Forecast {y} full year from the Aera Forecast Adjustments screen "
              f"(fetched {fa.fetch_date:%d %b %Y}); each bar is the forecast that {what.lower()} holds, "
              f"so the bars add up to the total."),
        dim=dim, year=y,
    )


BRIDGE_DIMS = {
    "country":  ("Country_Name", "by Country"),
    "category": ("Category_Grouper_Description_Z", "by Category"),
}


def fetch_bridge(sub_segment: str, countries: list[str], dim: str, cycle: Optional[Cycle] = None,
                 baseline: Optional[datetime.date] = None, max_steps: Optional[int] = None) -> Optional[Bridge]:
    """Fcst N-1 -> steps -> Forecast N for the plan-year total, the mock-up
    Deloitte/VP agreed: N-1 is the baseline snapshot, N the current forecast,
    each actualised the same way as the demand-plan rows, and every step is
    one country's (or category's) change between the two. Every category with
    volume is shown, zero change included (the mock-up lists them all); for
    countries the eight largest movers are shown and the rest folded into
    "Others". Steps are sorted by size of change."""
    cycle = cycle or Cycle.from_today()
    y = cycle.year
    col, dim_label = BRIDGE_DIMS[dim]
    if max_steps is None:
        max_steps = 8 if dim == "country" else 12
    snap_date = resolve_baseline(cycle, baseline)
    if snap_date is None:
        return None
    params = _scope_params(sub_segment, countries) + [bigquery.ScalarQueryParameter("snap", "DATE", snap_date)]
    like = f"% {y}"

    cur = _run(f"""
        SELECT {col} AS d, Month_Year, SUM(Adjusted_FC) AS v FROM {_t('adjfc_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries) AND Month_Year LIKE '{like}'
        GROUP BY 1, 2
    """, params)
    prev = _run(f"""
        SELECT {col} AS d, Month_Year, SUM(Adjusted_FC) AS v FROM {_t('adjfc_cycle_snapshots')}
        WHERE snapshot_month = @snap
          AND Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries) AND Month_Year LIKE '{like}'
        GROUP BY 1, 2
    """, params)
    oh = _run(f"""
        SELECT {col} AS d, Month_Year, SUM(Order_Qty_9LC) AS v FROM {_t('order_history_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries) AND Month_Year LIKE '{like}'
        GROUP BY 1, 2
    """, params)

    def _by_dim(df: pd.DataFrame) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for d, g in df.groupby("d"):
            out[str(d)] = _monthly_dict(g, "v")
        return out

    cur_d, prev_d, so_d = _by_dim(cur), _by_dim(prev), _by_dim(oh)
    dims = set(cur_d) | set(prev_d) | set(so_d)

    # Decide per month, on the scope total, whether a closed month is taken
    # from orders (Aera has zeroed its forecast) — the same test the demand
    # plan rows use — and apply that decision to every country/category on
    # both sides. Deciding per dimension would let a category whose orders
    # and forecast carry different labels drift, and the two bridges of one
    # scope would no longer start from the same number.
    def _scope_total(series_by_dim: dict[str, dict]) -> dict:
        tot: dict = {}
        for s in series_by_dim.values():
            for k, v in s.items():
                tot[k] = tot.get(k, 0.0) + v
        return tot
    cur_tot, prev_tot = _scope_total(cur_d), _scope_total(prev_d)
    months = [(m, y) for m in range(1, 13)]
    use_so_cur  = {k: cycle.is_closed(*k) and not (cur_tot.get(k) or 0) > 0 for k in months}
    use_so_prev = {k: cycle.is_closed(*k) and not (prev_tot.get(k) or 0) > 0 for k in months}

    totals: dict[str, tuple[float, float]] = {}
    for d in dims:
        c, p, s = cur_d.get(d, {}), prev_d.get(d, {}), so_d.get(d, {})
        n  = sum(s.get(k, 0.0) if use_so_cur[k]  else c.get(k, 0.0) for k in months)
        n1 = sum(s.get(k, 0.0) if use_so_prev[k] else p.get(k, 0.0) for k in months)
        totals[d] = (n1, n)
    start = sum(v[0] for v in totals.values())
    end   = sum(v[1] for v in totals.values())
    # every dimension that carries volume on either side; 'Not Set' only if it moved
    deltas = sorted(((d, v[1] - v[0]) for d, v in totals.items()
                     if (abs(v[0]) + abs(v[1]) >= 0.5 and d != "Not Set") or abs(v[1] - v[0]) >= 0.5),
                    key=lambda t: -abs(t[1]))
    steps = deltas[:max_steps]
    rest = deltas[max_steps:]
    if rest:
        names = ", ".join(d for d, _ in rest[:3]) + (f" +{len(rest) - 3} more" if len(rest) > 3 else "")
        steps.append((f"Others ({names})", sum(v for _, v in rest)))
    note = (f"Fcst N-1 = Adjusted Forecast snapshot of {snap_date:%d %b %Y}; Forecast N = current Aera forecast. "
            f"{y} FY = booked orders for actualised months + Adjusted Forecast for the rest, both sides.")
    return Bridge(f"Bridge {y} FY Month on Month – {dim_label}", "Fcst N-1", start, steps, "Forecast N", end, note)


# ── 2. NPD / NMI tracking ─────────────────────────────────────────────────────
@dataclass
class NpdTracking:
    table: pd.DataFrame                    # one row per SKU + subtotal
    actual_cols: list[str]
    frozen_cols: list[str]
    forecast_cols: list[str]
    note: str


def fetch_npd_tracking(sub_segment: str, countries: list[str],
                       cycle: Optional[Cycle] = None, months_back: int = 12,
                       actual_months: int = 6, max_rows: int = 18) -> NpdTracking:
    """SKUs that launched within the last `months_back` months, or have no sales
    yet but carry a forecast in the next 12 months (upcoming launches).

    Aera's own NMI list is not exported to BigQuery, so launches are read off
    the order history: first order date within the window.
    """
    cycle = cycle or Cycle.from_today()
    y, ny = cycle.year, cycle.next_year
    params = _scope_params(sub_segment, countries)

    oh = _run(f"""
        SELECT Material_Number, Month_Year,
               ANY_VALUE(Sub_Brand_Description) AS sb, ANY_VALUE(Volume) AS vol,
               ANY_VALUE(NULLIF(NULLIF(TRIM(UPC_Code), ''), 'Not Set')) AS upc,
               SUM(Order_Qty_9LC) AS so
        FROM {_t('order_history_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
        GROUP BY 1, 2
    """, params)
    fc = _run(f"""
        SELECT Material_Number, Month_Year,
               ANY_VALUE(Sub_Brand_Long_Description) AS sb, ANY_VALUE(Volume) AS vol,
               SUM(Adjusted_FC) AS adjfc
        FROM {_t('adjfc_raw')}
        WHERE Sub_Segments = @sub_segment AND Country_Name IN UNNEST(@countries)
        GROUP BY 1, 2
    """, params)

    def _date(s):
        try:
            return datetime.datetime.strptime(str(s), "%b %Y").date()
        except ValueError:
            return None

    oh["d"] = oh["Month_Year"].map(_date)
    fc["d"] = fc["Month_Year"].map(_date)
    oh = oh.dropna(subset=["d"]); fc = fc.dropna(subset=["d"])
    oh = oh[oh["so"] > 0]

    window_start = (cycle.first_open - pd.DateOffset(months=months_back)).date()
    horizon_end = (cycle.first_open + pd.DateOffset(months=12)).date()
    # A launch is a new product, not a new code: key the first order on the UPC
    # (one physical pack) so vintage-year and repack re-codes of an existing
    # product inherit its history and do not show up as launches.
    upc_of = (oh.dropna(subset=["upc"]).drop_duplicates("Material_Number")
                .set_index("Material_Number")["upc"])
    oh["_key"] = oh["Material_Number"].map(upc_of).fillna(oh["Material_Number"])
    first_key = oh.groupby("_key")["d"].min()
    first = oh.groupby("Material_Number")["d"].min()
    mat_key = oh.drop_duplicates("Material_Number").set_index("Material_Number")["_key"]
    launched = {m for m in first.index if first_key.get(mat_key.get(m, m), first[m]) >= window_start}
    future_fc = (fc[(fc["d"] >= cycle.first_open) & (fc["d"] < horizon_end)]
                 .groupby("Material_Number")["adjfc"].sum())
    upcoming = set(future_fc[future_fc > 0].index) - set(first.index)
    skus = launched | upcoming

    # attributes: forecast file first (it is the live master), order history as fallback
    attr = {}
    for df_, sbcol in ((oh, "sb"), (fc, "sb")):
        for _, r in df_.drop_duplicates("Material_Number").iterrows():
            attr.setdefault(r["Material_Number"], {})
            attr[r["Material_Number"]].setdefault("sb", r[sbcol])
            attr[r["Material_Number"]].setdefault("vol", r["vol"])

    am = [(m, y) for m in cycle.closed_months][-actual_months:]
    fm = [(m, y) for m in cycle.open_months]
    nm = [(m, ny) for m in range(1, 13)]
    a_lbl = [f"{MONTHS[m-1]}-{str(yr)[2:]}" for m, yr in am]
    f_lbl = [f"{MONTHS[m-1]}-{str(yr)[2:]}" for m, yr in fm]
    n_lbl = [f"{MONTHS[m-1]}-{str(yr)[2:]}" for m, yr in nm]

    so_map = {(r["Material_Number"], r["d"].month, r["d"].year): r["so"] for _, r in oh.iterrows()}
    fc_map = {(r["Material_Number"], r["d"].month, r["d"].year): r["adjfc"] for _, r in fc.iterrows()}

    recs = []
    for sku in skus:
        a = attr.get(sku, {})
        vol = a.get("vol")
        try:
            size = "%g" % float(vol)
        except (TypeError, ValueError):
            size = "" if vol is None else str(vol)
        rec = {"Sub brand": a.get("sb", ""), "Size (L)": size, "SKU": sku, "Note": ""}
        for (m, yr), lbl in zip(am, a_lbl):
            rec[lbl] = so_map.get((sku, m, yr), np.nan)
        for (m, yr), lbl in zip(fm + nm, f_lbl + n_lbl):
            rec[lbl] = fc_map.get((sku, m, yr), np.nan)
        recs.append(rec)
    cols = ["Sub brand", "Size (L)", "SKU", "Note"] + a_lbl + f_lbl + n_lbl
    df = pd.DataFrame(recs, columns=cols)
    if df.empty:
        return NpdTracking(df, a_lbl, f_lbl, n_lbl, "No launches in the last 12 months and no upcoming NMI forecast.")

    num = a_lbl + f_lbl + n_lbl
    df["_vol"] = df[a_lbl].fillna(0).sum(axis=1) + df[f_lbl + n_lbl].fillna(0).sum(axis=1)
    df = df.sort_values(["_vol", "Sub brand"], ascending=[False, True])
    total = df[num].sum(min_count=1)
    n_all = len(df)
    shown = df.head(max_rows).drop(columns="_vol")
    sub = {c: "" for c in cols}
    sub["Sub brand"] = f"Subtotal NMIs ({n_all} SKUs)" if n_all > max_rows else "Subtotal NMIs"
    for c in num:
        sub[c] = total[c]
    shown = pd.concat([shown, pd.DataFrame([sub])], ignore_index=True)
    note = (f"Launches = first order within the last {months_back} months, plus SKUs with a forecast "
            f"but no sales yet. {n_all} SKUs qualify" + (f"; the {max_rows} largest are shown, the subtotal covers all." if n_all > max_rows else "."))
    return NpdTracking(shown, a_lbl, f_lbl, n_lbl, note)


# ── 3. N-3 drivers (top 10 by volume) ─────────────────────────────────────────
@dataclass
class N3Drivers:
    table: pd.DataFrame                    # 10 rows + TOTAL
    month_label: str                       # e.g. 'Sep 2026'
    lag_note: str
    lag_table_date: Optional[datetime.datetime] = None   # when lag1_data was last built


DRIVER_COLS = ["Sub Brand / Size", "IBP", "Actual Sales", "Forecast Error",
               "Error ABS UPC", "% VOL", "MAPE UPC", "BIAS", "Root Cause / Action"]


def lag_table_date() -> Optional[datetime.datetime]:
    try:
        return _client().get_table(f"{GCP_PROJECT}.{DATASET}.lag1_data").modified
    except Exception:
        return None


def fetch_n3_drivers(sub_segment: str, countries: list[str],
                     chain_successors: bool = True) -> Optional[N3Drivers]:
    """Power BI 'Last Month Top 10 By Volume' replica, as in the pre-work PDF:
        Forecast Error = Actual - IBP, Error ABS UPC = sum |error| at UPC grain,
        % VOL = share of the market's actual volume, MAPE UPC = Error / Actual,
        BIAS = Forecast Error / IBP.
    With several countries the UPC key carries the country, so nothing nets
    across markets. The month is the latest closed month the lag table holds.

    chain_successors re-keys retired pack codes onto their successor, the
    pre-work convention (SKU_SUCCESSORS in prework_pdf). On by default: the
    Kraken 330ml -> 375ml pair only reproduces the Power BI line when chained,
    because the new code has no UPC of its own. The one pair where this departs
    from Power BI is Sparkling Margarita 701056 -> 701060 (Power BI leaves the
    split, the deck's own root cause says to combine). Off = raw Power BI view.
    """
    frames = []
    lags = set()
    for c in countries:
        acc = fetch_accuracy(c, sub_segment)
        if acc is None or acc.empty:
            continue
        frames.append(acc.assign(_country=c))
        lags.add(lag_months(c, sub_segment))
    if not frames:
        return None
    acc = pd.concat(frames, ignore_index=True)

    ibp_cols = [c for c in acc.columns if c.startswith("IBP_")]
    if not ibp_cols:
        return None

    def _order(col):
        mon, yr = col.split("_")[1:3]
        return (int(yr), MONTHS.index(mon))

    fc_c = max(ibp_cols, key=_order)
    mon, yr = fc_c.split("_")[1:3]
    act_c = f"Actual_{mon}_{yr}"
    if act_c not in acc.columns:
        return None

    if "Material_Long_Description" not in acc.columns:
        acc = acc.assign(Material_Long_Description="")
    keep = ["Material_Number", "Sub_Brand_Description", "Volume", "UPC_Code",
            "Material_Long_Description", fc_c, act_c, "_country"]
    w = acc[keep].copy()
    w[[fc_c, act_c]] = w[[fc_c, act_c]].fillna(0)
    if chain_successors:
        w = _chain(w, ["Sub_Brand_Description", "Volume", "Material_Long_Description"])

    u = w["UPC_Code"].fillna("").astype(str).str.strip()
    first = (w.assign(_u=u)[lambda d: d["_u"] != ""]
              .drop_duplicates("Material_Number").set_index("Material_Number")["_u"])
    w["UPC_Code"] = u.where(u != "", w["Material_Number"].map(first)).fillna("")
    cmap = _canon_subbrand_map(w["Sub_Brand_Description"])
    w["Sub_Brand_Description"] = w["Sub_Brand_Description"].map(cmap).fillna(w["Sub_Brand_Description"])
    w["_k"] = _upc_key(w) + "|" + w["_country"].astype(str)
    w, = _attach_grain([w])

    vol = w.groupby("_grain", as_index=False)[[fc_c, act_c]].sum()
    has_upc = w[w["UPC_Code"].fillna("").astype(str).str.strip() != ""]
    per_upc = has_upc.groupby(["_grain", "_k"], as_index=False)[[fc_c, act_c]].sum()
    per_upc["_abs"] = (per_upc[fc_c] - per_upc[act_c]).abs()
    err = per_upc.groupby("_grain", as_index=False)["_abs"].sum().rename(columns={"_abs": "Error"})
    rows = vol.merge(err, on="_grain", how="left").rename(columns={fc_c: "IBP", act_c: "Actuals"})
    rows["Error"] = rows["Error"].fillna(0)
    market_vol = rows["Actuals"].sum()
    rows = rows[rows["Actuals"] > 0].copy()
    rows["FcstErr"] = rows["Actuals"] - rows["IBP"]
    rows["PctVol"] = rows["Actuals"] / market_vol * 100 if market_vol else 0
    rows["MAPE"] = rows["Error"] / rows["Actuals"] * 100
    rows["Bias"] = rows.apply(lambda r: 100.0 if r["IBP"] == 0 else r["FcstErr"] / r["IBP"] * 100, axis=1)
    top = rows.nlargest(10, "Actuals")

    t_ibp, t_act, t_err = top["IBP"].sum(), top["Actuals"].sum(), top["Error"].sum()
    t_fe = t_act - t_ibp
    out = pd.DataFrame({
        "Sub Brand / Size": list(top["_grain"]) + ["TOTAL"],
        "IBP":              list(top["IBP"]) + [t_ibp],
        "Actual Sales":     list(top["Actuals"]) + [t_act],
        "Forecast Error":   list(top["FcstErr"]) + [t_fe],
        "Error ABS UPC":    list(top["Error"]) + [t_err],
        "% VOL":            list(top["PctVol"]) + [t_act / market_vol * 100 if market_vol else 0],
        "MAPE UPC":         list(top["MAPE"]) + [t_err / t_act * 100 if t_act else 0],
        "BIAS":             list(top["Bias"]) + [t_fe / t_ibp * 100 if t_ibp else 100.0],
        "Root Cause / Action": [""] * (len(top) + 1),
    })
    lag_txt = ", ".join(f"n-{k}" for k in sorted(lags))
    note = f"IBP = Adjusted Forecast snapshot {lag_txt} for this market"
    note += "; retired pack codes chained onto successors." if chain_successors else "."
    return N3Drivers(out, f"{mon} {yr}", note, lag_table_date())
