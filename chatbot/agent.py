"""
Agentic loop: Claude (Anthropic API) with tool use over BigQuery demand planning data.
"""

import os
import time
from pathlib import Path

import anthropic
import pandas as pd

from schema import SYSTEM_PROMPT
from tools import run_sql, get_schema

MODEL          = "claude-sonnet-5"
MAX_ITERATIONS = 15
_RETRY_DELAYS  = [10, 20, 40]


def _load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        key = st.secrets.get("ANTHROPIC_API_KEY")
        if key:
            return key
    except Exception:
        pass
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise EnvironmentError("ANTHROPIC_API_KEY not found. Add it to .env: ANTHROPIC_API_KEY=sk-ant-...")


_client = anthropic.Anthropic(api_key=_load_api_key())

TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Execute a BigQuery SELECT query over the demand planning tables. "
            "Returns up to 2,000 rows. Always call get_schema first to confirm "
            "exact column names. Use fully qualified table names:\n"
            "  `euphoric-hull-442815-n8.aera_demand_planning.customer_analysis`\n"
            "  `euphoric-hull-442815-n8.aera_demand_planning.stat_3pd_forecast`"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Valid BigQuery SQL SELECT statement.",
                },
                "label": {
                    "type": "string",
                    "description": "Short human-readable label for this query result (used as table title in UI).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_schema",
        "description": (
            "Return the exact column names and data types for a table from "
            "BigQuery INFORMATION_SCHEMA. Call this before writing SQL to avoid "
            "column name guessing errors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "enum": ["customer_analysis", "stat_3pd_forecast"],
                    "description": "Table to inspect.",
                },
            },
            "required": ["table_name"],
        },
    },
]


def run_agent(messages: list) -> dict:
    """
    Run one user turn through the agent loop.

    Args:
        messages: Full conversation history in Anthropic format (mutated in place).

    Returns:
        {"text": str, "dataframes": list[{"title": str, "df": pd.DataFrame}]}
    """
    dataframes: list[dict] = []
    iterations = 0

    while iterations < MAX_ITERATIONS:
        iterations += 1

        response = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                response = _client.messages.create(
                    model=MODEL,
                    max_tokens=8096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
                break
            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                # Retry rate limits and transient overloads (529)
                status = getattr(e, "status_code", None)
                if status in (429, 500, 529) and attempt < len(_RETRY_DELAYS):
                    continue
                raise

        if response is None:
            raise RuntimeError("Failed after all retries.")

        # Serialize content blocks to plain dicts so session state stays JSON-safe
        assistant_content = []
        text_parts = []
        tool_uses  = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
            elif block.type == "tool_use":
                assistant_content.append({
                    "type":  "tool_use",
                    "id":    block.id,
                    "name":  block.name,
                    "input": block.input,
                })
                tool_uses.append(block)

        messages.append({"role": "assistant", "content": assistant_content})

        # ── Done ─────────────────────────────────────────────────────────────
        if response.stop_reason != "tool_use":
            return {"text": "\n\n".join(text_parts), "dataframes": dataframes}

        # ── Tool calls ────────────────────────────────────────────────────────
        tool_results = []
        for tu in tool_uses:
            args = tu.input or {}

            if tu.name == "run_sql":
                query = args.get("query", "")
                label = args.get("label", query[:60])
                df, error = run_sql(query)

                if error:
                    result_content = f"SQL Error: {error}"
                elif df is None or len(df) == 0:
                    result_content = "Query returned 0 rows."
                else:
                    n            = len(df)
                    preview_rows = min(n, 30)
                    result_content = (
                        f"{n} row(s) returned. "
                        + (f"First {preview_rows} shown:\n" if n > preview_rows else "")
                        + df.head(preview_rows).to_string(index=False)
                    )
                    if n >= 2 or df.shape[1] > 3:
                        dataframes.append({"title": label, "df": df})

            elif tu.name == "get_schema":
                result_content = get_schema(args.get("table_name", ""))

            else:
                result_content = f"Unknown tool: {tu.name}"

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tu.id,
                "content":     result_content,
            })

        messages.append({"role": "user", "content": tool_results})

    return {
        "text":       "Reached maximum iteration limit. Try breaking the question into smaller parts.",
        "dataframes": dataframes,
    }
