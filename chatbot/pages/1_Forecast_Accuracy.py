"""
Forecast Accuracy by Lag — n-1 … n-6 vs booked sales orders, 2026 to date.

"n-k" is the Adjusted Forecast as it stood k calendar months before the month it
was forecasting: n-3 for September is the June snapshot. Comparing the lags side
by side shows how accuracy decays with horizon, which a single-lag view cannot.

Metrics follow the pre-work definitions:
    wMAPE = SUM|Fcst - Actual| / SUM(Actual)
    Bias  = SUM(Fcst - Actual) / SUM(Actual)
Both are volume-weighted — summed over the scope first, then divided — never an
average of row-level percentages.
"""
import os
import re
import sys

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prework_queries import GCP_PROJECT, DATASET, _client
from prework_pdf import _canon_subbrand_map, _fmt_size, _chain

st.set_page_config(page_title="Forecast Accuracy by Lag", page_icon="📈", layout="wide")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Lag is ORDINAL, so one hue light->dark rather than categorical hues. A six-step
# single-hue ramp cannot hold visible gaps between every adjacent pair, so the
# per-lag comparisons use one hue and the multi-lag trend uses the categorical
# slots in fixed order (valid on the adjacent pairlist a line chart presents).
BLUE        = "#2a78d6"
SEQ_BLUE    = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
POS_RED, NEG_BLUE, NEUTRAL = "#e34948", "#2a78d6", "#f0efec"
INK, INK_MUTED = "#0b0b0b", "#52514e"


