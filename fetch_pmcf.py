"""
Fetch Previous Month Consensus Forecast (PMCF) from Aera Forecast Adjustments.

Calls the Forecast Adjustments skill (handler=Start) and parses the
"Previous Month Consensus Forecast*" row to get monthly values for
the full EMEA & APAC portfolio (all categories, no filter).

Output: pmcf_monthly.parquet — columns: Month Year, PMCF

Usage:
  python3 fetch_pmcf.py
  python3 fetch_pmcf.py --no-save
"""

import argparse
import os
import re
import sys
import time

import pandas as pd
import requests

DIR        = os.path.dirname(os.path.abspath(__file__))
OUTPUT     = os.path.join(DIR, "pmcf_monthly.parquet")
BASE_URL   = "https://becleproximo.aeratechnology.com"
PROCESS_ID = "65FDAE33_AD16_45D5_B0DB_EB7ACC5B9201"
START_URL  = f"{BASE_URL}/ispring/Default?handler=Start&processID={PROCESS_ID}&__requestjson__=true"

sys.path.insert(0, DIR)
from aera_auth import ensure_token


def _headers_cookies(tok):
    token     = tok["access_token"]
    jsession  = tok.get("jsessionid", "")
    lb        = tok.get("lb_instance_id", "")
    cookies   = {"JSESSIONID": jsession, "accessToken": token, "token": token}
    headers   = {"Authorization": token, "Accept": "*/*", "Origin": BASE_URL, "Referer": BASE_URL}
    if lb:
        cookies["lb-instance-id"] = lb
        headers["lb-instance-id"] = lb
    return headers, cookies


# fieldName → human month abbreviation
FIELD_TO_MONTH = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar",  "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul",  "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov",  "DECE": "Dec",
    "JAN_NEXT_YEAR": "Jan", "FEB_NEXT_YEAR": "Feb", "MAR_NEXT_YEAR": "Mar",
    "APR_NEXT_YEAR": "Apr", "MAY_NEXT_YEAR": "May", "JUN_NEXT_YEAR": "Jun",
    "JUL_NEXT_YEAR": "Jul", "AUG_NEXT_YEAR": "Aug", "SEP_NEXT_YEAR": "Sep",
    "OCT_NEXT_YEAR": "Oct", "NOV_NEXT_YEAR": "Nov", "DECE_NEXT_YEAR": "Dec",
}


def _parse_pmcf(text: str) -> pd.DataFrame:
    """Extract the PMCF row values from the handler=Start JSON blob."""
    pmcf_idx = text.find("Previous Month Consensus Forecast")
    if pmcf_idx < 0:
        raise ValueError("'Previous Month Consensus Forecast' row not found in response")

    # Take enough context: the row extends ~4 KB after the label
    ctx = text[pmcf_idx : pmcf_idx + 5000]

    # Use current calendar year from system clock
    from datetime import datetime
    current_year = datetime.now().year
    next_year    = current_year + 1

    records = []
    for field_name, month_abbr in FIELD_TO_MONTH.items():
        pattern = rf'"fieldName":"{field_name}","value":"([^"]+)"'
        m = re.search(pattern, ctx)
        if not m:
            continue
        val_str = m.group(1).replace(",", "")
        try:
            val = float(val_str)
        except ValueError:
            continue
        year = next_year if "NEXT_YEAR" in field_name else current_year
        records.append({"Month Year": f"{month_abbr} {year}", "PMCF": val})

    return pd.DataFrame(records)


def fetch_pmcf() -> pd.DataFrame:
    tok             = ensure_token(min_seconds=300)
    headers, cookies = _headers_cookies(tok)

    print("Fetching Previous Month Consensus Forecast from Aera...")
    t0   = time.time()
    resp = requests.get(START_URL, headers=headers, cookies=cookies, timeout=90)
    resp.raise_for_status()
    elapsed = time.time() - t0
    print(f"  Response: {len(resp.text):,} chars in {elapsed:.1f}s")

    df = _parse_pmcf(resp.text)
    print(f"  Parsed {len(df)} monthly PMCF values:")
    for _, row in df.iterrows():
        print(f"    {row['Month Year']:12s}: {row['PMCF']:>12,.0f}")

    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    df = fetch_pmcf()

    if not args.no_save:
        df.to_parquet(OUTPUT, index=False)
        print(f"\n✓ Saved → {OUTPUT}")
    else:
        print("\n(--no-save: parquet not written)")

    return df


if __name__ == "__main__":
    main()
