"""
Outlier Detection — Demand History Health Dashboard
===================================================
Management view over the Aera outlier corridor data.
Reads from BigQuery (aera_demand_planning.outlier_detection / cov_segmentation) when
credentials are available — this is what powers the hosted Streamlit Cloud app, so the
public repo never needs to carry the underlying business data. Falls back to local
parquet files for offline/local dev.

Run:  streamlit run outlier_dashboard.py
"""
import json
import os

import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit as st

_BQ_PROJECT = "euphoric-hull-442815-n8"
_BQ_DATASET = "aera_demand_planning"
_BQ_ACCOUNT = "jfaizan07@gmail.com"   # local gcloud fallback only


def _bq_client():
    """BigQuery client. Credential priority:
    1. st.secrets["gcp_service_account"]  — Streamlit Cloud / local .streamlit/secrets.toml
    2. GCP_SA_JSON env var                — same convention as gqo_dashboard.py
    3. gcloud auth print-access-token     — local dev fallback
    """
    from google.cloud import bigquery

    try:
        if "gcp_service_account" in st.secrets:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                dict(st.secrets["gcp_service_account"]),
                scopes=["https://www.googleapis.com/auth/bigquery"])
            return bigquery.Client(project=_BQ_PROJECT, credentials=creds)
    except Exception:
        pass   # no secrets.toml present locally — fall through

    sa_json = os.getenv("GCP_SA_JSON")
    if sa_json:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/bigquery"])
        return bigquery.Client(project=_BQ_PROJECT, credentials=creds)

    import subprocess
    from google.oauth2 import credentials as goog_creds
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token", f"--account={_BQ_ACCOUNT}"],
        stderr=subprocess.DEVNULL).decode().strip()
    return bigquery.Client(project=_BQ_PROJECT,
                           credentials=goog_creds.Credentials(token=token))

# ── palette (validated reference instance, light mode) ───────────────────────
SURFACE   = "#fcfcfb"
PAGE      = "#f9f9f7"
INK       = "#0b0b0b"
INK_2     = "#52514e"
MUTED     = "#898781"
GRID      = "#e1e0d9"
BASELINE  = "#c3c2b7"
S1_BLUE   = "#2a78d6"
S2_ORANGE = "#eb6834"
NEUTRAL   = "#f0efec"
CRITICAL  = "#d03b3b"
GOOD      = "#0ca30c"

DATA_CUT = 202607        # last fully closed month at fetch time
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

st.set_page_config(page_title="Outlier Detection — Demand History Health",
                   layout="wide", page_icon="📈")

