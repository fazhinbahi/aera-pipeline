"""
Demand Planning Chatbot — Streamlit UI.

Run with:
    cd /Users/faizanriaz/Documents/aera/chatbot
    streamlit run app.py
"""

import io
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import run_agent
from prework_queries import fetch_customer_analysis, fetch_accuracy, _client, GCP_PROJECT, DATASET
from prework_pdf import build_prework_pdf

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Demand Planning Assistant",
    page_icon="📊",
    layout="wide",
)

_SUB_SEGMENTS = [
    "EMEA ENTERP", "EMEA DEVELOP", "EMEA GTR", "EMEA IMC",
    "APAC ENTERP", "APAC DEVELOP", "APAC GTR", "APAC IMC",
]


@st.cache_data(ttl=3600, show_spinner=False)
def _load_market_map() -> dict[str, list[str]]:
    """Returns {country: sorted list of sub-segments} from BQ (cached 1 hr)."""
    try:
        df = _client().query(
            f"SELECT DISTINCT Country_Name, Sub_Segments "
            f"FROM `{GCP_PROJECT}.{DATASET}.customer_analysis` "
            "WHERE Country_Name IS NOT NULL AND Sub_Segments IS NOT NULL "
            "ORDER BY Country_Name, Sub_Segments"
        ).to_dataframe()
        result: dict[str, list[str]] = {}
        for _, row in df.iterrows():
            result.setdefault(row["Country_Name"], []).append(row["Sub_Segments"])
        return result
    except Exception:
        return {}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📊 Demand Planning Assistant")
    st.caption("Powered by GPT-4.1 + BigQuery")
    st.divider()
    st.markdown(
        "**Available tables**\n"
        "- `customer_analysis` — order history, AdjFC, MAPE\n"
        "- `stat_3pd_forecast` — SF / 3PD / Source FC\n"
        "- `lag1_data` — lag-1/3 accuracy vs actuals\n"
    )
    st.divider()
    st.markdown(
        "**Example questions**\n"
        "- *Prepare analysis for Australia market*\n"
        "- *Which SKUs have the highest MAPE in Japan?*\n"
        "- *Compare SF vs 3PD for APAC IMC in Aug 2026*\n"
        "- *Top 10 customers by volume in UK 2025*\n"
        "- *What is the YTD growth for EMEA Enterprise?*\n"
    )
    st.divider()

    # ── Pre-Work PDF Generator ────────────────────────────────────────────────
    st.subheader("📋 Generate Pre-Work PDF")
    _market_map = _load_market_map()
    _all_countries = sorted(_market_map.keys())

    pw_country = st.selectbox(
        "Country",
        options=_all_countries,
        index=None,
        placeholder="Select a country…",
        key="pw_country",
    )

    # Sub-segment list trims to only what exists for the selected country
    _available_subsegs = _market_map.get(pw_country, _SUB_SEGMENTS) if pw_country else _SUB_SEGMENTS
    pw_subseg = st.selectbox(
        "Sub-Segment",
        options=_available_subsegs,
        index=None,
        placeholder="Select a sub-segment…",
        key="pw_subseg",
    )
    if st.button("Generate Pre-Work", type="primary", use_container_width=True):
        if not pw_country or not pw_subseg:
            st.error("Please select both a country and a sub-segment.")
        else:
            with st.spinner(f"Building pre-work for {pw_country} {pw_subseg}…"):
                try:
                    ca  = fetch_customer_analysis(pw_country, pw_subseg)
                    acc = fetch_accuracy(pw_country, pw_subseg)
                    if ca.empty:
                        st.warning(
                            f"No data found for **{pw_country}** / **{pw_subseg}**. "
                            "Check the country name matches exactly (e.g. 'United Kingdom', "
                            "'Utd.Arab Emir.', 'Australia')."
                        )
                    else:
                        pdf_bytes = build_prework_pdf(ca, acc, pw_country, pw_subseg)
                        fname = (f"PreWork_{pw_country.replace(' ','_')}"
                                 f"_{pw_subseg.replace(' ','_')}.pdf")
                        st.download_button(
                            label="⬇ Download PDF",
                            data=pdf_bytes,
                            file_name=fname,
                            mime="application/pdf",
                            use_container_width=True,
                        )
                        st.success(f"PDF ready — {len(ca):,} rows processed.")
                except Exception as exc:
                    st.error(f"Error generating PDF: {exc}")

    st.divider()
    if st.button("🗑 Clear conversation"):
        st.session_state.messages = []
        st.session_state.chat_display = []
        st.rerun()

