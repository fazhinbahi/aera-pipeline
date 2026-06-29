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

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Demand Planning Assistant",
    page_icon="📊",
    layout="wide",
)

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
