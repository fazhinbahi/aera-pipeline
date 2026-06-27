"""
Aera API Client — fetches report data directly from Aera's internal API.

HOW IT WORKS:
1. Opens a browser for you to log in once (captures the auth token automatically)
2. Uses that token to call ExecuteBIObjectData for any report
3. Returns clean pandas DataFrames — save to Excel, CSV, or feed into any script

USAGE:
  python3 aera_api.py
  python3 aera_api.py --report distributor
  python3 aera_api.py --output my_data.xlsx
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL   = "https://becleproximo.aeratechnology.com"
TOKEN_URL  = f"{BASE_URL}/ispring/client/oauth/token"
DATA_URL   = f"{BASE_URL}/ispring/awc?v=3&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7&ServiceName=ExecuteBIObjectData"
APP_ID     = "695EB357-4AE4-11ED-BCC9-0A3087F18497"
LOGIN_URL  = f"{BASE_URL}/ispring/p/BAD/dashboard3?ssup=slogin"

# ── Report Configurations ─────────────────────────────────────────────────────
# Each entry defines the exact parameters for an ExecuteBIObjectData call.
# To add a new report: navigate to it in Aera, open DevTools → Network,
# filter for "ExecuteBIObjectData", copy the request body parameters here.

REPORTS = {
    "distributor": {
        "name": "Distributor Report — EMEA IMC Ireland (Brand x Month)",
        "bioid":   "7FAB4732_BAAE_4399_8A85_B9B3444F292C",
        "sheetid": "732DE9BB-1E50-45EB-BC5B-219B4191AFE6",
        "fid":     "683F21F6_1495_4835_8474_1E09C8BDFB24",
        # row dimensions (pipe-separated dimension IDs)
        "row": (
            "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569|,"
            "0F80F189-0F93-4274-8B84-2F1DE0A612FB_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|"
        ),
        # column dimension (date/month)
        "col": "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371",
        # measure (Sales Order Item Quantity 9LC, SUM)
        "mea": "7C3F4114-0B27-451E-8FD4-CFB36349AAA9|SUM|||||",
        # active filters (decoded for readability — script re-encodes)
        "filters": [
            # Only active/current year records
            "F93532CC-EF1E-4F72-AA66-6A2A65598B56_DAB42D7F-6407-4CA3-ADA4-92456C940A47~=|Yes~EN",
            "ED73E1C1-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371~=|var_DSD_CURRENT_YEAR~EN",
            # Business sub-segment = EMEA IMC
            "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569~=|EMEA IMC~EN",
            # Country = Ireland
            "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47~=|Ireland~EN",
        ],
        # friendly column labels (mapped from internal IDs in the response)
        "col_labels": {
            "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569": "Business Sub-Segment",
            "0F80F189-0F93-4274-8B84-2F1DE0A612FB_454245B2-6AF3-49B8-AA8E-18FEC4E340DC": "Brand",
        },
    },
}

# ── Authentication ────────────────────────────────────────────────────────────

def get_token_via_browser(token_file: str = ".aera_token.json") -> dict:
    """
    Opens Aera in a browser window. You log in normally.
    Playwright intercepts the OAuth token AND extracts JSESSIONID (HttpOnly cookie).
    Both are cached — subsequent runs reuse the cache until the token expires.
    """
    # Try cached token first
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                cached = json.load(f)
            if time.time() < cached.get("expires_at", 0) - 60:
                print(f"✓ Using cached token (expires in {int(cached['expires_at'] - time.time())}s)")
                return cached
        except Exception:
            pass

    print("\nOpening Aera in browser — please log in.")
    print("The window will close automatically after login is detected.\n")

    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--window-size=1280,800"])
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        def on_response(response):
            if "/oauth/token" in response.url and response.status == 200:
                try:
                    data = response.json()
                    if "access_token" in data:
                        captured.update(data)
                        captured["expires_at"] = time.time() + 2700  # 45-minute window
                        # Extract JSESSIONID and lb-instance-id (HttpOnly cookies)
                        all_cookies = context.cookies()
                        for c in all_cookies:
                            if c["name"] == "JSESSIONID":
                                captured["jsessionid"] = c["value"]
                            elif c["name"] == "lb-instance-id":
                                captured["lb_instance_id"] = c["value"]
                        print("✓ Auth token and session captured.")
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)

        # Wait up to 3 minutes for login
        deadline = time.time() + 180
        while not captured and time.time() < deadline:
            time.sleep(0.5)

        time.sleep(1)
        browser.close()

    if not captured:
        print("✗ No token captured. Please log in within 3 minutes.")
        sys.exit(1)

    with open(token_file, "w") as f:
        json.dump(captured, f)

    return captured


def refresh_token(refresh_tok: str) -> dict | None:
    """Exchange a refresh token for a new access token."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok, "app_id": APP_ID},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ── Data Fetching ─────────────────────────────────────────────────────────────