# ── Data ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="Loading lag data…")
def load() -> tuple[pd.DataFrame, list[int], list[str]]:
    """customer_analysis is the spine; lag1_data supplies only the forecasts.

    Two reasons it is built this way rather than the other way round:

    Actuals. lag1_data's Actual_* columns are MAX(invoiced, booked SO) per row —
    a "best actual" built for the lag table — which runs 4-8% above the booked
    sales orders Aera shows. Taking actuals from customer_analysis makes the
    denominator reconcile to the Aera screen exactly.

    Sub-segment. 191 Material x Country x Customer combinations sit in more than
    one sub-segment, so a single ANY_VALUE pick silently moves volume between
    them. customer_analysis already carries the sub-segment, and the lag forecast
    is split across them in proportion to each one's actuals (evenly where the
    triple has no actuals to weight on).

    lag1_data rows with no customer_analysis match are dropped, costing 0.01-0.15%
    of forecast volume — the price of anchoring to what Aera reports.
    """
    client = _client()
    cols = {f.name for f in client.get_table(f"{GCP_PROJECT}.{DATASET}.lag1_data").schema}

    lags = sorted({int(m.group(1)) for c in cols
                   if (m := re.match(r"Fcst(\d+)M_", c))})
    months = [m for m in MONTHS if f"Actual_{m}_2026" in cols]
    if not lags or not months:
        return pd.DataFrame(), [], []

    act_sel = ", ".join(f"SUM(COALESCE(Actual_{m}_2026,0)) AS act_{m}" for m in months)
    tot_act = " + ".join(f"COALESCE(Actual_{m}_2026,0)" for m in months)
    fc_sel  = ", ".join(f"COALESCE(l.Fcst{lg}M_{m}_2026,0) * w.wt AS f{lg}_{m}"
                        for lg in lags for m in months
                        if f"Fcst{lg}M_{m}_2026" in cols)

    q = f"""
        WITH ca AS (
            SELECT Material_Number, Country_Name, Customer_Number, Sub_Segments,
                   ANY_VALUE(Customer_Name)         AS Customer_Name,
                   ANY_VALUE(Brand_Family)          AS Brand_Family,
                   ANY_VALUE(Sub_Brand_Description) AS Sub_Brand,
                   ANY_VALUE(Volume)                AS Size_Raw,
                   ANY_VALUE(UPC_Code)              AS UPC_Code,
                   SUM({tot_act})                   AS seg_actual,
                   {act_sel}
            FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
            GROUP BY 1, 2, 3, 4
        ),
        tri AS (
            SELECT Material_Number, Country_Name, Customer_Number,
                   SUM(seg_actual) AS tri_actual, COUNT(*) AS n_seg
            FROM ca GROUP BY 1, 2, 3
        ),
        w AS (
            SELECT ca.*,
                   CASE WHEN tri.tri_actual > 0 THEN ca.seg_actual / tri.tri_actual
                        ELSE 1.0 / tri.n_seg END AS wt
            FROM ca JOIN tri USING (Material_Number, Country_Name, Customer_Number)
        )
        SELECT w.Material_Number, w.Country_Name, w.Customer_Number, w.Sub_Segments,
               w.Customer_Name, w.Brand_Family, w.Sub_Brand, w.Size_Raw, w.UPC_Code,
               {', '.join(f'w.act_{m}' for m in months)}, {fc_sel}
        FROM w
        LEFT JOIN `{GCP_PROJECT}.{DATASET}.lag1_data` l
          USING (Material_Number, Country_Name, Customer_Number)
    """
    df = client.query(q).to_dataframe()
    for c in df.columns:
        if re.match(r"^(f\d+_|act_)", c):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ["Sub_Segments", "Customer_Name", "Brand_Family", "Sub_Brand",
              "Size_Raw", "UPC_Code"]:
        df[c] = df[c].fillna("").astype(str).str.strip()

    # Re-key retired packs onto their successor, as the pre-work does. Aera will
    # not chain across pack sizes, so a changeover otherwise books twice: forecast
    # on the dead code with no sales, sales on the new code with no forecast.
    df = _chain(df, ["Sub_Brand", "Size_Raw"])

    # UPC is a property of the material but is blank on some of its rows and set
    # on others, so fill each material from whichever row carries one. Without
    # this a chained pack keeps its predecessor's UPC on some rows and none on
    # others, and the two halves stop netting against each other.
    _u = df["UPC_Code"]
    _first = (df.assign(_u=_u)[lambda x: x["_u"] != ""]
                .drop_duplicates("Material_Number")
                .set_index("Material_Number")["_u"])
    df["UPC_Code"] = _u.where(_u != "", df["Material_Number"].map(_first)).fillna("")

    # Sub-brand/size is the grain the business reviews, but the master spells one
    # product several ways ("JOSE CUERVO ESPECIAL SILVER" vs "JC Especial Silver")
    # and writes size as both 0.33 and 0.3300. Left alone each product splits into
    # several lines. Same canonicalisation the pre-work uses, so the two agree.
    cmap = _canon_subbrand_map(df["Sub_Brand"])
    sb   = df["Sub_Brand"].map(cmap).fillna(df["Sub_Brand"])
    size = df["Size_Raw"].apply(_fmt_size)
    df["Grain"] = sb.astype(str) + size.apply(lambda s: f" / {s}" if s else "")
    return df, lags, months


def metrics(df: pd.DataFrame, lag: int, month: str, netted: bool) -> dict | None:
    """wMAPE and Bias for one lag-month, optionally netting inside each UPC."""
    fc, ac = f"f{lag}_{month}", f"act_{month}"
    if fc not in df.columns or ac not in df.columns:
        return None
    actual = df[ac].sum()
    if actual <= 0:
        return None
    if netted:
        # Customers and codes sharing a UPC offset before the absolute is taken —
        # the convention of Aera's "Accuracy UPC Code" tab and the Power BI measure.
        key = df["UPC_Code"].where(df["UPC_Code"] != "", "SB:" + df["Sub_Brand"])
        g = df.assign(_k=key).groupby("_k")[[fc, ac]].sum()
        abs_err = (g[fc] - g[ac]).abs().sum()
    else:
        abs_err = (df[fc] - df[ac]).abs().sum()
    signed = (df[fc] - df[ac]).sum()
    return {"lag": f"n-{lag}", "lag_n": lag, "month": month,
            "Forecast": df[fc].sum(), "Actual": actual,
            "wMAPE": abs_err / actual * 100, "Bias": signed / actual * 100}


