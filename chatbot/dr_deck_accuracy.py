"""
Forecast-accuracy series for the Demand Review deck: Enrique's "Monthly WMAPE
and BIAS" chart (Forecast_Accuracy_Review.html), one per deck section.

Replicates the dashboard's engine on the datos_reducidos tables in BigQuery
(datos_reducidos_fc / _act / _cal / _meta, loaded by load_datos_reducidos.py):

* a point = one closed month t (<= META last_closed) measured at lag N
  (default 3): for each group (IMC / 3PD / GTR) the cycle is t - N months and
  the forecast version is the earliest Primary approval of that cycle in the
  calendar; a cycle without a Primary borrows the previous one ("carried") as
  long as the real distance stays within META max_lag; a group without a usable
  approval is skipped for that month.
* units are UPC x group (countries and sub-segments of the scope summed BEFORE
  the absolute error — the dashboard's official grain); forecast and orders are
  full-outer-joined, a missing side counts as 0, units with both at 0 drop out.
* WMAPE = sum|F - A| / sum A, BIAS = (sum F - sum A) / sum A, per month and
  pooled over the window (default 18 closed months, as the dashboard's "18").

Verified 6 Nov 2026 against the dashboard filtered to IMC Australia, N-3, 18
months (53.0 % pooled, +8.9 % bias, 656,598 / 715,240 cases, Jun-25 30.1 %,
Oct-26 90.5 %).
"""
import datetime
import io
import json
import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from dr_deck_data import _client, _run, _scope_params, _t

DEFAULT_LAG = 3
DEFAULT_WINDOW = 18
DEFAULT_MAX_LAG = 5
RUNSTAT_MID_MONTH_FROM_CYCLE = datetime.date(2026, 8, 1)   # dashboard CONFIG.runstatMidMonthFromCycle
GROUPS = ("IMC", "3PD", "GTR")


def _add_months(d: datetime.date, n: int) -> datetime.date:
    y, m = d.year, d.month + n
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return datetime.date(y, m, 1)


