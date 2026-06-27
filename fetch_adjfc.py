"""
Adj FC + Actuals EA fetch pipeline.

Pulls all rows from the "Adj FC + Actuals EA - Scott (New Zealand) - Faizan"
report via the Aera API and loads them as a pandas DataFrame (df_adjfc).

Columns (13 dimensions + 4 measures):
  Organisation, Region, Business Segment, Sub-Segments, Country Name,
  Customer Number, Distributor Name, Category Grouper Description (Z),
  External Material Group Description, Sub-Brand Long Description,
  Volume, Month Year, Material Number,
  Actuals, Adjusted FC, Adj FC + Actuals, Adj FC 9LC

Base filter applied (report-level, ~254k rows):
  - Active flag = Yes
  - Year = current year | next year (var_DSD_CURRENT_YEAR | var_DSD_NEXT_YEAR)

Usage:
  python3 fetch_adjfc.py                 # fetch all, save parquet + csv
  python3 fetch_adjfc.py --no-save       # fetch only, print summary
  python3 fetch_adjfc.py --page-size 5000
"""

import argparse
import json
import os
import sys
import time

import pandas as pd
import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL      = "https://becleproximo.aeratechnology.com"
DATA_URL      = (
    f"{BASE_URL}/ispring/awc?v=3"
    "&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7"
    "&ServiceName=ExecuteBIObjectData"
)
TOKEN_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aera_token.json")
OUTPUT_PARQUET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adjfc_nz.parquet")
OUTPUT_CSV     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adjfc_nz.csv")

PAGE_SIZE = 2000

# ── Report identifiers ────────────────────────────────────────────────────────

REPORT = {
    "bioid":   "01CD4B76_19A3_49A9_8A45_A5CBCEDDDD15",
    "sheetid": "AEAC976F-1BE2-453F-83F6-33428B763610",
    "fid":     "683F21F6_1495_4835_8474_1E09C8BDFB24",

    # 15 dimensions in report order
    "row": (
        "4139FB4C-DEA9-4717-B063-1E98D6D47BE2_DAB42D7F-6407-4CA3-ADA4-92456C940A47|,"   # Organisation
        "EB566473-2BA3-4F5B-9B25-45D7A88FABCD_00AD4286-6930-4FA3-A130-1C00B2511569|,"   # Region
        "7958218A-DFCF-44D5-8C40-1F995E612B1C_00AD4286-6930-4FA3-A130-1C00B2511569|,"   # Business Segment
        "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569|,"   # Sub-Segments
        "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47|,"   # Country Name
        "50191AB8-1EDB-11ED-A548-0A617A24E20D_9EB3B832-5F1A-4FF1-814B-CC82933E9F14|,"   # Customer Number
        "7EDF5B81-9D05-4B2B-B941-8A1094782E95_9EB3B832-5F1A-4FF1-814B-CC82933E9F14|,"   # Distributor Name
        "76DD8280-4FAD-4F4E-B313-5BE498747EA1_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"   # Category Grouper Description (Z)
        "FA492511-1A35-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"   # External Material Group Description
        "6D8FE76E-1334-45AD-B4DD-9994E6A2B3C5_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"   # Sub-Brand Long Description
        "126FD4B7-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"   # Volume
        "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371|,"   # Month Year
        "04E2EDB1-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|"    # Material Number
    ),

    # 4 measures
    "mea": (
        "FC42CB8B-8B96-4966-9EB0-ED0EEB078E62|SUM|||||,"      # Actuals
        "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|||||,"      # Adjusted FC
        "7C3F4114-0B27-451E-8FD4-CFB36349AAA9|SUM|||||,"      # Adj FC + Actuals
        "5709B0A2_5A12_4CCF_A1F8_9C3253A2FA7C|EXPRESSION|||||"  # Adj FC 9LC
    ),

    # Base report filter (no material/country drill-down)
    "filter": (
        "F93532CC-EF1E-4F72-AA66-6A2A65598B56_DAB42D7F-6407-4CA3-ADA4-92456C940A47~=|Yes~EN"
        "^ED73E1C1-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371~=|var_DSD_CURRENT_YEAR|var_DSD_NEXT_YEAR~EN"
    ),

    "sort": "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371",
    "dir":  "ASC",
}