# ── Page ──────────────────────────────────────────────────────────────────────
st.title("📈 Forecast Accuracy by Lag")
df, LAGS, AVAIL = load()

if df.empty:
    st.error("No lag data available in `lag1_data`.")
    st.stop()

st.caption(
    f"**n-k = the Adjusted Forecast as it stood k months before the month it forecast.** "
    f"n-3 for September is the June 2026 snapshot. Compared against booked sales orders "
    f"for the same month. 2026, {AVAIL[0]}–{AVAIL[-1]}. Volume in 9L cases. "
    f"Actuals are the booked sales orders Aera reports, so totals tie to the "
    f"Forecast Adjustments screen."
)

missing = [l for l in range(1, 7) if l not in LAGS]
if missing:
    st.info(f"Lags currently loaded: {', '.join(f'n-{l}' for l in LAGS)}. "
            f"Still being fetched from Aera: {', '.join(f'n-{l}' for l in missing)}.")

with st.sidebar:
    st.header("Filters")
    def pick(label, col):
        opts = sorted(x for x in df[col].unique() if x)
        chosen = st.multiselect(label, opts, default=[])
        return chosen
    f_seg  = pick("Sub-segment", "Sub_Segments")
    f_ctry = pick("Country", "Country_Name")
    f_cust = pick("Customer", "Customer_Name")
    f_brand= pick("Brand family", "Brand_Family")
    st.divider()
    months = st.multiselect("Months", AVAIL, default=AVAIL)
    netted = st.toggle(
        "Net within UPC", value=False,
        help="On: customers and codes sharing a UPC offset before the absolute "
             "error is taken (Aera 'Accuracy UPC Code' convention). Off: absolute "
             "error at customer × SKU row level. Note materials with no UPC fall "
             "back to their sub-brand here rather than being dropped, so this runs "
             "above the pre-work's Error ABS UPC, which excludes them as Power BI "
             "does.")

d = df.copy()
for col, sel in [("Sub_Segments", f_seg), ("Country_Name", f_ctry),
                 ("Customer_Name", f_cust), ("Brand_Family", f_brand)]:
    if sel:
        d = d[d[col].isin(sel)]
if not months:
    st.warning("Select at least one month."); st.stop()
if d.empty:
    st.warning("No rows match those filters."); st.stop()

rows = [m for lg in LAGS for mth in months if (m := metrics(d, lg, mth, netted))]
if not rows:
    st.warning("No actuals in the selected scope."); st.stop()
res = pd.DataFrame(rows)

# Totals per lag: re-weight across months rather than averaging the percentages
by_lag = (res.groupby(["lag", "lag_n"], as_index=False)
            .apply(lambda g: pd.Series({
                "Forecast": g.Forecast.sum(), "Actual": g.Actual.sum(),
                "wMAPE": (g.wMAPE / 100 * g.Actual).sum() / g.Actual.sum() * 100,
                "Bias":  (g.Bias  / 100 * g.Actual).sum() / g.Actual.sum() * 100}),
                   include_groups=False)
            .sort_values("lag_n"))

