"""
Order History EMEA & APAC fetch pipeline.

Pulls all rows from the "Order History EMEA & APAC - Daily - Power Bi Tracker - Faizan"
report via the Aera API and saves them as a parquet file.

Columns (dimensions):
  Sales Organisation, Business Segment, Region, Sub-Segments, Country Name,
  Category Grouper Description (Z), Brand Family, Sub-Brand Description,
  Volume, UPC Code, Month Year, Material Number, Material Long Description,
  Material Description (Short Text), Material Description (Brand),
  Customer Bill-to Number, Customer Bill-to Sales Number, Customer Bill-to Name,
  SubBrand Description, Customer Sold-to Number, Customer Sold-to Name,
  Category Description (Z), Brand Description

Measure: Order Qty 9LC

Filter: Year >= 2024

Usage:
  python3 fetch_order_history.py
  python3 fetch_order_history.py --no-save
  python3 fetch_order_history.py --page-size 5000
"""

import argparse
import os
import sys
import time

import pandas as pd
import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://becleproximo.aeratechnology.com"
DATA_URL = (
    f"{BASE_URL}/ispring/awc?v=3"
    "&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7"
    "&ServiceName=ExecuteBIObjectData"
)
DIR            = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE     = os.path.join(DIR, ".aera_token.json")
OUTPUT_PARQUET = os.path.join(DIR, "order_history_emea_apac.parquet")

PAGE_SIZE = 2000

# ── Report identifiers ────────────────────────────────────────────────────────

REPORT = {
    "bioid":   "5FBAC812_5510_418E_A817_66EB346D1F01",
    "sheetid": "9DB67DAD-863F-4535-9C97-F7A59A82B8F9",
    "fid":     "E1648832_2435_4C57_9ABD_7BE75EE4FC54",

    # 23 dimension fields — verified from live API capture
    "row": (
        "BD45ACA8-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD|,"  # Sales Organisation
        "EB566473-2BA3-4F5B-9B25-45D7A88FABCD_32B878A0-D32B-44A2-B94D-E540ED9A2BFB|,"  # Region
        "7958218A-DFCF-44D5-8C40-1F995E612B1C_32B878A0-D32B-44A2-B94D-E540ED9A2BFB|,"  # Business Segment
        "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD|,"  # Sub-Segments
        "57C93509-074F-49E4-93C8-878522345DFF_75EF5201-8308-4CEC-9845-EDD085837F71|,"   # Country Name
        "76DD8280-4FAD-4F4E-B313-5BE498747EA1_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Category Grouper Description (Z)
        "FA492511-1A35-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Brand Family
        "0F80F189-0F93-4274-8B84-2F1DE0A612FB_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Sub-Brand Description
        "126FD4B7-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Volume
        "11D64541-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # UPC Code
        "F2C3C017-1EDF-11ED-A548-0A617A24E20D_FD38291C-89BD-43DC-AD7B-ED4E13463F19|,"  # Month Year
        "04E2EDB1-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Material Number
        "0A1C541B-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Material Long Description
        "09CF6C61-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Material Description (Short Text)
        "BCF5D9C6-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD|,"  # Material Description (Brand)
        "50191AB8-1EDB-11ED-A548-0A617A24E20D_A6E1A52A-6FCD-4199-A442-37CFF2337369|,"  # Customer Bill-to Number
        "B8D541CE-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD|,"  # Customer Bill-to Sales Number
        "7EDF5B81-9D05-4B2B-B941-8A1094782E95_A6E1A52A-6FCD-4199-A442-37CFF2337369|,"  # Customer Bill-to Name
        "7975A6D6-6D51-4228-BC42-F049FDE1DE80_75EF5201-8308-4CEC-9845-EDD085837F71|,"  # SubBrand Description
        "50191AB8-1EDB-11ED-A548-0A617A24E20D_BCB9895F-EED4-4DA1-9C53-E6218AE6D04B|,"  # Customer Sold-to Number
        "7EDF5B81-9D05-4B2B-B941-8A1094782E95_BCB9895F-EED4-4DA1-9C53-E6218AE6D04B|,"  # Customer Sold-to Name
        "DC111F6A-6C39-4684-891B-3096FECB0F20_021EAF86-1F5B-483C-A38D-87F34EACA5D6|,"  # Category Description (Z)
        "4C97818A-1EDB-11ED-A548-0A617A24E20D_A6E1A52A-6FCD-4199-A442-37CFF2337369|"   # Brand Description
    ),

    # Measure: Order Qty 9LC (expression field in the report)
    "mea": "70FA3ADC-5626-4476-8CB3-5AD7B6D7BF45|EXPRESSION|||||",

    # Filter: Year >= 2024
    "filter": (
        "ED73E1C1-1EDF-11ED-A548-0A617A24E20D_FD38291C-89BD-43DC-AD7B-ED4E13463F19~>=|2024~EN"
    ),
}

