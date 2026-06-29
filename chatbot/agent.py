"""
Agentic loop: OpenAI GPT-4o-mini with tool use over BigQuery demand planning data.
"""

import json
import os
import time
from pathlib import Path

from openai import OpenAI, RateLimitError
import pandas as pd

from schema import SYSTEM_PROMPT
from tools import run_sql, get_schema

MODEL          = "gpt-4.1"
MAX_ITERATIONS = 15
_RETRY_DELAYS  = [10, 20, 40]


def _load_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise EnvironmentError("OPENAI_API_KEY not found. Add it to .env: OPENAI_API_KEY=sk-proj-...")


_client = OpenAI(api_key=_load_api_key())

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Execute a BigQuery SELECT query over the demand planning tables. "
                "Returns up to 2,000 rows. Always call get_schema first to confirm "
                "exact column names. Use fully qualified table names:\n"
                "  `euphoric-hull-442815-n8.aera_demand_planning.customer_analysis`\n"
                "  `euphoric-hull-442815-n8.aera_demand_planning.stat_3pd_forecast`\n"
                "  `euphoric-hull-442815-n8.aera_demand_planning.lag1_data`"
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": (
                "Return the exact column names and data types for a table from "
                "BigQuery INFORMATION_SCHEMA. Call this before writing SQL to avoid "
                "column name guessing errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "enum": ["customer_analysis", "stat_3pd_forecast", "lag1_data"],
                        "description": "Table to inspect.",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
]


def run_agent(messages: list) -> dict:
    """
    Run one user turn through the agent loop.

    Args:
        messages: Full conversation history in OpenAI format (mutated in place).

    Returns:
        {"text": str, "dataframes": list[{"title": str, "df": pd.DataFrame}]}
    """
    dataframes: list[dict] = []
    iterations = 0

    # System prompt prepended for every API call but not stored in session state
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    while iterations < MAX_ITERATIONS:
        iterations += 1

        response = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                response = _client.chat.completions.create(
                    model=MODEL,
                    max_tokens=8096,
                    tools=TOOLS,
                    parallel_tool_calls=False,
                    messages=full_messages,
                )
                break
            except RateLimitError:
                if attempt == len(_RETRY_DELAYS):
                    raise
                continue
            except Exception as e:
                # Retry on tool_use_failed — model occasionally generates wrong format
                if "tool_use_failed" in str(e) and attempt < len(_RETRY_DELAYS):
                    time.sleep(3)
                    continue
                raise

        if response is None:
            raise RuntimeError("Failed after all retries.")

        choice = response.choices[0]
        msg    = choice.message

        # Build assistant message for history
        assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        full_messages.append(assistant_msg)
        messages.append(assistant_msg)

        # ── Done ─────────────────────────────────────────────────────────────
        if choice.finish_reason == "stop":
            return {"text": msg.content or "", "dataframes": dataframes}

        # ── Tool calls ────────────────────────────────────────────────────────
        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                if name == "run_sql":
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

                elif name == "get_schema":
                    result_content = get_schema(args.get("table_name", ""))

                else:
                    result_content = f"Unknown tool: {name}"

                tool_msg = {
                    "role":         "tool",
                    "content":      result_content,
                    "tool_call_id": tc.id,
                }
                full_messages.append(tool_msg)
                messages.append(tool_msg)

        else:
            return {
                "text":       msg.content or f"Stopped unexpectedly ({choice.finish_reason}).",
                "dataframes": dataframes,
            }

    return {
        "text":       "Reached maximum iteration limit. Try breaking the question into smaller parts.",
        "dataframes": dataframes,
    }