st.markdown(f"""
<style>
  .stApp {{ background: {PAGE}; }}
  div[data-testid="stMetric"] {{
     background: {SURFACE}; border: 1px solid rgba(11,11,11,0.10);
     border-radius: 10px; padding: 14px 16px;
  }}
  div[data-testid="stMetricLabel"] p {{ color: {INK_2}; font-size: 0.82rem; }}
  h1, h2, h3 {{ color: {INK}; }}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=3600, show_spinner="Loading outlier data…")
def load():
    try:
        client = _bq_client()
        d = client.query(
            f"SELECT * FROM `{_BQ_PROJECT}.{_BQ_DATASET}.outlier_detection`"
        ).to_dataframe()
    except Exception:
        d = pd.read_parquet("outlier_detection_final.parquet")
    d["has_corridor"] = (d["UPPER_BOUND"] > 0) | (d["LOWER_BOUND"] > 0)
    # Zero-width corridor: Aera collapses Upper=Lower when a SKU/market has too little
    # history to compute real variance (avg ~2 months vs ~12 for normal corridors).
    # Any deviation from that single point then always breaches — not a genuine anomaly.
    d["degenerate"] = d["has_corridor"] & (d["UPPER_BOUND"] == d["LOWER_BOUND"])
    d["spike"]  = d["has_corridor"] & (d["UPPER_BOUND"] > 0) & (d["ORDER_QTY_9LC"] > d["UPPER_BOUND"])
    d["crater"] = d["has_corridor"] & (d["LOWER_BOUND"] > 0) & (d["ORDER_QTY_9LC"] < d["LOWER_BOUND"])
    d["breach"] = d["spike"] | d["crater"]
    d["excess"] = np.where(d["spike"], d["ORDER_QTY_9LC"] - d["UPPER_BOUND"],
                  np.where(d["crater"], d["LOWER_BOUND"] - d["ORDER_QTY_9LC"], 0.0))
    d["dt"] = pd.to_datetime(d["MONTHYEAR"].astype(int).astype(str), format="%Y%m")
    return d


@st.cache_data(ttl=3600, show_spinner="Loading COV segmentation…")
def load_cov():
    """COV segmentation scatter extracted from the Aera COV Segmentation dashboard
    (sub-brand × market grain, volume/COV percentile ranks + assigned quadrant)."""
    try:
        client = _bq_client()
        return client.query(
            f"SELECT * FROM `{_BQ_PROJECT}.{_BQ_DATASET}.cov_segmentation`"
        ).to_dataframe()
    except Exception:
        pass
    try:
        return pd.read_parquet("cov_segmentation.parquet")
    except Exception:
        return pd.DataFrame()


@st.cache_data
def build_brand_agg(data: pd.DataFrame) -> pd.DataFrame:
    """Aggregate SKU-level data to sub-brand × market × month.

    Upper/lower bounds are summed across SKUs — so a breach at this level
    means the *whole sub-brand's* combined volume is outside the combined
    corridor, removing false flags from within-brand SKU shifts.
    """
    g = (data.groupby(["sub_brand", "market", "dt", "MONTHYEAR"], dropna=False)
             .agg(
                 ORDER_QTY_9LC =("ORDER_QTY_9LC",  "sum"),
                 UPPER_BOUND   =("UPPER_BOUND",    "sum"),
                 LOWER_BOUND   =("LOWER_BOUND",    "sum"),
                 sub_segment   =("sub_segment",    "first"),
                 category      =("category",       "first"),
                 has_corridor  =("has_corridor",   "any"),
                 n_skus        =("SKU",            "nunique"),
             )
             .reset_index())
    g["spike"]  = g["has_corridor"] & (g["UPPER_BOUND"] > 0) & (g["ORDER_QTY_9LC"] > g["UPPER_BOUND"])
    g["crater"] = g["has_corridor"] & (g["LOWER_BOUND"] > 0) & (g["ORDER_QTY_9LC"] < g["LOWER_BOUND"])
    g["breach"] = g["spike"] | g["crater"]
    g["excess"] = np.where(g["spike"], g["ORDER_QTY_9LC"] - g["UPPER_BOUND"],
                  np.where(g["crater"], g["LOWER_BOUND"] - g["ORDER_QTY_9LC"], 0.0))
    return g


def style(fig, h=340):
    fig.update_layout(
        height=h, plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK_2, size=12),
        margin=dict(l=8, r=8, t=28, b=8),
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12)),
        hoverlabel=dict(bgcolor=INK, font=dict(color="#ffffff", family=FONT)),
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=BASELINE, zerolinecolor=BASELINE,
                     tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=BASELINE, zerolinecolor=BASELINE,
                     tickfont=dict(color=MUTED))
    return fig


def compact(v):
    if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
    if v >= 10_000:    return f"{v/1_000:.0f}K"
    return f"{v:,.0f}"


def kpi_row(corr):
    n_corr   = len(corr)
    n_spike  = int(corr["spike"].sum())
    n_crater = int(corr["crater"].sum())
    rate     = (n_spike + n_crater) / n_corr * 100 if n_corr else 0.0
    vol_out  = corr["excess"].sum()
    return n_corr, n_spike, n_crater, rate, vol_out


def time_chart(corr):
    m = corr.groupby("dt").agg(spikes=("spike", "sum"), craters=("crater", "sum")).reset_index()
    fig = go.Figure()
    fig.add_bar(x=m["dt"], y=m["spikes"], name="Spikes (above corridor)",
                marker=dict(color=S1_BLUE, line=dict(color=SURFACE, width=2)))
    fig.add_bar(x=m["dt"], y=m["craters"], name="Craters (below corridor)",
                marker=dict(color=S2_ORANGE, line=dict(color=SURFACE, width=2)))
    fig.update_layout(barmode="stack")
    return fig


def seg_rate_chart(corr):
    g = (corr.groupby("sub_segment")
             .agg(rate=("breach", "mean"), n=("breach", "size"))
             .reset_index().dropna())
    g = g[g["n"] >= 50].sort_values("rate")
    fig = go.Figure(go.Bar(
        x=g["rate"] * 100, y=g["sub_segment"], orientation="h",
        marker=dict(color=S1_BLUE, line=dict(color=SURFACE, width=2)),
        text=[f"{v*100:.0f}%" for v in g["rate"]], textposition="outside", cliponaxis=False,
        textfont=dict(color=INK_2),
    ))
    fig.update_xaxes(title="breach months / screened months (%)", title_font=dict(color=MUTED))
    return fig


def brand_vol_chart(corr, n=10):
    g = (corr[corr["breach"]].groupby("sub_brand")["excess"].sum()
         .sort_values(ascending=False).head(n).sort_values())
    fig = go.Figure(go.Bar(
        x=g.values, y=g.index, orientation="h",
        marker=dict(color=S1_BLUE, line=dict(color=SURFACE, width=2)),
        text=[f"{v:,.0f}" for v in g.values], textposition="outside", cliponaxis=False,
        textfont=dict(color=INK_2),
    ))
    fig.update_xaxes(title="9L cases", title_font=dict(color=MUTED))
    return fig


def mkt_vol_chart(corr, n=10):
    g = (corr[corr["breach"]].groupby("market")["excess"].sum()
         .sort_values(ascending=False).head(n).sort_values())
    fig = go.Figure(go.Bar(
        x=g.values, y=g.index, orientation="h",
        marker=dict(color=S1_BLUE, line=dict(color=SURFACE, width=2)),
        text=[f"{v:,.0f}" for v in g.values], textposition="outside", cliponaxis=False,
        textfont=dict(color=INK_2),
    ))
    fig.update_xaxes(title="9L cases", title_font=dict(color=MUTED))
    return fig


def apply_strict(df: pd.DataFrame) -> pd.DataFrame:
    """Zero out zero-width (degenerate) corridors, treating them like 'no corridor'
    (order volume is kept; it just stops counting as a reliable bound for breach checks).
    """
    df = df.copy()
    deg = df["degenerate"]
    df.loc[deg, ["UPPER_BOUND", "LOWER_BOUND"]] = 0.0
    df["has_corridor"] = (df["UPPER_BOUND"] > 0) | (df["LOWER_BOUND"] > 0)
    df["spike"]  = df["has_corridor"] & (df["UPPER_BOUND"] > 0) & (df["ORDER_QTY_9LC"] > df["UPPER_BOUND"])
    df["crater"] = df["has_corridor"] & (df["LOWER_BOUND"] > 0) & (df["ORDER_QTY_9LC"] < df["LOWER_BOUND"])
    df["breach"] = df["spike"] | df["crater"]
    df["excess"] = np.where(df["spike"], df["ORDER_QTY_9LC"] - df["UPPER_BOUND"],
                    np.where(df["crater"], df["LOWER_BOUND"] - df["ORDER_QTY_9LC"], 0.0))
    return df


# ── load & filter ─────────────────────────────────────────────────────────────
d = load()
n_degenerate = int(d["degenerate"].sum())

st.sidebar.header("Filters")
strict = st.sidebar.checkbox(
    "Exclude zero-width corridors", value=True,
    help=f"{n_degenerate:,} rows have Upper Bound = Lower Bound — Aera collapses the "
         "corridor to a single point when a SKU/market has too little history (~2 months "
         "avg) to compute real variance. Any deviation then always breaches, which isn't "
         "a genuine anomaly. On by default; volume is still counted, just not treated as "
         "a reliable corridor.",
)
d_eff = apply_strict(d) if strict else d

seg   = st.sidebar.multiselect("Sub-segment", sorted(d["sub_segment"].dropna().unique()))
cat   = st.sidebar.multiselect("Category",    sorted(d["category"].dropna().unique()))
sb    = st.sidebar.multiselect("Sub-brand",   sorted(d["sub_brand"].dropna().unique()))
mkt   = st.sidebar.multiselect("Market",      sorted(d["market"].dropna().unique()))
scope = st.sidebar.radio("Period",
        ["Closed history (Jan 22 – Jul 26)", "Open + future months", "All"], index=0)

f = d_eff.copy()
if seg:  f = f[f["sub_segment"].isin(seg)]
if cat:  f = f[f["category"].isin(cat)]
if sb:   f = f[f["sub_brand"].isin(sb)]
if mkt:  f = f[f["market"].isin(mkt)]
if scope.startswith("Closed"):
    f = f[f["MONTHYEAR"] <= DATA_CUT]
elif scope.startswith("Open"):
    f = f[f["MONTHYEAR"] > DATA_CUT]

st.title("Outlier Detection — Demand History Health")
st.caption(f"Aera EMEA & APAC corridor screening · data as of {d['fetch_date'].iloc[0]} "
           f"· source: aera_demand_planning.outlier_detection"
           + (f" · {n_degenerate:,} zero-width-corridor rows excluded" if strict else ""))

tab_sku, tab_brand, tab_cov = st.tabs(["SKU × Market (grain level)",
                                       "Sub-brand × Market (aggregated)",
                                       "COV Segmentation (sub-brand)"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — original SKU-level view
# ══════════════════════════════════════════════════════════════════════════════
with tab_sku:
    st.caption("Grain: **SKU × market × month** — individual SKU corridors")
    corr = f[f["has_corridor"]]
    n_corr, n_spike, n_crater, rate, vol_out = kpi_row(corr)
    grains_hit = corr.loc[corr["breach"], ["SKU", "market"]].drop_duplicates().shape[0]
    grains_all = corr[["SKU", "market"]].drop_duplicates().shape[0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Grain-months screened", f"{n_corr:,}")
    c2.metric("Outlier rate", f"{rate:.1f}%",
              help="Breach months ÷ screened months (corridor exists)")
    c3.metric("Spikes / Craters", f"{n_spike:,} / {n_crater:,}",
              help="Above upper bound / below lower bound")
    c4.metric("Volume outside corridor", f"{compact(vol_out)} 9LC",
              help="Σ |qty − nearest bound| over breach months")
    c5.metric("Grains affected", f"{grains_hit:,} / {grains_all:,}",
              help="SKU × market combinations with ≥1 breach month")

    st.divider()
    left, right = st.columns((3, 2))
    with left:
        st.subheader("Outlier months over time")
        st.plotly_chart(style(time_chart(corr)), use_container_width=True, key="sku_time")
    with right:
        st.subheader("Outlier rate by sub-segment")
        st.plotly_chart(style(seg_rate_chart(corr)), use_container_width=True, key="sku_segrate")

    l2, r2 = st.columns(2)
    with l2:
        st.subheader("Volume outside corridor — by sub-brand")
        st.plotly_chart(style(brand_vol_chart(corr)), use_container_width=True, key="sku_brandvol")
    with r2:
        st.subheader("Volume outside corridor — by market")
        st.plotly_chart(style(mkt_vol_chart(corr)), use_container_width=True, key="sku_mktvol")

    st.divider()
    st.subheader("Cleaning worklist — biggest corridor breaches")
    st.caption("Candidate months for the monthly outlier review in Aera "
               "(Outlier Detection & Management → Add Row → Validate → Update Main Table). "
               "Ask per row: will this month's number repeat next year?")
    wl = corr[corr["breach"]].copy()
    wl["type"] = np.where(wl["spike"], "spike", "crater")
    wl = (wl[["SKU", "market", "sub_brand", "sub_segment", "category", "MONTHYEAR",
              "type", "ORDER_QTY_9LC", "LOWER_BOUND", "UPPER_BOUND", "excess"]]
          .sort_values("excess", ascending=False))
    st.dataframe(wl.head(25).style.format({
        "ORDER_QTY_9LC": "{:,.0f}", "LOWER_BOUND": "{:,.0f}",
        "UPPER_BOUND": "{:,.0f}", "excess": "{:,.0f}"}), use_container_width=True, height=420)
    st.download_button("Download full worklist (CSV)", wl.to_csv(index=False),
                       "outlier_worklist_sku.csv", "text/csv")

    st.divider()
    st.subheader("Grain explorer — corridor view")
    ge1, ge2 = st.columns(2)
    sku_pick = ge1.selectbox("SKU", sorted(f["SKU"].unique()), key="sku_pick")
    mkts     = sorted(f.loc[f["SKU"] == sku_pick, "market"].dropna().unique())
    mkt_pick = ge2.selectbox("Market", mkts, key="mkt_pick_sku")

    g = d[(d["SKU"] == sku_pick) & (d["market"] == mkt_pick)].sort_values("dt")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=g["dt"], y=g["UPPER_BOUND"], name="Corridor",
                             line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=g["dt"], y=g["LOWER_BOUND"], name="Corridor (bounds)",
                             fill="tonexty", fillcolor="rgba(195,194,183,0.28)",
                             line=dict(width=0), hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=g["dt"], y=g["ORDER_QTY_9LC"], name="Order Qty (9LC)",
                             mode="lines+markers", line=dict(color=S1_BLUE, width=2),
                             marker=dict(size=6)))
    br = g[g["breach"]]
    if len(br):
        fig.add_trace(go.Scatter(x=br["dt"], y=br["ORDER_QTY_9LC"], mode="markers",
                                 name="⚠ Outside corridor",
                                 marker=dict(color=CRITICAL, size=10,
                                             line=dict(color=SURFACE, width=2))))
    sub = g["sub_brand"].dropna().iloc[0] if g["sub_brand"].notna().any() else ""
    st.caption(f"{sku_pick} · {mkt_pick} · {sub}")
    st.plotly_chart(style(fig, h=380), use_container_width=True, key="sku_explorer")

    with st.expander("Definitions & method"):
        st.markdown(f"""
- **Corridor**: Aera's own Upper/Lower bounds per SKU × market × month, fetched from the
  Outlier Detection & Management screen (values are authoritative, not recomputed).
- **Spike / Crater**: order quantity above the upper / below the (non-zero) lower bound.
- **Outlier rate**: breach months ÷ months where a corridor exists.
- **Volume outside corridor**: how far outside the bounds, summed — the size of the anomaly, in 9L cases.
- **Closed history** = Jan 2022–Jul 2026 (settled actuals). **Open/future** = Aug 2026 onward
  (order book still filling — low-side readings there are *not* anomalies).
- Every uncorrected historical spike/crater feeds the statistical forecast next cycle —
  cleaning them in Aera (validated outliers) permanently improves the baseline.
""")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Sub-brand × Market aggregated view
# ══════════════════════════════════════════════════════════════════════════════
with tab_brand:
    st.caption(
        "Grain: **sub-brand × market × month** — all SKUs under the same sub-brand are "
        "combined before the corridor check. A breach here means the *total* sub-brand "
        "volume is genuinely outside the expected range, not just a sale landing on a "
        "different SKU within the family."
    )

    # Build aggregation from the filtered SKU data
    agg = build_brand_agg(f)
    corr_b = agg[agg["has_corridor"]]

    n_corr_b, n_spike_b, n_crater_b, rate_b, vol_out_b = kpi_row(corr_b)
    combos_hit = corr_b.loc[corr_b["breach"], ["sub_brand", "market"]].drop_duplicates().shape[0]
    combos_all = corr_b[["sub_brand", "market"]].drop_duplicates().shape[0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Brand-market-months screened", f"{n_corr_b:,}")
    c2.metric("Outlier rate", f"{rate_b:.1f}%",
              help="Breach months ÷ screened months at sub-brand × market level")
    c3.metric("Spikes / Craters", f"{n_spike_b:,} / {n_crater_b:,}",
              help="Combined volume above / below combined SKU corridors")
    c4.metric("Volume outside corridor", f"{compact(vol_out_b)} 9LC",
              help="Excess volume at sub-brand level (genuine brand-level anomalies)")
    c5.metric("Brand-markets affected", f"{combos_hit:,} / {combos_all:,}",
              help="Sub-brand × market combinations with ≥1 breach month")

    st.info(
        f"**SKU-level rate: {kpi_row(f[f['has_corridor']])[3]:.1f}%  →  "
        f"Sub-brand level rate: {rate_b:.1f}%** — "
        "the reduction shows how many SKU-level flags are explained by within-brand volume shifts "
        "rather than genuine demand anomalies.",
        icon="📊",
    )

    st.divider()
    left, right = st.columns((3, 2))
    with left:
        st.subheader("Outlier months over time")
        st.plotly_chart(style(time_chart(corr_b)), use_container_width=True, key="br_time")
    with right:
        st.subheader("Outlier rate by sub-segment")
        st.plotly_chart(style(seg_rate_chart(corr_b)), use_container_width=True, key="br_segrate")

    l2, r2 = st.columns(2)
    with l2:
        st.subheader("Volume outside corridor — by sub-brand")
        st.plotly_chart(style(brand_vol_chart(corr_b)), use_container_width=True, key="br_brandvol")
    with r2:
        st.subheader("Volume outside corridor — by market")
        st.plotly_chart(style(mkt_vol_chart(corr_b)), use_container_width=True, key="br_mktvol")

    st.divider()
    st.subheader("Cleaning worklist — brand-level breaches")
    st.caption(
        "These are genuine demand anomalies at sub-brand level — volume shifted "
        "between SKUs within the brand has already been cancelled out."
    )
    wl_b = corr_b[corr_b["breach"]].copy()
    wl_b["type"] = np.where(wl_b["spike"], "spike", "crater")
    wl_b = (wl_b[["sub_brand", "market", "sub_segment", "category", "n_skus",
                   "MONTHYEAR", "type", "ORDER_QTY_9LC",
                   "LOWER_BOUND", "UPPER_BOUND", "excess"]]
            .sort_values("excess", ascending=False))
    st.dataframe(
        wl_b.head(25).style.format({
            "ORDER_QTY_9LC": "{:,.0f}", "LOWER_BOUND": "{:,.0f}",
            "UPPER_BOUND": "{:,.0f}", "excess": "{:,.0f}",
            "n_skus": "{:.0f}",
        }),
        use_container_width=True, height=420,
    )
    st.download_button("Download full brand worklist (CSV)", wl_b.to_csv(index=False),
                       "outlier_worklist_brand.csv", "text/csv")

    st.divider()
    st.subheader("Sub-brand explorer — aggregated corridor view")
    be1, be2 = st.columns(2)
    brand_pick = be1.selectbox(
        "Sub-brand", sorted(agg["sub_brand"].dropna().unique()), key="brand_pick"
    )
    brand_mkts = sorted(agg.loc[agg["sub_brand"] == brand_pick, "market"].dropna().unique())
    mkt_pick_b = be2.selectbox("Market", brand_mkts, key="mkt_pick_brand")

    # Use the full (unfiltered) aggregation for the explorer so history is complete
    agg_full = build_brand_agg(d_eff)
    g_b = agg_full[
        (agg_full["sub_brand"] == brand_pick) & (agg_full["market"] == mkt_pick_b)
    ].sort_values("dt")

    if g_b.empty:
        st.info("No data for this combination.")
    else:
        n_skus_label = int(g_b["n_skus"].max())
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=g_b["dt"], y=g_b["UPPER_BOUND"],
                                 name="Combined corridor", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=g_b["dt"], y=g_b["LOWER_BOUND"],
                                 name="Corridor (combined bounds)",
                                 fill="tonexty", fillcolor="rgba(195,194,183,0.28)",
                                 line=dict(width=0), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=g_b["dt"], y=g_b["ORDER_QTY_9LC"],
                                 name="Total order qty (9LC)",
                                 mode="lines+markers",
                                 line=dict(color=S1_BLUE, width=2),
                                 marker=dict(size=6)))
        br_b = g_b[g_b["breach"]]
        if len(br_b):
            fig.add_trace(go.Scatter(x=br_b["dt"], y=br_b["ORDER_QTY_9LC"],
                                     mode="markers", name="⚠ Outside corridor",
                                     marker=dict(color=CRITICAL, size=10,
                                                 line=dict(color=SURFACE, width=2))))
        st.caption(
            f"{brand_pick} · {mkt_pick_b} · {n_skus_label} SKU(s) combined "
            f"· bounds = sum of individual SKU corridors"
        )
        st.plotly_chart(style(fig, h=380), use_container_width=True, key="br_explorer")

        # Show which SKUs make up this brand+market
        skus_in = (d[(d["sub_brand"] == brand_pick) & (d["market"] == mkt_pick_b)]
                   ["SKU"].unique())
        st.caption(f"SKUs included: {', '.join(sorted(skus_in))}")

    with st.expander("How sub-brand aggregation works"):
        st.markdown("""
- **Upper and lower bounds are summed** across all SKUs within the same sub-brand × market × month.
  This represents the total expected range for the brand family in that market.
- A **spike** is flagged only when the *combined* volume exceeds the *combined* upper bound —
  meaning the brand as a whole sold more than expected, not just one SKU getting a big order.
- A **crater** is flagged only when *combined* volume falls below the *combined* lower bound —
  a genuine demand drop for the whole brand, not a redistribution between SKUs.
- SKUs with no corridor contribute their volume to the total but add 0 to the bounds —
  this is slightly conservative (may produce some false spikes), but keeps the view simple.
- The **rate reduction** vs the SKU tab quantifies how much of the apparent volatility is
  explained by within-brand SKU switching rather than true demand variation.
""")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — COV Segmentation (replicated from the Aera COV Segmentation dashboard)
# ══════════════════════════════════════════════════════════════════════════════
with tab_cov:
    cov = load_cov()
    if cov.empty:
        st.info("cov_segmentation.parquet not found — run the COV fetch first.")
    else:
        # honour the shared sidebar filters where the columns exist
        cv = cov.copy()
        if seg: cv = cv[cv["sub_segment"].isin(seg)]
        if sb:  cv = cv[cv["sub_brand"].isin(sb)]
        if mkt: cv = cv[cv["market"].isin(mkt)]

        # quadrant filter
        fc1, fc2 = st.columns([2, 2])
        with fc1:
            quad_pick = st.multiselect(
                "Quadrant", ["Focus", "Monitor", "Selective", "Stable"],
                default=[], placeholder="All quadrants",
                help="Filter the whole tab to one or more segmentation quadrants")
        with fc2:
            focus_sub = st.multiselect(
                "Focus sub-quadrant", ["Focus 1", "Focus 2", "Focus 3", "Focus 4"],
                default=[], placeholder="All of Focus",
                help="Only applies to grains in the Focus quadrant",
                disabled=bool(quad_pick) and "Focus" not in quad_pick)
        if quad_pick:
            cv = cv[cv["segment_group"].isin(quad_pick)]
        if focus_sub and (not quad_pick or "Focus" in quad_pick):
            cv = cv[(cv["segment_group"] != "Focus") | (cv["segment"].isin(focus_sub))]

        st.caption(
            f"Grain: **sub-brand × market** — Aera COV segmentation "
            f"(volume vs COV percentile ranks) · {len(cv):,} of {len(cov):,} grains shown "
            f"· fetched {cov['fetch_date'].iloc[0]} · source: aera_demand_planning.cov_segmentation"
        )

        SEG_COLORS = {"Focus": CRITICAL, "Monitor": S1_BLUE,
                      "Selective": S2_ORANGE, "Stable": GOOD}

        n_tot = len(cv)
        n_foc = int((cv["segment_group"] == "Focus").sum())
        n_f1  = int((cv["segment"] == "Focus 1").sum())
        n_sta = int((cv["segment_group"] == "Stable").sum())
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Grains segmented", f"{n_tot:,}")
        c2.metric("Focus (high vol × high COV)", f"{n_foc:,} · {n_foc/n_tot*100:.0f}%" if n_tot else "0")
        c3.metric("Focus 1 (worst quadrant)", f"{n_f1:,}",
                  help="Volume rank ≥75 AND COV rank ≥75 — the highest-volume, most volatile grains")
        c4.metric("Monitor", f"{int((cv['segment_group']=='Monitor').sum()):,}")
        c5.metric("Stable", f"{n_sta:,} · {n_sta/n_tot*100:.0f}%" if n_tot else "0")

        col_sc, col_mix = st.columns([3, 2])

        with col_sc:
            st.subheader("Segmentation by sub-brand")
            fig = go.Figure()
            # quadrant shading
            shades = [(0, 50, 0, 50, GOOD), (0, 50, 50, 100, S1_BLUE),
                      (50, 100, 0, 50, S2_ORANGE), (50, 100, 50, 100, CRITICAL)]
            for x0, x1, y0, y1, ccol in shades:
                fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1,
                              fillcolor=ccol, opacity=0.06, line_width=0, layer="below")
            for b in (50, 75):
                fig.add_hline(y=b, line=dict(color=BASELINE, width=1,
                              dash="solid" if b == 50 else "dot"))
                fig.add_vline(x=b, line=dict(color=BASELINE, width=1,
                              dash="solid" if b == 50 else "dot"))
            # corner quadrant tags (as in the Aera dashboard)
            corner_tags = [
                (2,  101, "left",  "<b>Monitor</b><br>High Volume & Low COV",  S1_BLUE),
                (98, 101, "right", "<b>Focus</b><br>High Volume & High COV",   CRITICAL),
                (2,  -1,  "left",  "<b>Stable</b><br>Low Volume & Low COV",    GOOD),
                (98, -1,  "right", "<b>Selective</b><br>Low Volume & High COV", S2_ORANGE),
            ]
            for cx, cy, anch, txt, ccol in corner_tags:
                fig.add_annotation(
                    x=cx, y=cy, text=txt, showarrow=False,
                    xanchor=anch, yanchor="top" if cy > 50 else "bottom",
                    align="left" if anch == "left" else "right",
                    font=dict(size=11, color=ccol, family=FONT),
                    bgcolor="rgba(252,252,251,0.75)", borderpad=2,
                )
            for grp, cc in SEG_COLORS.items():
                sub_df = cv[cv["segment_group"] == grp]
                fig.add_trace(go.Scatter(
                    x=sub_df["cov_rank"], y=sub_df["vol_rank"], mode="markers", name=grp,
                    marker=dict(color=cc, size=6, opacity=0.65,
                                line=dict(color=SURFACE, width=0.5)),
                    customdata=sub_df[["sub_brand", "market", "sub_segment", "segment"]],
                    hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]}<br>"
                                   "%{customdata[2]} · %{customdata[3]}<br>"
                                   "COV rank %{x:.0f} · Volume rank %{y:.0f}<extra></extra>"),
                ))
            fig.update_xaxes(title="COV percentile rank →  (more volatile)", range=[-2, 103],
                             title_font=dict(color=MUTED))
            fig.update_yaxes(title="Volume percentile rank →  (bigger)", range=[-2, 103],
                             title_font=dict(color=MUTED))
            st.plotly_chart(style(fig, h=520), use_container_width=True, key="cov_scatter")

        with col_mix:
            st.subheader("Segment mix by sub-segment")
            mix = (cv.groupby(["sub_segment", "segment_group"]).size()
                     .unstack(fill_value=0))
            if not mix.empty:
                mix_pct = mix.div(mix.sum(axis=1), axis=0) * 100
                figm = go.Figure()
                for grp in ["Stable", "Monitor", "Selective", "Focus"]:
                    if grp in mix_pct.columns:
                        figm.add_bar(y=mix_pct.index, x=mix_pct[grp], name=grp,
                                     orientation="h",
                                     marker=dict(color=SEG_COLORS[grp],
                                                 line=dict(color=SURFACE, width=1)))
                figm.update_layout(barmode="stack")
                figm.update_xaxes(title="% of grains", title_font=dict(color=MUTED))
                st.plotly_chart(style(figm, h=380), use_container_width=True, key="cov_mix")

            st.subheader("Grains in selection")
            st.caption(f"All {len(cv):,} grains shown in the scatter, worst quadrant first — "
                       "Focus 1 grains are the top candidates for history cleaning "
                       "and forecast review")
            SEG_ORDER = {"Focus 1": 0, "Focus 2": 1, "Focus 3": 2, "Focus 4": 3,
                         "Monitor": 4, "Selective": 5, "Stable": 6}
            wl = (cv.assign(_o=cv["segment"].map(SEG_ORDER))
                  .sort_values(["_o", "vol_rank", "cov_rank"],
                               ascending=[True, False, False])
                  [["sub_brand", "market", "sub_segment", "segment", "vol_rank", "cov_rank"]]
                  .reset_index(drop=True))
            st.dataframe(wl, use_container_width=True, hide_index=True, height=300)
            st.download_button("Download segmentation CSV",
                               cv.to_csv(index=False).encode(),
                               "cov_segmentation.csv", "text/csv")

        with st.expander("How segmentation works"):
            st.markdown("""
- Every **sub-brand × market** grain is ranked on two axes: **volume percentile**
  (bigger sellers rank higher) and **COV percentile** (more volatile demand ranks higher).
- The 50th percentile splits both axes into four quadrants:
  **Stable** (low vol, low COV), **Monitor** (high vol, low COV — big but predictable),
  **Selective** (low vol, high COV), **Focus** (high vol, high COV — big and erratic).
- The Focus quadrant splits again at the 75th percentiles: **F1** (≥75 on both — worst),
  **F2** (COV ≥75), **F3** (volume ≥75), **F4** (the rest).
- Quadrant boundaries are **inclusive at the threshold** (rank 50 counts as the high side),
  matching Aera's assignment — verified against Aera's own labels (19/20 exact, the one
  difference being exactly this boundary rule).
- Cross-reference with the outlier tabs: a Focus-1 grain with corridor breaches is the
  strongest candidate for **manual history cleaning** before the next stat forecast run.
""")
