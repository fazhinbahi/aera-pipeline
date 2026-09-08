"""
BigQuery fetch functions for the pre-alignment PDF generator.
"""
import datetime
import json
import os
import subprocess
from typing import Optional

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


def fetch_customers(country: str, sub_segment: str) -> list:
    """Return [(customer_number, display_name), ...] sorted by distributor name.

    Pulls Distributor_Name from adjfc_raw (which always has it) and falls
    back to Customer_Number as the label if the name is missing.
    """
    q = f"""
        SELECT DISTINCT
            Customer_Number,
            COALESCE(NULLIF(TRIM(Distributor_Name), ''), Customer_Number) AS display_name
        FROM `{GCP_PROJECT}.{DATASET}.adjfc_raw`
        WHERE Country_Name  = @country
          AND Sub_Segments  = @sub_segment
          AND Customer_Number IS NOT NULL
          AND Customer_Number != ''
        ORDER BY display_name
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("country",     "STRING", country),
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
    ])
    df = _client().query(q, job_config=cfg).to_dataframe()
    return [(r["Customer_Number"], r["display_name"]) for _, r in df.iterrows()]


def fetch_customer_analysis(country: str, sub_segment: str,
                            customer_numbers: Optional[list] = None) -> pd.DataFrame:
    """Full customer_analysis slice for one market, optionally filtered by customer(s)."""
    customer_clause = "AND Customer_Number IN UNNEST(@customer_numbers)" if customer_numbers else ""
    q = f"""
        SELECT *
        FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
        WHERE Country_Name = @country
          AND Sub_Segments = @sub_segment
          {customer_clause}
    """
    params = [
        bigquery.ScalarQueryParameter("country",     "STRING", country),
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
    ]
    if customer_numbers:
        params.append(bigquery.ArrayQueryParameter("customer_numbers", "STRING", customer_numbers))
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    return _client().query(q, job_config=cfg).to_dataframe()


def fetch_accuracy(country: str, sub_segment: str,
                   customer_numbers: Optional[list] = None) -> pd.DataFrame:
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
    customer_clause = "AND l.Customer_Number IN UNNEST(@customer_numbers)" if customer_numbers else ""
    q = f"""
        WITH ca_cust AS (
            -- customer-level attributes within the requested market
            SELECT Material_Number, Country_Name, Customer_Number,
                   ANY_VALUE(Sub_Brand_Description) AS Sub_Brand_Description,
                   ANY_VALUE(UPC_Code)              AS UPC_Code,
                   ANY_VALUE(Brand_Family)          AS Brand_Family
            FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
            WHERE Country_Name = @country AND Sub_Segments = @sub_segment
            GROUP BY 1, 2, 3
        ),
        ca_any AS (
            -- customers known in ANY sub-segment (to exclude e.g. GTR customers
            -- from an IMC market rather than adopting them as orphans)
            SELECT DISTINCT Material_Number, Country_Name, Customer_Number
            FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
            WHERE Country_Name = @country
        ),
        ca_mat AS (
            -- material-level fallback attributes within the requested market
            SELECT Material_Number, Country_Name,
                   ANY_VALUE(Sub_Brand_Description) AS Sub_Brand_Description,
                   ANY_VALUE(UPC_Code)              AS UPC_Code,
                   ANY_VALUE(Brand_Family)          AS Brand_Family
            FROM `{GCP_PROJECT}.{DATASET}.customer_analysis`
            WHERE Country_Name = @country AND Sub_Segments = @sub_segment
            GROUP BY 1, 2
        )
        SELECT
            l.Material_Number,
            l.Country_Name,
            l.Customer_Number,
            COALESCE(c.Sub_Brand_Description, m.Sub_Brand_Description) AS Sub_Brand_Description,
            COALESCE(c.UPC_Code,              m.UPC_Code)              AS UPC_Code,
            COALESCE(c.Brand_Family,          m.Brand_Family)          AS Brand_Family,
            {lag_cols}
        FROM `{GCP_PROJECT}.{DATASET}.lag1_data` l
        LEFT JOIN ca_cust c
          ON  l.Material_Number = c.Material_Number
          AND l.Country_Name    = c.Country_Name
          AND l.Customer_Number = c.Customer_Number
        LEFT JOIN ca_any x
          ON  l.Material_Number = x.Material_Number
          AND l.Country_Name    = x.Country_Name
          AND l.Customer_Number = x.Customer_Number
        LEFT JOIN ca_mat m
          ON  l.Material_Number = m.Material_Number
          AND l.Country_Name    = m.Country_Name
        WHERE l.Country_Name = @country
          AND (c.Customer_Number IS NOT NULL
               OR (x.Customer_Number IS NULL AND m.Material_Number IS NOT NULL))
          {customer_clause}
    """
    params = [
        bigquery.ScalarQueryParameter("country",     "STRING", country),
        bigquery.ScalarQueryParameter("sub_segment", "STRING", sub_segment),
    ]
    if customer_numbers:
        params.append(bigquery.ArrayQueryParameter("customer_numbers", "STRING", customer_numbers))
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    try:
        return client.query(q, job_config=cfg).to_dataframe()
    except Exception:
        return pd.DataFrame()