# Field ID → friendly column name (positionally confirmed from live data)
COL_NAMES = {
    "BD45ACA8-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD": "Sales Organisation",
    "EB566473-2BA3-4F5B-9B25-45D7A88FABCD_32B878A0-D32B-44A2-B94D-E540ED9A2BFB": "Region",
    "7958218A-DFCF-44D5-8C40-1F995E612B1C_32B878A0-D32B-44A2-B94D-E540ED9A2BFB": "Business Segment",
    "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD": "Sub-Segments",
    "57C93509-074F-49E4-93C8-878522345DFF_75EF5201-8308-4CEC-9845-EDD085837F71":  "Country Name",
    "76DD8280-4FAD-4F4E-B313-5BE498747EA1_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Category Grouper Description (Z)",
    "FA492511-1A35-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Brand Family",
    "0F80F189-0F93-4274-8B84-2F1DE0A612FB_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Sub-Brand Description",
    "126FD4B7-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Volume",
    "11D64541-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "UPC Code",
    "F2C3C017-1EDF-11ED-A548-0A617A24E20D_FD38291C-89BD-43DC-AD7B-ED4E13463F19": "Month Year",
    "04E2EDB1-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Material Number",
    "0A1C541B-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Material Long Description",
    "09CF6C61-1A36-11ED-A548-0A617A24E20D_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Material Description (Short Text)",
    "BCF5D9C6-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD": "Material Description (Brand)",
    "50191AB8-1EDB-11ED-A548-0A617A24E20D_A6E1A52A-6FCD-4199-A442-37CFF2337369": "Customer Bill-to Number",
    "B8D541CE-1EDB-11ED-A548-0A617A24E20D_68563EB3-1A6B-4FC7-9EF2-2BCC77BE57CD": "Customer Bill-to Sales Number",
    "7EDF5B81-9D05-4B2B-B941-8A1094782E95_A6E1A52A-6FCD-4199-A442-37CFF2337369": "Customer Bill-to Name",
    "7975A6D6-6D51-4228-BC42-F049FDE1DE80_75EF5201-8308-4CEC-9845-EDD085837F71": "SubBrand Description",
    "50191AB8-1EDB-11ED-A548-0A617A24E20D_BCB9895F-EED4-4DA1-9C53-E6218AE6D04B": "Customer Sold-to Number",
    "7EDF5B81-9D05-4B2B-B941-8A1094782E95_BCB9895F-EED4-4DA1-9C53-E6218AE6D04B": "Customer Sold-to Name",
    "DC111F6A-6C39-4684-891B-3096FECB0F20_021EAF86-1F5B-483C-A38D-87F34EACA5D6": "Category Description (Z)",
    "4C97818A-1EDB-11ED-A548-0A617A24E20D_A6E1A52A-6FCD-4199-A442-37CFF2337369": "Brand Description",
    # Measure
    "70FA3ADC-5626-4476-8CB3-5AD7B6D7BF45|EXPRESSION|":                           "Order Qty 9LC",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

sys.path.insert(0, DIR)
from aera_auth import ensure_token, login as _aera_login


def load_token() -> dict:
    return ensure_token(min_seconds=300)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(token: str, jsessionid: str, payload: dict,
          lb_instance_id: str = "", retries: int = 8) -> dict:
    def _cookies(tok, sid, lbid):
        c = {"JSESSIONID": sid, "accessToken": tok, "token": tok}
        if lbid:
            c["lb-instance-id"] = lbid
        return c

    def _headers(tok, lbid):
        h = {
            "Authorization": tok,
            "Content-Type":  "application/x-www-form-urlencoded",
            "Origin":        BASE_URL,
            "Referer":       BASE_URL,
        }
        if lbid:
            h["lb-instance-id"] = lbid
        return h

    cookies = _cookies(token, jsessionid, lb_instance_id)
    headers = _headers(token, lb_instance_id)

    for attempt in range(retries):
        try:
            resp = requests.post(DATA_URL, data=payload, headers=headers,
                                 cookies=cookies, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            if resp.status_code == 403 and attempt < retries - 1:
                print(f"    ⚠ 403 — re-logging in (attempt {attempt + 1})...")
                time.sleep(10)
                fresh          = _aera_login()
                token          = fresh["access_token"]
                jsessionid     = fresh.get("jsessionid", jsessionid)
                lb_instance_id = fresh.get("lb_instance_id", lb_instance_id)
                cookies = _cookies(token, jsessionid, lb_instance_id)
                headers = _headers(token, lb_instance_id)
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
        "sort":         "",
        "dir":          "",
        "in_val":       "[]",
        "uom":          "",
        "currency":     "",
        "rate":         "",
        "currencyDate": "T",
        "source":       "report",
        "plimit":       str(page_size),
        "requestID":    "PYTHON-ORDER-HISTORY-FETCH",
    }

    print("  Fetching page 1 (getting total row count)...")
    payload = {**base_payload, "pstart": "0", "page": "1"}
    raw     = _post(token, jsessionid, payload, lb_instance_id=lb_instance_id)
    total   = raw.get("totalRows", 0)

    if total == 0:
        print("  No rows returned — check filter or token.")
        return pd.DataFrame()

    print(f"  Total rows: {total:,}")

    fields   = [f["name"] for f in raw["metaData"]["fields"]]
    friendly = [
        COL_NAMES.get(f, COL_NAMES.get(f.split("|")[0] + "|EXPRESSION|", f))
        for f in fields
    ]

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
    parser = argparse.ArgumentParser(description="Fetch Order History EMEA&APAC from Aera")
    parser.add_argument("--no-save",   action="store_true",
                        help="Skip saving parquet (print info only)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help=f"Rows per API call (default: {PAGE_SIZE})")
    args = parser.parse_args()

    tok            = load_token()
    token          = tok["access_token"]
    jsessionid     = tok.get("jsessionid", "")
    lb_instance_id = tok.get("lb_instance_id", "")

    print("\nFetching Order History EMEA&APAC (Year >= 2024)...")
    t0 = time.time()
    df = fetch_all_rows(token, jsessionid, lb_instance_id, page_size=args.page_size)
    elapsed = time.time() - t0

    if df.empty:
        print("No data returned.")
        return

    # Convert numeric columns
    df["Order Qty 9LC"] = pd.to_numeric(df["Order Qty 9LC"], errors="coerce")
    df["Volume"]        = pd.to_numeric(df["Volume"],        errors="coerce")

    print(f"\n{'─' * 60}")
    print(f"Rows:    {len(df):,}")
    print(f"Columns: {list(df.columns)}")
    print(f"Time:    {elapsed:.1f}s")
    print(f"{'─' * 60}")
    print(df.head(3).to_string())

    if not args.no_save:
        df.to_parquet(OUTPUT_PARQUET, index=False)
        print(f"\n✓ Saved → {OUTPUT_PARQUET}")
        print(f"  Shape: {df.shape[0]:,} rows × {df.shape[1]} cols")
        print("Next: python3 upload_sply_compare.py")

    return df


if __name__ == "__main__":
    df = main()