def fetch_report(report_key: str, token: str, jsessionid: str = "", page: int = 1, limit: int = 100000) -> dict:
    """Call ExecuteBIObjectData and return the raw JSON response."""
    cfg = REPORTS[report_key]

    filter_str = "^".join(cfg["filters"])

    payload = {
        "sheetid":      cfg["sheetid"],
        "bioid":        cfg["bioid"],
        "fid":          cfg["fid"],
        "row":          cfg["row"],
        "col":          cfg["col"],
        "mea":          cfg["mea"],
        "filter":       filter_str,
        "pot":          "0",
        "sort":         "",
        "dir":          "",
        "in_val":       "[]",
        "uom":          "",
        "currency":     "",
        "rate":         "",
        "currencyDate": "T",
        "pivotColSort": "",
        "pstart":       "0",
        "source":       "report",
        "plimit":       str(limit),
        "page":         str(page),
        "requestID":    "PYTHON-API-CLIENT",
    }

    cookies = {"JSESSIONID": jsessionid, "accessToken": token} if jsessionid else {}

    resp = requests.post(
        DATA_URL,
        data=payload,
        headers={
            "Authorization": token,
            "Content-Type":  "application/x-www-form-urlencoded",
            "Origin":        BASE_URL,
            "Referer":       BASE_URL,
        },
        cookies=cookies,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def to_dataframe(raw: dict, report_key: str) -> pd.DataFrame:
    """Convert raw ExecuteBIObjectData response into a clean pandas DataFrame."""
    cfg = REPORTS[report_key]
    col_labels = cfg.get("col_labels", {})

    fields = [f["name"] for f in raw["metaData"]["fields"]]
    data   = raw["data"]

    # Map internal field IDs to friendly names
    friendly_cols = []
    for f in fields:
        # Date columns look like "Jan 2026~<uuid>|SUM|" — extract just the date
        if "~" in f:
            friendly_cols.append(f.split("~")[0])
        elif f in col_labels:
            friendly_cols.append(col_labels[f])
        else:
            friendly_cols.append(f)

    df = pd.DataFrame(data, columns=friendly_cols)

    # Replace empty strings with NaN, then 0 for numeric columns
    date_cols = [c for c in friendly_cols if any(m in c for m in
                 ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])]
    for col in date_cols:
        df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce").fillna(0)

    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Aera report data via API")
    parser.add_argument("--report",  default="distributor", choices=list(REPORTS.keys()),
                        help="Which report to fetch")
    parser.add_argument("--output",  default=None,
                        help="Output file path (.xlsx or .csv). Default: auto-named.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Force re-login even if a cached token exists")
    args = parser.parse_args()

    if args.no_cache and os.path.exists(".aera_token.json"):
        os.remove(".aera_token.json")

    # 1. Auth
    token_data = get_token_via_browser()
    access_token = token_data["access_token"]
    print(f"\nFetching: {REPORTS[args.report]['name']}")

    # 2. Fetch data
    jsessionid = token_data.get("jsessionid", "")
    raw = fetch_report(args.report, access_token, jsessionid=jsessionid)
    total = raw.get("totalRows", 0)
    print(f"✓ {total} rows returned")

    # 3. Convert to DataFrame
    df = to_dataframe(raw, args.report)
    print(df.to_string(index=False, max_rows=10))
    print(f"\n... ({len(df)} rows total)")

    # 4. Save
    if args.output is None:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        ext = ".xlsx"
        args.output = f"aera_{args.report}_{ts}{ext}"

    out = args.output
    if out.endswith(".csv"):
        df.to_csv(out, index=False)
    else:
        df.to_excel(out, index=False, engine="openpyxl")

    print(f"\n✓ Saved to: {out}")


if __name__ == "__main__":
    main()