best, worst = by_lag.loc[by_lag.wMAPE.idxmin()], by_lag.loc[by_lag.wMAPE.idxmax()]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Actual sales", f"{by_lag.Actual.iloc[0]:,.0f}", help="9L cases in scope")
c2.metric(f"Best lag — {best.lag}", f"{best.wMAPE:.1f}%", help="Lowest wMAPE")
c3.metric(f"Worst lag — {worst.lag}", f"{worst.wMAPE:.1f}%", help="Highest wMAPE")
c4.metric("Spread", f"{worst.wMAPE - best.wMAPE:.1f} pp",
          help="How much accuracy decays across the horizon")

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("wMAPE by lag")
    st.caption("Lower is better. How far the forecast sat from booked orders.")
    base = alt.Chart(by_lag).encode(
        x=alt.X("lag:N", sort=list(by_lag.lag), title=None,
                axis=alt.Axis(labelAngle=0, labelColor=INK, domainColor="#d8d7d3", ticks=False)),
        tooltip=[alt.Tooltip("lag:N", title="Lag"),
                 alt.Tooltip("wMAPE:Q", title="wMAPE", format=".1f"),
                 alt.Tooltip("Forecast:Q", title="Forecast", format=",.0f"),
                 alt.Tooltip("Actual:Q", title="Actual", format=",.0f")])
    st.altair_chart(
        (base.mark_bar(size=38, cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=BLUE)
             .encode(y=alt.Y("wMAPE:Q", title="wMAPE %",
                             axis=alt.Axis(gridColor="#eceae6", domain=False, ticks=False,
                                           labelColor=INK_MUTED)))
         + base.mark_text(dy=-8, color=INK, fontSize=12, fontWeight=600)
               .encode(y="wMAPE:Q", text=alt.Text("wMAPE:Q", format=".1f"))
        ).properties(height=300).configure_view(stroke=None), use_container_width=True)

with right:
    st.subheader("Bias by lag")
    st.caption("Above zero = over-forecast, below = under-forecast.")
    bb = by_lag.assign(dir=lambda x: x.Bias.apply(lambda v: "Over-forecast" if v >= 0 else "Under-forecast"))
    bbase = alt.Chart(bb).encode(
        x=alt.X("lag:N", sort=list(by_lag.lag), title=None,
                axis=alt.Axis(labelAngle=0, labelColor=INK, domainColor="#d8d7d3", ticks=False)),
        tooltip=[alt.Tooltip("lag:N", title="Lag"),
                 alt.Tooltip("Bias:Q", title="Bias", format="+.1f"),
                 alt.Tooltip("dir:N", title="Direction")])
    st.altair_chart(
        (bbase.mark_bar(size=38, cornerRadius=4)
              .encode(y=alt.Y("Bias:Q", title="Bias %",
                              axis=alt.Axis(gridColor="#eceae6", domain=False, ticks=False,
                                            labelColor=INK_MUTED)),
                      color=alt.Color("dir:N",
                                      scale=alt.Scale(domain=["Over-forecast", "Under-forecast"],
                                                      range=[POS_RED, NEG_BLUE]),
                                      legend=alt.Legend(title=None, orient="top")))
         + alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#b9b7b1", size=1).encode(y="y:Q")
        ).properties(height=300).configure_view(stroke=None), use_container_width=True)

st.divider()
st.subheader("wMAPE by month and lag")
st.caption("Each cell is one month forecast at one horizon. Darker = less accurate.")
st.altair_chart(
    alt.Chart(res).mark_rect(stroke="#fcfcfb", strokeWidth=2).encode(
        x=alt.X("month:N", sort=months, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=INK, domain=False, ticks=False)),
        y=alt.Y("lag:N", sort=[f"n-{l}" for l in LAGS], title=None,
                axis=alt.Axis(labelColor=INK, domain=False, ticks=False)),
        color=alt.Color("wMAPE:Q", title="wMAPE %",
                        scale=alt.Scale(range=SEQ_BLUE),
                        legend=alt.Legend(gradientLength=180)),
        tooltip=[alt.Tooltip("month:N", title="Month"), alt.Tooltip("lag:N", title="Lag"),
                 alt.Tooltip("wMAPE:Q", title="wMAPE", format=".1f"),
                 alt.Tooltip("Bias:Q", title="Bias", format="+.1f"),
                 alt.Tooltip("Forecast:Q", title="Forecast", format=",.0f"),
                 alt.Tooltip("Actual:Q", title="Actual", format=",.0f")]
    ).properties(height=40 * len(LAGS) + 40).configure_view(stroke=None),
    use_container_width=True)

st.subheader("Trend over the year")
show = st.multiselect("Lags to plot", [f"n-{l}" for l in LAGS],
                      default=[f"n-{l}" for l in LAGS][:3],
                      help="Up to 4 reads cleanly; beyond that use the heatmap above.")
