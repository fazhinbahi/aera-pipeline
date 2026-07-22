"""
Fetch AdjFC for 2028 months from Aera.

Uses the same Aera report as fetch_adjfc.py but filters Year = 2028.
Saves: adjfc_2028.parquet, adjfc_2028.csv
"""

import os, sys, time, argparse
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aera_auth import ensure_token

BASE_URL  = "https://becleproximo.aeratechnology.com"
DATA_URL  = (
    f"{BASE_URL}/ispring/awc?v=3"
    "&processID=6C9EBAEB_0F03_4A5D_AF19_7188A3AEA9C7"
    "&ServiceName=ExecuteBIObjectData"
)
DIR       = os.path.dirname(os.path.abspath(__file__))
OUT_PAR   = os.path.join(DIR, "adjfc_2028.parquet")
OUT_CSV   = os.path.join(DIR, "adjfc_2028.csv")
PAGE_SIZE = 2000

YEAR_FIELD = "ED73E1C1-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371"
ACTIVE_FIELD = "F93532CC-EF1E-4F72-AA66-6A2A65598B56_DAB42D7F-6407-4CA3-ADA4-92456C940A47"

REPORT = {
    "bioid":   "01CD4B76_19A3_49A9_8A45_A5CBCEDDDD15",
    "sheetid": "AEAC976F-1BE2-453F-83F6-33428B763610",
    "fid":     "683F21F6_1495_4835_8474_1E09C8BDFB24",
    "row": (
        "4139FB4C-DEA9-4717-B063-1E98D6D47BE2_DAB42D7F-6407-4CA3-ADA4-92456C940A47|,"
        "EB566473-2BA3-4F5B-9B25-45D7A88FABCD_00AD4286-6930-4FA3-A130-1C00B2511569|,"
        "7958218A-DFCF-44D5-8C40-1F995E612B1C_00AD4286-6930-4FA3-A130-1C00B2511569|,"
        "EA082A5B-6405-4ACA-AD0E-D32DCF46C5FA_00AD4286-6930-4FA3-A130-1C00B2511569|,"
        "060CACC0-EC7A-4946-A601-C86E4D69AB29_DAB42D7F-6407-4CA3-ADA4-92456C940A47|,"
        "50191AB8-1EDB-11ED-A548-0A617A24E20D_9EB3B832-5F1A-4FF1-814B-CC82933E9F14|,"
        "7EDF5B81-9D05-4B2B-B941-8A1094782E95_9EB3B832-5F1A-4FF1-814B-CC82933E9F14|,"
        "76DD8280-4FAD-4F4E-B313-5BE498747EA1_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"
        "FA492511-1A35-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"
        "6D8FE76E-1334-45AD-B4DD-9994E6A2B3C5_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"
        "126FD4B7-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|,"
        "F2C3C017-1EDF-11ED-A548-0A617A24E20D_035317EF-A2C0-415E-B864-0F032A347371|,"
        "04E2EDB1-1A36-11ED-A548-0A617A24E20D_454245B2-6AF3-49B8-AA8E-18FEC4E340DC|"
    ),
    "mea": (
        "FC42CB8B-8B96-4966-9EB0-ED0EEB078E62|SUM|||||,"
        "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|||||,"
        "7C3F4114-0B27-451E-8FD4-CFB36349AAA9|SUM|||||,"
        "5709B0A2_5A12_4CCF_A1F8_9C3253A2FA7C|EXPRESSION|||||"
    ),
    # Year = 2028 only (active products)
    "filter": (
        f"{ACTIVE_FIELD}~=|Yes~EN"
        f"^{YEAR_FIELD}~=|2028~EN"
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


def _post(token, jsessionid, payload, lb_instance_id="", retries=8):
    cookies = {"JSESSIONID": jsessionid, "accessToken": token, "token": token}
    headers = {"Authorization": token,
               "Content-Type": "application/x-www-form-urlencoded",
               "Origin": BASE_URL, "Referer": BASE_URL}
    if lb_instance_id:
        cookies["lb-instance-id"] = lb_instance_id
        headers["lb-instance-id"] = lb_instance_id

    for attempt in range(retries):
        try:
            resp = requests.post(DATA_URL, data=payload, headers=headers,
                                 cookies=cookies, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            if resp.status_code == 403 and attempt < retries - 1:
                print(f"    ⚠ 403 — re-logging in…")
                time.sleep(10)
                from aera_auth import login as _login
                fresh = _login()
                token = fresh["access_token"]
                jsessionid = fresh.get("jsessionid", jsessionid)
                lb_instance_id = fresh.get("lb_instance_id", lb_instance_id)
                cookies = {"JSESSIONID": jsessionid, "accessToken": token, "token": token}
                headers["Authorization"] = token
                if lb_instance_id:
                    cookies["lb-instance-id"] = lb_instance_id
                    headers["lb-instance-id"] = lb_instance_id
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"    ⚠ {resp.status_code} — retry in {wait}s…")
                time.sleep(wait)
                continue
            raise
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise


def fetch_2028(token, jsessionid, lb_instance_id="", page_size=PAGE_SIZE):
    base_payload = {
        "sheetid": REPORT["sheetid"], "bioid": REPORT["bioid"], "fid": REPORT["fid"],
        "row": REPORT["row"], "col": "", "mea": REPORT["mea"],
        "filter": REPORT["filter"],
        "pot": "0", "sort": REPORT["sort"], "dir": REPORT["dir"],
        "in_val": "[]", "uom": "", "currency": "", "rate": "",
        "currencyDate": "T", "pivotColSort": "", "source": "report",
        "plimit": str(page_size), "requestID": "PYTHON-ADJFC-2028",
    }

    print("  Fetching page 1 (getting total count)…")
    raw   = _post(token, jsessionid, {**base_payload, "pstart": "0", "page": "1"}, lb_instance_id)
    total = raw.get("totalRows", 0)

    if total == 0:
        print("  No 2028 rows returned — check filter.")
        return pd.DataFrame()

    print(f"  Total 2028 rows: {total:,}")
    fields   = [f["name"] for f in raw["metaData"]["fields"]]
    friendly = [COL_NAMES.get(f, COL_NAMES.get(f.split("|")[0] + "|SUM|",
                COL_NAMES.get(f.split("|")[0] + "|EXPRESSION|", f))) for f in fields]
    all_data = list(raw["data"])
    pages    = (total + page_size - 1) // page_size

    for p in range(1, pages):
        pstart = p * page_size
        pct    = int((p / pages) * 100)
        print(f"  Fetching page {p+1}/{pages}  ({pct}% — {len(all_data):,} rows so far)…")
        time.sleep(0.5)
        raw      = _post(token, jsessionid, {**base_payload, "pstart": str(pstart), "page": str(p+1)}, lb_instance_id)
        all_data.extend(raw["data"])

    df = pd.DataFrame(all_data, columns=friendly)
    for col in ["Actuals", "Adjusted FC", "Adj FC + Actuals", "Adj FC 9LC"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"  Done — {len(df):,} rows loaded.")
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    args = parser.parse_args()

    tok  = ensure_token(min_seconds=300)
    token, jsessionid, lbid = tok["access_token"], tok.get("jsessionid",""), tok.get("lb_instance_id","")

    print("\nFetching 2028 AdjFC from Aera…")
    t0 = time.time()
    df  = fetch_2028(token, jsessionid, lbid, page_size=args.page_size)
    print(f"Time: {time.time()-t0:.1f}s")

    if df.empty:
        return

    months = sorted(df["Month Year"].unique())
    print(f"\nMonths in 2028 data: {months}")
    print(f"Distinct SKUs: {df['Material Number'].nunique():,}")
    print(f"Distinct Countries: {df['Country Name'].nunique():,}")
    print(f"\nSample:\n{df.head(3).to_string()}")

    if not args.no_save:
        df.to_parquet(OUT_PAR, index=False)
        df.to_csv(OUT_CSV, index=False)
        print(f"\n✓ Saved → {OUT_PAR}")
        print(f"✓ Saved → {OUT_CSV}")


if __name__ == "__main__":
    main()