def _months_between(a: datetime.date, b: datetime.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def month_label(d: datetime.date, first: bool = False) -> str:
    """May '25 for January and the first point, Jun otherwise (the dashboard's axis)."""
    return d.strftime("%b '%y") if (first or d.month == 1) else d.strftime("%b")


@dataclass
class AccuracySeries:
    title: str
    lag: int
    window: int
    table: pd.DataFrame            # month, label, wmape, bias, actual, forecast, abs_err, items, used, runstat
    pooled_wmape: Optional[float]
    pooled_bias: Optional[float]
    sum_actual: float
    sum_forecast: float
    best: Optional[tuple[str, float]]       # (month label, wmape)
    worst: Optional[tuple[str, float]]
    last_closed: Optional[datetime.date]
    generated_at: str
    note: str
    skipped: list[str] = field(default_factory=list)


# ── data ──────────────────────────────────────────────────────────────────────

def _meta() -> dict:
    df = _client().query(f"SELECT key, value FROM {_t('datos_reducidos_meta')}").to_dataframe()
    return dict(zip(df["key"], df["value"]))


def _calendar() -> pd.DataFrame:
    cal = _client().query(
        f"SELECT approval_date, group_name, cycle_month, IFNULL(role, '') AS role FROM {_t('datos_reducidos_cal')}"
    ).to_dataframe()
    cal["approval_date"] = pd.to_datetime(cal["approval_date"]).dt.date
    cal["cycle_month"] = pd.to_datetime(cal["cycle_month"]).dt.date
    cal["role"] = cal["role"].fillna("").str.strip().str.lower().replace("", "primary")
    return cal


def _fc_has() -> set[tuple[datetime.date, str]]:
    df = _client().query(
        f"SELECT DISTINCT approval_date, group_name FROM {_t('datos_reducidos_fc')}").to_dataframe()
    return set(zip(pd.to_datetime(df["approval_date"]).dt.date, df["group_name"]))


def _act_months() -> list[datetime.date]:
    df = _client().query(f"SELECT DISTINCT month FROM {_t('datos_reducidos_act')} ORDER BY month").to_dataframe()
    return sorted(pd.to_datetime(df["month"]).dt.date)


def _scope_rows(sub_segment: Optional[str], countries: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """FC and ACT rows of the scope; sub_segment None = every sub-segment of those
    countries (the dashboard filtered on country alone)."""
    params = _scope_params(sub_segment or "", countries)
    where = "country IN UNNEST(@countries)" + (" AND sub_segment = @sub_segment" if sub_segment else "")
    fc = _run(f"""
        SELECT approval_date, group_name, sub_segment, country, upc, month, consensus, statistical
        FROM {_t('datos_reducidos_fc')} WHERE {where}""", params)
    act = _run(f"""
        SELECT group_name, sub_segment, country, upc, month, actual
        FROM {_t('datos_reducidos_act')} WHERE {where}""", params)
    for df in (fc, act):
        df["month"] = pd.to_datetime(df["month"]).dt.date
    fc["approval_date"] = pd.to_datetime(fc["approval_date"]).dt.date
    fc["consensus"] = pd.to_numeric(fc["consensus"], errors="coerce").fillna(0.0)
    act["actual"] = pd.to_numeric(act["actual"], errors="coerce").fillna(0.0)
    return fc, act


class _Resolver:
    """resolveUsed() of the dashboard: which approval forecasts (group, cycle)."""

    def __init__(self, cal: pd.DataFrame, fc_has: set, max_lag: int):
        cal = cal[cal["role"] != "ignore"]
        prim = cal[cal["role"] == "primary"].sort_values("approval_date")
        self.first = prim.groupby(["group_name", "cycle_month"])["approval_date"].first().to_dict()
        self.cycles = {g: sorted(c for (gg, c) in self.first if gg == g) for g in GROUPS}
        self.fc_has, self.max_lag = fc_has, max_lag

    def resolve(self, group: str, cycle: datetime.date, target: datetime.date):
        """(approval_date, carried_from_cycle | None) or None when the month is skipped."""
        carried = None
        if (group, cycle) in self.first:
            appr = self.first[(group, cycle)]
        else:
            cycles = self.cycles.get(group, [])
            if not cycles or cycle < cycles[0] or cycle > cycles[-1]:
                return None
            earlier = [c for c in cycles if c < cycle]
            if not earlier:
                return None
            carried = earlier[-1]
            appr = self.first[(group, carried)]
            if _months_between(carried, target) > self.max_lag:
                return None
        if (appr, group) not in self.fc_has:
            return None
        return appr, carried


def fetch_accuracy_series(sub_segment: Optional[str], countries: list[str], lag: int = DEFAULT_LAG,
                          window: int = DEFAULT_WINDOW, title: Optional[str] = None) -> Optional[AccuracySeries]:
    """The dashboard's monthly WMAPE / BIAS series for a scope, or None when the
    datos_reducidos tables are not loaded or hold nothing for the scope."""
    try:
        meta = _meta()
    except Exception:
        return None
    last_closed = pd.to_datetime(meta["last_closed"] + "-01").date() if meta.get("last_closed") else None
    max_lag = int(meta.get("max_lag", DEFAULT_MAX_LAG) or DEFAULT_MAX_LAG)
    generated = meta.get("generated_at", "")
    fc, act = _scope_rows(sub_segment, countries)
    if fc.empty and act.empty:
        return None
    resolver = _Resolver(_calendar(), _fc_has(), max_lag)
    months = [m for m in _act_months() if last_closed is None or m <= last_closed]
    months = months[-window:]
    groups = sorted(set(fc["group_name"]) | set(act["group_name"]))

    rows, skipped = [], []
    for t in months:
        cycle = _add_months(t, -lag)
        abs_err = err = A = F = 0.0
        items, used = 0, []
        for g in groups:
            r = resolver.resolve(g, cycle, t)
            if r is None:
                skipped.append(f"{t:%b %Y} {g}")
                continue
            appr, carried = r
            f = fc[(fc["approval_date"] == appr) & (fc["group_name"] == g) & (fc["month"] == t)]
            a = act[(act["group_name"] == g) & (act["month"] == t)]
            j = pd.concat([f.groupby("upc")["consensus"].sum().rename("f"),
                           a.groupby("upc")["actual"].sum().rename("a")], axis=1).fillna(0.0)
            j = j[(j["f"] != 0) | (j["a"] != 0)]
            abs_err += float((j["f"] - j["a"]).abs().sum())
            err += float((j["f"] - j["a"]).sum())
            A += float(j["a"].sum())
            F += float(j["f"].sum())
            items += len(j)
            used.append(f"{g}: {appr:%d %b %Y}" + (f" (carried from {carried:%b %Y})" if carried else ""))
        rows.append({
            "month": t, "label": month_label(t, first=(t == months[0])),
            "wmape": abs_err / A if A > 0 else None, "bias": err / A if A > 0 else None,
            "actual": A, "forecast": F, "abs_err": abs_err, "items": items,
            "used": "; ".join(used), "runstat": cycle >= RUNSTAT_MID_MONTH_FROM_CYCLE,
        })
    table = pd.DataFrame(rows)
    if table.empty or table["items"].sum() == 0:
        return None
    sum_a, sum_f, sum_abs = table["actual"].sum(), table["forecast"].sum(), table["abs_err"].sum()
    valid = table.dropna(subset=["wmape"])
    best = worst = None
    if not valid.empty:
        b, w = valid.loc[valid["wmape"].idxmin()], valid.loc[valid["wmape"].idxmax()]
        best, worst = (b["month"].strftime("%b %Y"), float(b["wmape"])), (w["month"].strftime("%b %Y"), float(w["wmape"]))
    scope = ", ".join(countries) if len(countries) <= 3 else f"{len(countries)} markets"
    return AccuracySeries(
        title=title or f"Monthly WMAPE and BIAS – {sub_segment or 'all sub-segments'} {scope} · N-{lag}",
        lag=lag, window=len(table), table=table,
        pooled_wmape=sum_abs / sum_a if sum_a > 0 else None,
        pooled_bias=(sum_f - sum_a) / sum_a if sum_a > 0 else None,
        sum_actual=float(sum_a), sum_forecast=float(sum_f), best=best, worst=worst,
        last_closed=last_closed, generated_at=generated,
        note=(f"Consensus forecast (approved {lag} months before the month, per the cycle calendar) vs sales orders, "
              f"9LC, UPC level; countries of the scope summed before the absolute error. WMAPE = Σ|F−A| ÷ ΣA, "
              f"BIAS = (ΣF−ΣA) ÷ ΣA. Source: Enrique's datos_reducidos export generated {generated}"
              + (f", closed through {last_closed:%b %Y}" if last_closed else "") + "."),
        skipped=skipped,
    )


# ── RCA table: biggest offenders by absolute error ────────────────────────────

CLASS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "demand_classification.json")
_CLASS_CACHE: dict = {}
_ACT_CACHE: dict = {}
DTYPES = ("Smooth", "Erratic", "Intermittent", "Lumpy")
MIN_VOLUME = 25            # dashboard: rows below this show — instead of WMAPE / BIAS


def _class_lookup() -> dict:
    """The dashboard's embedded VP/Proximo classification (ADI × CV, snapshot FA_2026-09-16),
    keyed (upc, country, sub_segment)."""
    if not _CLASS_CACHE:
        try:
            data = json.load(open(CLASS_JSON, encoding="utf-8"))
            cols = data["columns"]
            for r in data["rows"]:
                d = dict(zip(cols, r))
                _CLASS_CACHE[(d["upc"], d["country"], d["sub_segment"])] = d
            # merged UPCs ("fusion"): an old UPC resolves to the row of the UPC it was merged into
            acols = data.get("alias_columns", [])
            for a in data.get("aliases", []):
                d = dict(zip(acols, a))
                key = (d["alias_upc"], d["country"], d["sub_segment"])
                target = _CLASS_CACHE.get((d["upc"], d["country"], d["sub_segment"]))
                if key not in _CLASS_CACHE and target is not None:
                    _CLASS_CACHE[key] = target
            _CLASS_CACHE["__meta__"] = data["meta"]
        except Exception:
            _CLASS_CACHE["__meta__"] = {}
    return _CLASS_CACHE


def _act_all() -> pd.DataFrame:
    """Every ACT row of the file (small): unit volumes and monthly series for the classification."""
    if "df" not in _ACT_CACHE:
        df = _client().query(f"SELECT group_name, sub_segment, country, upc, month, actual FROM {_t('datos_reducidos_act')}").to_dataframe()
        df["month"] = pd.to_datetime(df["month"]).dt.date
        df["actual"] = pd.to_numeric(df["actual"], errors="coerce").fillna(0.0)
        _ACT_CACHE["df"] = df
        _ACT_CACHE["vol"] = df.groupby(["group_name", "sub_segment", "country", "upc"])["actual"].sum().to_dict()
    return _ACT_CACHE["df"]


def _unit_dtype(upc: str, country: str, sub_segment: str, last_closed: Optional[datetime.date]) -> str:
    """classify() + classifyNew() of the dashboard for one unit: the lookup's class when it is
    one of the four types; a 'Too New To Classify' unit is re-classified from its orders with
    the same ADI / CV thresholds once it has 6+ months of life (only when the orders reproduce
    what the lookup saw at its closing month); anything else is 'Not classifiable'."""
    L = _class_lookup()
    r = L.get((upc, country, sub_segment))
    if not r:
        return "Not classifiable"
    cls = r["demand_type"]
    if cls in DTYPES:
        return cls
    meta = L.get("__meta__") or {}
    th, closed = meta.get("thresholds"), meta.get("closed")
    if cls != "Too New To Classify" or not th or not closed or last_closed is None:
        return "Not classifiable"
    cier = pd.to_datetime(closed).date().replace(day=1)
    if not last_closed > cier:
        return "Not classifiable"
    act = _act_all()
    q = act[(act["upc"] == upc) & (act["country"] == country) & (act["sub_segment"] == sub_segment)
            & (act["month"] <= last_closed)].copy()
    q["actual"] = q["actual"].clip(lower=0)
    series = q.groupby("month")["actual"].sum()

    def stat(to: datetime.date):
        ms = sorted(m for m, v in series.items() if m <= to and v > 0)
        if not ms:
            return None
        v = [float(series[m]) for m in ms]
        n, mean = len(v), sum(v) / len(v)
        cv = (sum((x - mean) ** 2 for x in v) / n) ** 0.5 / mean if n > 1 else None
        return {"vida": _months_between(ms[0], to) + 1, "dem": n, "cv": cv}

    if r.get("life_months") is None or r.get("demand_months") is None:
        return "Not classifiable"
    a = stat(cier)
    if not a or a["vida"] != r["life_months"] or a["dem"] != r["demand_months"]:
        return "Not classifiable"
    b = stat(last_closed)
    if not b or b["vida"] < th.get("vidaMinima", 6):
        return "Not classifiable"
    adi, hi = b["vida"] / b["dem"], (b["cv"] is not None and b["cv"] >= th["CV"])
    return ("Erratic" if hi else "Smooth") if adi < th["ADI"] else ("Lumpy" if hi else "Intermittent")


def item_demand_type(group: str, upc: str, last_closed: Optional[datetime.date]) -> str:
    """The dashboard's Type column (ncType): the item is UPC × group across every country and
    sub-segment of the file; its type is that of its classifiable unit with the most orders."""
    _act_all()
    best, best_vol = None, float("-inf")
    for (g, seg, country, u), vol in _ACT_CACHE["vol"].items():
        if g != group or u != upc:
            continue
        t = _unit_dtype(u, country, seg, last_closed)
        if t == "Not classifiable":
            continue
        if vol > best_vol:
            best, best_vol = t, vol
    return best or "Not classifiable"


RCA_COLS = ["#", "UPC", "SKU", "Sub-brand", "Size", "Type", "Ordered", "Forecast", "F − A", "|F − A|",
            "% of total error", "WMAPE", "BIAS", "Root Cause / Action"]


@dataclass
class RcaDrivers:
    month: datetime.date
    month_label: str
    lag: int
    table: pd.DataFrame
    items: int
    abs_err_total: float
    sum_actual: float
    sum_forecast: float
    wmape: Optional[float]
    bias: Optional[float]
    used: str
    note: str


def fetch_rca_drivers(sub_segment: Optional[str], countries: list[str], lag: int = DEFAULT_LAG,
                      top: int = 10, month: Optional[datetime.date] = None) -> Optional[RcaDrivers]:
    """The dashboard's 'Biggest offenders by absolute error · by country' for the
    last closed month: UPC × country × sub-segment units of the scope at N-lag,
    sorted by |F − A|, top N, with the RCA Excel columns. Root cause left blank."""
    try:
        meta = _meta()
    except Exception:
        return None
    last_closed = pd.to_datetime(meta["last_closed"] + "-01").date() if meta.get("last_closed") else None
    max_lag = int(meta.get("max_lag", DEFAULT_MAX_LAG) or DEFAULT_MAX_LAG)
    fc, act = _scope_rows(sub_segment, countries)
    if fc.empty and act.empty:
        return None
    months = [m for m in _act_months() if last_closed is None or m <= last_closed]
    if not months:
        return None
    t = month or months[-1]
    cycle = _add_months(t, -lag)
    resolver = _Resolver(_calendar(), _fc_has(), max_lag)
    units, used = [], []
    for g in sorted(set(fc["group_name"]) | set(act["group_name"])):
        r = resolver.resolve(g, cycle, t)
        if r is None:
            continue
        appr, carried = r
        f = fc[(fc["approval_date"] == appr) & (fc["group_name"] == g) & (fc["month"] == t)]
        a = act[(act["group_name"] == g) & (act["month"] == t)]
        key = ["sub_segment", "country", "upc"]
        j = pd.concat([f.groupby(key)["consensus"].sum().rename("f"),
                       a.groupby(key)["actual"].sum().rename("a")], axis=1).fillna(0.0)
        j = j[(j["f"] != 0) | (j["a"] != 0)].reset_index()
        j["group_name"] = g
        units.append(j)
        used.append(f"{g}: approved {appr:%d %b %Y} · cycle {cycle:%b %Y}" + (f" (carried from {carried:%b %Y})" if carried else ""))
    if not units:
        return None
    u = pd.concat(units, ignore_index=True)
    u["err"] = u["f"] - u["a"]
    u["abs_err"] = u["err"].abs()
    # official (UPC × group) items: a unit's error counts with the sign of its item's net error
    item_err = u.groupby(["group_name", "upc"])["err"].transform("sum")
    u["alloc"] = item_err.map(lambda v: 1.0 if v >= 0 else -1.0) * u["err"]
    total_abs = float(u.groupby(["group_name", "upc"])["err"].sum().abs().sum())
    sum_a, sum_f = float(u["a"].sum()), float(u["f"].sum())
    u["pct_total"] = u["alloc"] / total_abs if total_abs else 0.0
    u["wmape"] = u.apply(lambda r: r["abs_err"] / r["a"] if r["a"] >= MIN_VOLUME else None, axis=1)
    u["bias"] = u.apply(lambda r: r["err"] / r["a"] if r["a"] >= MIN_VOLUME else None, axis=1)
    topn = u.sort_values("abs_err", ascending=False).head(top).reset_index(drop=True)
    # attributes: SKU codes and sub-brand / size from the file's SKU and DIM rows
    params = _scope_params(sub_segment or "", countries)
    where = "country IN UNNEST(@countries)" + (" AND sub_segment = @sub_segment" if sub_segment else "")
    sku = _run(f"SELECT group_name, sub_segment, country, upc, material FROM {_t('datos_reducidos_sku')} WHERE {where}", params)
    dim = _run(f"SELECT group_name, sub_segment, country, upc, sub_brand, category, size_l FROM {_t('datos_reducidos_dim')} WHERE {where}", params)
    skus = sku.groupby(["sub_segment", "country", "upc"])["material"].apply(list).to_dict()
    dims = {(r.sub_segment, r.country, r.upc): r for r in dim.itertuples()}
    dims_upc = {r.upc: r for r in dim.itertuples()}
    rows = []
    for i, r in topn.iterrows():
        k = (r["sub_segment"], r["country"], r["upc"])
        mats = skus.get(k, [])
        d = dims.get(k) or dims_upc.get(r["upc"])
        size = d.size_l if d is not None and pd.notna(d.size_l) else None
        rows.append({
            "#": i + 1, "UPC": r["upc"],
            "SKU": (mats[0] + (f" +{len(mats) - 1}" if len(mats) > 1 else "")) if mats else "—",
            "Sub-brand": d.sub_brand if d is not None else "", "Size": f"{size:g} L" if size is not None else "",
            "Type": item_demand_type(r["group_name"], r["upc"], last_closed),
            "Ordered": r["a"], "Forecast": r["f"], "F − A": r["err"], "|F − A|": r["abs_err"],
            "% of total error": r["pct_total"], "WMAPE": r["wmape"], "BIAS": r["bias"],
            "Root Cause / Action": "",
            "country": r["country"], "sub_segment": r["sub_segment"], "group": r["group_name"],
        })
    table = pd.DataFrame(rows, columns=RCA_COLS + ["country", "sub_segment", "group"])
    cls_meta = _class_lookup().get("__meta__") or {}
    return RcaDrivers(
        month=t, month_label=t.strftime("%b %Y"), lag=lag, table=table, items=len(u),
        abs_err_total=total_abs, sum_actual=sum_a, sum_forecast=sum_f,
        wmape=total_abs / sum_a if sum_a else None, bias=(sum_f - sum_a) / sum_a if sum_a else None,
        used="; ".join(used),
        note=(f"Units = UPC × country × sub-segment at N-{lag}, ordered by |F − A| (9LC); rows under {MIN_VOLUME} ordered cases "
              f"show — for WMAPE / BIAS. % of total error = the row's share of the scope's Σ|F − A| (negative = it offsets other rows). "
              f"Demand type: VP/Proximo ADI × CV classification of the UPC's main market" + (f" (snapshot {cls_meta.get('snapshot', '')})" if cls_meta else "")
              + f". Source: Enrique's datos_reducidos export generated {meta.get('generated_at', '')}. Root cause / action: manual input."),
    )


# ── chart ─────────────────────────────────────────────────────────────────────

def render_accuracy_png(acc: AccuracySeries, width_in: float, height_in: float, dpi: int = 220) -> bytes:
    """The dashboard's two-panel chart: WMAPE line with point labels on top,
    BIAS bars (gold = over-forecast, teal = under-forecast) below, RunStat
    mid-month cycles shaded; crisp PNG for Google Slides."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    line, gold, teal, band, grid = "#2E5D68", "#B8892B", "#3A8C8F", "#FFF2C4", "#DCDCDC"
    t = acc.table
    n = len(t)
    x = list(range(n))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(width_in, height_in), dpi=dpi,
                                   gridspec_kw={"height_ratios": [1.3, 1.0], "hspace": 0.42})
    big = 11 if n <= 18 else 9.5
    for ax in (ax1, ax2):
        for i, rs in enumerate(t["runstat"]):
            if rs:
                ax.axvspan(i - 0.5, i + 0.5, color=band, zorder=0, lw=0)
        ax.set_xlim(-0.6, n - 0.4)
        ax.grid(axis="y", color=grid, lw=0.8, zorder=1)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color("#BFBFBF")
        ax.tick_params(axis="both", length=0, labelsize=big)
        ax.set_xticks(x)
    # WMAPE
    w = [None if pd.isna(v) else float(v) for v in t["wmape"]]
    xs = [i for i, v in enumerate(w) if v is not None]
    ys = [w[i] for i in xs]
    ax1.plot(xs, ys, color=line, lw=2.2, marker="o", ms=6, zorder=3)
    top = max([1.0] + [v + 0.12 for v in ys])
    ax1.set_ylim(0, top)
    ax1.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10]))   # ≤5 labels whatever the range
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
    for i, v in zip(xs, ys):
        ax1.annotate(f"{v:.0%}", (i, v), textcoords="offset points", xytext=(0, 8), ha="center",
                     fontsize=big, color="#222222", zorder=4)
    ax1.set_xticklabels([""] * n)
    ax1.text(0, 1.04, "WMAPE", transform=ax1.transAxes, fontsize=big + 1, color="#222222", va="bottom")
    # BIAS
    b = [0.0 if pd.isna(v) else float(v) for v in t["bias"]]
    ax2.bar(x, b, color=[gold if v >= 0 else teal for v in b], width=0.6, zorder=3)
    ax2.axhline(0, color="#9E9E9E", lw=0.8, zorder=2)
    lim = max(0.2, max(abs(v) for v in b) * 1.25)
    ax2.set_ylim(-lim if min(b) < 0 else -lim * 0.25, lim)
    ax2.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10], symmetric=min(b) < 0))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0%}" if round(v, 6) else "0%"))
    ax2.set_xticklabels(list(t["label"]), fontsize=big)
    ax2.text(0, 1.04, "BIAS · above 0 = over-forecast", transform=ax2.transAxes, fontsize=big + 1,
             color="#222222", va="bottom")
    handles = [Line2D([], [], color=line, lw=2.2, marker="o", label="WMAPE"),
               Patch(color=gold, label="BIAS over-forecast"), Patch(color=teal, label="BIAS under-forecast"),
               Patch(color=band, label="RunStat mid-month cycle")]
    fig.legend(handles=handles, loc="lower left", ncol=4, frameon=False, fontsize=big - 1,
               bbox_to_anchor=(0.04, -0.005))
    fig.subplots_adjust(left=0.055, right=0.985, top=0.93, bottom=0.14)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    return buf.getvalue()
