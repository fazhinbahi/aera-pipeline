"""
BigQuery tool implementations for the demand planning agent.
"""

import json
import os
import re
import subprocess

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import credentials

GCP_PROJECT  = "euphoric-hull-442815-n8"
DATASET      = "aera_demand_planning"
GCLOUD_ACC   = "jfaizan07@gmail.com"
MAX_ROWS     = 2000   # hard cap on rows returned to agent

VALID_TABLES = {"customer_analysis", "stat_3pd_forecast", "lag1_data"}


def _bq_client() -> bigquery.Client:
    # Cloud/CI: use service account JSON from env var
    sa_json = os.getenv("GCP_SA_JSON")
    if sa_json:
        from google.oauth2 import service_account
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery"]
        )
        return bigquery.Client(project=GCP_PROJECT, credentials=creds)
    # Local dev fallback: gcloud CLI
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
    creds = credentials.Credentials(token=token)
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


def run_sql(query: str) -> tuple[pd.DataFrame | None, str | None]:
    """
    Execute a SELECT query on BigQuery.
    Returns (DataFrame, None) on success or (None, error_string) on failure.
    Automatically adds LIMIT if absent.
    """
    q = query.strip().rstrip(";")

    if not re.match(r"^\s*SELECT\b", q, re.IGNORECASE):
        return None, "Only SELECT queries are permitted."

    # Add row cap if no LIMIT present
    if not re.search(r"\bLIMIT\b", q, re.IGNORECASE):
        q += f"\nLIMIT {MAX_ROWS}"

    try:
        client = _bq_client()
        df = client.query(q).to_dataframe()
        return df, None
    except Exception as exc:
        return None, str(exc)


def get_schema(table_name: str) -> str:
    """
    Return the exact column names and types for a table by querying
    INFORMATION_SCHEMA — guaranteed accurate, no guessing.
    """
    if table_name not in VALID_TABLES:
        return f"Unknown table '{table_name}'. Valid options: {', '.join(sorted(VALID_TABLES))}"

    query = f"""
        SELECT column_name, data_type, ordinal_position
        FROM `{GCP_PROJECT}.{DATASET}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table_name}'
        ORDER BY ordinal_position
    """
    try:
        client = _bq_client()
        df = client.query(query).to_dataframe()
        lines = [f"Table `{table_name}` — {len(df)} columns:\n"]
        for _, row in df.iterrows():
            lines.append(f"  {row['column_name']}  ({row['data_type']})")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching schema: {exc}"