if show:
    t = res[res.lag.isin(show)]
    sel = alt.selection_point(fields=["month"], nearest=True, on="mouseover", empty=False)
    line = alt.Chart(t).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True)).encode(
        x=alt.X("month:N", sort=months, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=INK, domainColor="#d8d7d3", ticks=False)),
        y=alt.Y("wMAPE:Q", title="wMAPE %",
                axis=alt.Axis(gridColor="#eceae6", domain=False, ticks=False, labelColor=INK_MUTED)),
        color=alt.Color("lag:N", sort=show,
                        scale=alt.Scale(domain=show, range=CATEGORICAL[:len(show)]),
                        legend=alt.Legend(title=None, orient="top")),
        tooltip=[alt.Tooltip("month:N", title="Month"), alt.Tooltip("lag:N", title="Lag"),
                 alt.Tooltip("wMAPE:Q", title="wMAPE", format=".1f"),
                 alt.Tooltip("Bias:Q", title="Bias", format="+.1f")])
    rule = alt.Chart(t).mark_rule(color="#d8d7d3").encode(x=alt.X("month:N", sort=months)).add_params(sel).transform_filter(sel)
    st.altair_chart((rule + line).properties(height=320).configure_view(stroke=None),
                    use_container_width=True)

st.divider()
st.subheader("Top 10 sub-brands by volume")

gc1, gc2 = st.columns([1, 2])
with gc1:
    g_lag = st.selectbox("Forecast horizon", LAGS, index=min(2, len(LAGS) - 1),
                         format_func=lambda l: f"n-{l}",
                         help="Which lag to treat as IBP for this table.")
with gc2:
    g_month = st.selectbox("Month", ["All selected months"] + months, index=0)

g_months = months if g_month == "All selected months" else [g_month]
gfc = [f"f{g_lag}_{m}" for m in g_months if f"f{g_lag}_{m}" in d.columns]
gac = [f"act_{m}" for m in g_months]