COL_NAMES = {
    "4139FB4C-DEA9-4717-B063-1E98D6D47BE2_DAB42D7F-6407-4CA3-ADA4-92456C940A47": "Organisation",
    "EB566473-2BA3-4F5B-9B25-45D7A88FABCD_00AD4286-6930-4FA3-A130-1C00B2511569": "Region",
    "7958218A-DFCF-44D5-8C40-1F995E612B1C_00AD4286-6930-4FA3-A130-1C00B2511569": "Business Segment",
    "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569": "Sub-Segments",
    "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47": "Country Name",
    "50191AB8-1EDB-11ED-A548-0A617A24E20D_9EB3B832-5F1A-4FF1-814B-CC82933E9F14": "Customer Number",
    "7EDF5B81-9D05-4B2B-B941-8A1094782E95_9EB3B832-5F1A-4FF1-814B-CC82933E9F14": "Distributor Name",
    "76DD8280-4FAD-4F4E-B313-5BE498747EA1_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "Category Grouper Description (Z)",
    "FA492511-1A35-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "External Material Group Description",
    "6D8FE76E-1334-45AD-B4DD-9994E6A2B3C5_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "Sub-Brand Long Description",
    "126FD4B7-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "Volume",
    "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371": "Month Year",
    "04E2EDB1-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "Material Number",
    "FC42CB8B-8B96-4966-9EB0-ED0EEB078E62|SUM|":                                  "Actuals",
    "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|":                                  "Adjusted FC",
    "7C3F4114-0B27-451E-8FD4-CFB36349AAA9|SUM|":                                  "Adj FC + Actuals",
    "5709B0A2_5A12_4CCF_A1F8_9C3253A2FA7C|EXPRESSION|":                           "Adj FC 9LC",
}


# ── Auth (delegated to aera_auth.py) ─────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aera_auth import ensure_token, login as _aera_login

