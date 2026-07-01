"""
BigQuery fetch functions for the pre-alignment PDF generator.
"""
import datetime
import json
import os
import subprocess

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import credentials as goog_creds

GCP_PROJECT = "euphoric-hull-442815-n8"
DATASET     = "aera_demand_planning"
GCLOUD_ACC  = "jfaizan07@gmail.com"

_ALL_MONTHS      = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_TODAY           = datetime.date.today()
_CUR_MONTH_START = _TODAY.replace(day=1)

CLOSED_2026 = [
    m for m in _ALL_MONTHS
    if datetime.datetime.strptime(f"{m} 2026", "%b %Y").date() < _CUR_MONTH_START
]
OPEN_2026   = [m for m in _ALL_MONTHS if m not in CLOSED_2026]
LAST_CLOSED = CLOSED_2026[-1] if CLOSED_2026 else None


def _client() -> bigquery.Client:
    sa_json = os.getenv("GCP_SA_JSON")
    if sa_json:
        from google.oauth2 import service_account
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery"]
        )
        return bigquery.Client(project=GCP_PROJECT, credentials=creds)
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token", f"--account={GCLOUD_ACC}"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
    return bigquery.Client(project=GCP_PROJECT,
                           credentials=goog_creds.Credentials(token=token))


def fetch_customer_analysis(country: str, sub_segment: str) -> pd.DataFrame:
    """Full customer_analysis slice for one market."""
    q = f"""
        SELECT *
        FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
        WHERE Country_Name = @country
          AND Sub_Segments = @sub_segment
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("country",     "STRING", country),
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
    ])
    return _client().query(q, job_config=cfg).to_dataframe()


def fetch_accuracy(country: str, sub_segment: str) -> pd.DataFrame:
    """lag1_data joined with customer_analysis for accuracy calculations."""
    if not CLOSED_2026:
        return pd.DataFrame()

    client = _client()

    # Only request months whose columns actually exist in lag1_data
    table = client.get_table(f"{GCP_PROJECT}.{DATASET}.lag1_data")
    existing = {f.name for f in table.schema}
    available = [
        m for m in CLOSED_2026
        if f"Fcst3M_{m}_2026" in existing and f"Actual_{m}_2026" in existing
    ]
    if not available:
        return pd.DataFrame()

    lag_cols = ", ".join(
        f"l.Fcst3M_{m}_2026, l.Actual_{m}_2026" for m in available
    )
    q = f"""
        SELECT
            l.Material_Number,
            l.Country_Name,
            l.Customer_Number,
            c.Sub_Brand_Description,
            c.UPC_Code,
            c.Brand_Family,
            {lag_cols}
        FROM `{GCP_PROJECT}.{DATASET}.lag1_data` l
        JOIN `{GCP_PROJECT}.{DATASET}.customer_analysis` c
          ON  l.Material_Number = c.Material_Number
          AND l.Country_Name    = c.Country_Name
          AND l.Customer_Number = c.Customer_Number
        WHERE l.Country_Name = @country
          AND c.Sub_Segments = @sub_segment
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("country",     "STRING", country),
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
    ])
    try:
        return client.query(q, job_config=cfg).to_dataframe()
    except Exception:
        return pd.DataFrame()
