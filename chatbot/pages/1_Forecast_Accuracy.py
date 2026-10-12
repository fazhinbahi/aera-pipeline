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
    client = _client()
    cols = {f.name for f in client.get_table(f"{GCP_PROJECT}.{DATASET}.lag1_data").schema}

    lags = sorted({int(m.group(1)) for c in cols
                   if (m := re.match(r"Fcst(\d+)M_", c))})
    months = [m for m in MONTHS if f"Actual_{m}_2026" in cols]
    if not lags or not months:
        return pd.DataFrame(), [], []

    sel = []
    for mth in months:
        sel.append(f"l.Actual_{mth}_2026")
        for lg in lags:
            if f"Fcst{lg}M_{mth}_2026" in cols:
                sel.append(f"l.Fcst{lg}M_{mth}_2026")

    # Attributes join on Material + Customer only. lag1_data carries its own
    # Country_Name straight from Aera, while customer_analysis currently has a
    # large block of rows keyed to Country = 'Not Set', so joining on country
    # would silently drop them.
    q = f"""
        WITH attr AS (
            SELECT Material_Number, Customer_Number,
                   ANY_VALUE(Sub_Segments)            AS Sub_Segments,
                   ANY_VALUE(Customer_Name)           AS Customer_Name,
                   ANY_VALUE(Brand_Family)            AS Brand_Family,
                   ANY_VALUE(Sub_Brand_Description)   AS Sub_Brand,
                   ANY_VALUE(UPC_Code)                AS UPC_Code
            FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
            GROUP BY 1, 2
        )
        SELECT l.Material_Number, l.Country_Name, l.Customer_Number,
               a.Sub_Segments, a.Customer_Name, a.Brand_Family, a.Sub_Brand, a.UPC_Code,
               {', '.join(sel)}
        FROM `{GCP_PROJECT}.{DATASET}.lag1_data` l
        LEFT JOIN attr a
          ON l.Material_Number = a.Material_Number
         AND l.Customer_Number = a.Customer_Number
    """
    df = client.query(q).to_dataframe()
    for c in df.columns:
        if c.startswith(("Fcst", "Actual")):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ["Sub_Segments", "Customer_Name", "Brand_Family", "Sub_Brand", "UPC_Code"]:
        df[c] = df[c].fillna("").astype(str).str.strip()
    return df, lags, months


def metrics(df: pd.DataFrame, lag: int, month: str, netted: bool) -> dict | None:
    """wMAPE and Bias for one lag-month, optionally netting inside each UPC."""
    fc, ac = f"Fcst{lag}M_{month}_2026", f"Actual_{month}_2026"
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
    f"for the same month. 2026, {AVAIL[0]}–{AVAIL[-1]}. Volume in 9L cases."
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
             "error is taken (Power BI / Aera 'Accuracy UPC Code' convention). "
             "Off: absolute error at customer × SKU row level.")

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