def load_token() -> dict:
    return ensure_token(min_seconds=300)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(token: str, jsessionid: str, payload: dict, lb_instance_id: str = "",
          retries: int = 8) -> dict:
    def _make_cookies(tok, sid, lbid):
        c = {"JSESSIONID": sid, "accessToken": tok, "token": tok}
        if lbid:
            c["lb-instance-id"] = lbid
        return c

    def _make_headers(tok, lbid):
        h = {
            "Authorization": tok,
            "Content-Type":  "application/x-www-form-urlencoded",
            "Origin":        BASE_URL,
            "Referer":       BASE_URL,
        }
        if lbid:
            h["lb-instance-id"] = lbid
        return h

    cookies = _make_cookies(token, jsessionid, lb_instance_id)
    headers = _make_headers(token, lb_instance_id)

    for attempt in range(retries):
        try:
            resp = requests.post(DATA_URL, data=payload, headers=headers,
                                 cookies=cookies, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            if resp.status_code == 403 and attempt < retries - 1:
                print(f"    ⚠ 403 — re-logging in (attempt {attempt + 1}/{retries})...")
                time.sleep(10)
                fresh          = _aera_login()
                token          = fresh["access_token"]
                jsessionid     = fresh.get("jsessionid", jsessionid)
                lb_instance_id = fresh.get("lb_instance_id", lb_instance_id)
                cookies = _make_cookies(token, jsessionid, lb_instance_id)
                headers = _make_headers(token, lb_instance_id)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"    ⚠ {resp.status_code} — retry in {wait}s...")
                time.sleep(wait)
                continue
            raise
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all_rows(token: str, jsessionid: str, lb_instance_id: str = "",
                   page_size: int = PAGE_SIZE) -> pd.DataFrame:
    base_payload = {
        "sheetid":      REPORT["sheetid"],
        "bioid":        REPORT["bioid"],
        "fid":          REPORT["fid"],
        "row":          REPORT["row"],
        "col":          "",
        "mea":          REPORT["mea"],
        "filter":       REPORT["filter"],
        "pot":          "0",
        "sort":         REPORT["sort"],
        "dir":          REPORT["dir"],
        "in_val":       "[]",
        "uom":          "",
        "currency":     "",
        "rate":         "",
        "currencyDate": "T",
        "pivotColSort": "",
        "source":       "report",
        "plimit":       str(page_size),
        "requestID":    "PYTHON-ADJFC-FETCH",
    }

    print("  Fetching page 1 (getting total count)...")
    payload = {**base_payload, "pstart": "0", "page": "1"}
    raw = _post(token, jsessionid, payload, lb_instance_id=lb_instance_id)
    total = raw.get("totalRows", 0)

    if total == 0:
        print("  No rows returned — check filter or token.")
        return pd.DataFrame()

    print(f"  Total rows: {total:,}")

    fields   = [f["name"] for f in raw["metaData"]["fields"]]
    friendly = [COL_NAMES.get(f, COL_NAMES.get(f.split("|")[0] + "|SUM|",
                COL_NAMES.get(f.split("|")[0] + "|EXPRESSION|", f))) for f in fields]

    all_data = list(raw["data"])
    pages    = (total + page_size - 1) // page_size

    for p in range(1, pages):
        pstart = p * page_size
        pct    = int((p / pages) * 100)
        print(f"  Fetching page {p + 1}/{pages}  ({pct}% — {len(all_data):,} rows so far)...")
        time.sleep(0.5)
        payload = {**base_payload, "pstart": str(pstart), "page": str(p + 1)}
        raw     = _post(token, jsessionid, payload, lb_instance_id=lb_instance_id)
        all_data.extend(raw["data"])

    df = pd.DataFrame(all_data, columns=friendly)
    print(f"  Done — {len(df):,} rows loaded.")
    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Adj FC + Actuals EA data from Aera")
    parser.add_argument("--no-save",   action="store_true",
                        help="Skip saving files (print DataFrame info only)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help=f"Rows per API call (default: {PAGE_SIZE})")
    args = parser.parse_args()

    tok            = load_token()
    token          = tok["access_token"]
    jsessionid     = tok.get("jsessionid", "")
    lb_instance_id = tok.get("lb_instance_id", "")

    print("\nFetching Adj FC + Actuals EA (New Zealand) report...")
    t0 = time.time()
    df_adjfc = fetch_all_rows(token, jsessionid, lb_instance_id, page_size=args.page_size)
    elapsed  = time.time() - t0

    if df_adjfc.empty:
        print("No data returned.")
        return

    print(f"\n{'─' * 60}")
    print(f"Rows:    {len(df_adjfc):,}")
    print(f"Columns: {list(df_adjfc.columns)}")
    print(f"Time:    {elapsed:.1f}s")
    print(f"{'─' * 60}")
    print(df_adjfc.head(3).to_string())

    if not args.no_save:
        # Backup existing parquet as "previous month" before overwriting
        PREV_PARQUET = OUTPUT_PARQUET.replace(".parquet", "_prev_month.parquet")
        if os.path.exists(OUTPUT_PARQUET):
            import shutil
            shutil.copy2(OUTPUT_PARQUET, PREV_PARQUET)
            print(f"  Backed up previous parquet → {os.path.basename(PREV_PARQUET)}")

        # Coerce measure columns to numeric (EXPRESSION measure can return '' for some rows)
        measure_cols = ["Actuals", "Adjusted FC", "Adj FC + Actuals", "Adj FC 9LC"]
        for col in measure_cols:
            if col in df_adjfc.columns:
                df_adjfc[col] = pd.to_numeric(df_adjfc[col], errors="coerce")
        df_adjfc.to_parquet(OUTPUT_PARQUET, index=False)
        df_adjfc.to_csv(OUTPUT_CSV, index=False)
        print(f"\n✓ Saved → {OUTPUT_PARQUET}")
        print(f"✓ Saved → {OUTPUT_CSV}")

    return df_adjfc


if __name__ == "__main__":
    df_adjfc = main()