if gfc:
    g = d.assign(IBP=d[gfc].sum(axis=1), Actual=d[gac].sum(axis=1))
    # Error ABS nets customers and codes inside each UPC first, the convention
    # of Aera's "Accuracy UPC Code" tab, when the netting toggle is on.
    if netted:
        key = g["UPC_Code"].where(g["UPC_Code"] != "", "SB:" + g["Sub_Brand"])
        per = g.assign(_k=key).groupby(["Grain", "_k"], as_index=False)[["IBP", "Actual"]].sum()
        per["AbsErr"] = (per.IBP - per.Actual).abs()
        err = per.groupby("Grain", as_index=False)["AbsErr"].sum()
    else:
        err = (g.assign(AbsErr=(g.IBP - g.Actual).abs())
                .groupby("Grain", as_index=False)["AbsErr"].sum())
    tot = g.groupby("Grain", as_index=False)[["IBP", "Actual"]].sum().merge(err, on="Grain")
    market = tot.Actual.sum()
    tot = tot[tot.Actual > 0].copy()
    tot["FcstErr"] = tot.Actual - tot.IBP
    tot["PctVol"]  = tot.Actual / market * 100 if market else 0
    tot["MAPE"]    = tot.AbsErr / tot.Actual * 100
    tot["Bias"]    = tot.apply(lambda r: 100.0 if r.IBP == 0
                               else (r.Actual - r.IBP) / r.IBP * 100, axis=1)
    top = tot.nlargest(10, "Actual").reset_index(drop=True)

    if top.empty:
        st.info("No sub-brand volume in this scope.")
    else:
        order = list(top.Grain)
        st.caption(f"IBP at **n-{g_lag}** vs booked sales orders"
                   f"{'' if g_month == 'All selected months' else f' · {g_month} 2026'}"
                   f" · 9L cases. Sub-brand / size, the grain the pre-work reports.")
        melted = top.melt(id_vars="Grain", value_vars=["IBP", "Actual"],
                          var_name="Series", value_name="Volume")
        st.altair_chart(
            alt.Chart(melted).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
               .encode(
                 y=alt.Y("Grain:N", sort=order, title=None,
                         axis=alt.Axis(labelLimit=260, labelColor=INK, domain=False, ticks=False)),
                 x=alt.X("Volume:Q", title="9L cases",
                         axis=alt.Axis(gridColor="#eceae6", domain=False, ticks=False,
                                       labelColor=INK_MUTED)),
                 yOffset=alt.YOffset("Series:N", sort=["IBP", "Actual"]),
                 color=alt.Color("Series:N", sort=["IBP", "Actual"],
                                 scale=alt.Scale(domain=["IBP", "Actual"],
                                                 range=[CATEGORICAL[0], CATEGORICAL[1]]),
                                 legend=alt.Legend(title=None, orient="top")),
                 tooltip=[alt.Tooltip("Grain:N", title="Sub-brand / size"),
                          alt.Tooltip("Series:N", title=""),
                          alt.Tooltip("Volume:Q", title="9LC", format=",.0f")])
               .properties(height=34 * len(top) + 40).configure_view(stroke=None),
            use_container_width=True)

        cols = ["Grain", "IBP", "Actual", "FcstErr", "AbsErr", "PctVol", "MAPE", "Bias"]
        t_ibp, t_act, t_err = top.IBP.sum(), top.Actual.sum(), top.AbsErr.sum()
        # MAPE and BIAS re-derive from the summed volumes rather than averaging the
        # rows above, the same weighting the pre-work's total line uses.
        total = pd.DataFrame([{
            "Grain": "Total (top 10)", "IBP": t_ibp, "Actual": t_act,
            "FcstErr": t_act - t_ibp, "AbsErr": t_err,
            "PctVol": t_act / market * 100 if market else 0,
            "MAPE": t_err / t_act * 100 if t_act else 0,
            "Bias": (t_act - t_ibp) / t_ibp * 100 if t_ibp else 0,
        }])
        show = pd.concat([top[cols], total[cols]], ignore_index=True)
        show.columns = ["Sub Brand / Size", "IBP", "Actual Sales", "Fcst Error",
                        "Error ABS", "% VOL", "MAPE", "BIAS"]
        last = len(show) - 1
        st.dataframe(
            show.style
                .format({"IBP": "{:,.0f}", "Actual Sales": "{:,.0f}",
                         "Fcst Error": "{:+,.0f}", "Error ABS": "{:,.2f}",
                         "% VOL": "{:.2f}%", "MAPE": "{:.2f}%", "BIAS": "{:+.2f}%"})
                .apply(lambda r: ["background-color: #f1efec"] * len(r)
                       if r.name == last else [""] * len(r), axis=1),
            use_container_width=True, hide_index=True,
            height=35 * (len(show) + 1) + 3)   # or the 11th row falls behind a scrollbar
        st.caption(f"The total line is these ten grains only — "
                   f"{t_act / market * 100:.1f}% of the scope's volume. "
                   f"MAPE and BIAS on it are volume-weighted, not row averages.")

st.divider()
st.subheader("Data")
tab1, tab2 = st.tabs(["By lag", "By month × lag"])
with tab1:
    st.dataframe(by_lag[["lag", "Forecast", "Actual", "wMAPE", "Bias"]]
                 .style.format({"Forecast": "{:,.0f}", "Actual": "{:,.0f}",
                                "wMAPE": "{:.1f}%", "Bias": "{:+.1f}%"}),
                 use_container_width=True, hide_index=True)
with tab2:
    piv = res.pivot(index="lag", columns="month", values="wMAPE").reindex(
        [f"n-{l}" for l in LAGS])[[m for m in months if m in res.month.unique()]]
    st.dataframe(piv.style.format("{:.1f}%").background_gradient(cmap="Blues", axis=None),
                 use_container_width=True)
st.download_button("⬇ Download as CSV",
                   res.to_csv(index=False).encode(),
                   f"forecast_accuracy_by_lag_2026.csv", "text/csv")