# ── Session state init ────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages: list = []       # Anthropic API message history
if "chat_display" not in st.session_state:
    st.session_state.chat_display: list = []   # UI display history


def _csv_download(df: pd.DataFrame, key: str, filename: str = "result.csv"):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    st.download_button(
        label="⬇ Download CSV",
        data=buf.getvalue(),
        file_name=filename,
        mime="text/csv",
        key=key,
    )


# ── Welcome message (shown only before first user message) ───────────────────
if not st.session_state.chat_display:
    with st.chat_message("assistant"):
        st.markdown(
            "## Hi, I'm the Aera Demand Planning Bot 👋\n\n"
            "I'm connected to your live demand planning data and can help you analyse "
            "forecast performance, customer trends, and market insights across "
            "**EMEA** and **APAC** in real time.\n\n"
            "**Here's what I can do:**\n\n"
            "- 📦 **Volume & actuals** — YTD sales, YoY growth, top customers by market\n"
            "- 🎯 **Forecast accuracy** — MAPE, Bias, Lag-1/Lag-3 vs actuals by SKU or country\n"
            "- 📊 **Forecast comparison** — AdjFC vs SF vs 3PD vs Source Forecast\n"
            "- 🚨 **Deviation flags** — which sub-brands are over/under plan and by how much\n"
            "- 📋 **Pre-work PDF** — generate a full pre-alignment document for any market "
            "(use the sidebar →)\n\n"
            "**Try asking:**\n"
            "> *What is the YTD volume for Australia APAC IMC?*\n\n"
            "> *Which SKUs have the highest MAPE in Japan?*\n\n"
            "> *Compare AdjFC vs SO for UK in H2 2026*\n\n"
            "> *Top 10 sub-brands by 2026 actual sales in EMEA Enterprise*"
        )

# ── Render existing conversation ──────────────────────────────────────────────
for chat_idx, item in enumerate(st.session_state.chat_display):
    with st.chat_message(item["role"]):
        st.markdown(item["content"])

        for df_idx, dfi in enumerate(item.get("dataframes", [])):
            df = dfi["df"]
            title = dfi.get("title", "")
            if title:
                st.caption(title)
            st.dataframe(df, use_container_width=True, hide_index=True)
            safe_title = title[:30].replace(" ", "_").replace("/", "-") or "result"
            _csv_download(df, key=f"dl_{chat_idx}_{df_idx}", filename=f"{safe_title}.csv")


# ── Chat input ────────────────────────────────────────────────────────────────
prompt = st.chat_input("Ask anything about your demand planning data…")

if prompt:
    # Show user message immediately
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.chat_display.append({"role": "user", "content": prompt, "dataframes": []})

    # Add to agent history
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Run agent
    with st.chat_message("assistant"):
        with st.spinner("Analysing…"):
            try:
                result = run_agent(st.session_state.messages)
                text = result["text"]
                dfs  = result["dataframes"]
            except Exception as exc:
                text = f"⚠ Error: {exc}"
                dfs  = []

        st.markdown(text)

        chat_idx = len(st.session_state.chat_display)
        for df_idx, dfi in enumerate(dfs):
            df = dfi["df"]
            title = dfi.get("title", "")
            if title:
                st.caption(title)
            st.dataframe(df, use_container_width=True, hide_index=True)
            safe_title = title[:30].replace(" ", "_").replace("/", "-") or "result"
            _csv_download(df, key=f"dl_{chat_idx}_{df_idx}", filename=f"{safe_title}.csv")

    # Persist to display history
    st.session_state.chat_display.append({
        "role": "assistant",
        "content": text,
        "dataframes": dfs,
    })
