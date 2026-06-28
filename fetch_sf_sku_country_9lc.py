"""
Fetch SF / 3PD / Source Forecast in 9LC at Material × Country grain
by applying BOTH SKU and Country filters simultaneously in Aera SSR.

Strategy:
  1. From forecast_3yr, identify all non-zero SKU × Country pairs (~2,829)
  2. For each pair POST handler=Next with both SKU + Country filters active
     → Aera returns exact 9LC totals for that SKU in that country (no allocation)
  3. Allocate to Sub-Segments within each SKU × Country using forecast_3yr
     proportions — UOM-safe because conversion factor is constant per SKU
  4. Save sf_9lc_grain.parquet

Why this is better than all previous approaches:
  - Country-level approach (67 calls):   exact country, approximate per-SKU
  - Per-SKU approach (1,131 calls):      exact per-SKU, approximate per-country
  - This approach (2,829 calls):         exact per-SKU AND per-country  ✓
  - Full cross approach (76k calls):     not needed — most pairs are zero
"""

import base64
import json
import os
import re
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aera_auth import ensure_token

DIR      = os.path.dirname(os.path.abspath(__file__))
BASE     = "https://becleproximo.aeratechnology.com"
PROC_ID  = "65FDAE33_AD16_45D5_B0DB_EB7ACC5B9201"
START_URL = f"{BASE}/ispring/Default?handler=Start&processID={PROC_ID}"

COUNTRY_FILTER_FIELD = "169678935383300"
SKU_FILTER_FIELD     = "90484966"

MONTHS_LABELS    = ["JAN","FEB","MAR","APR","MAY","JUN",
                    "JUL","AUG","SEP","OCT","NOV","DECE"]
NEXT_YEAR_LABELS = ["JAN_NEXT_YEAR","FEB_NEXT_YEAR","MAR_NEXT_YEAR","APR_NEXT_YEAR",
                    "MAY_NEXT_YEAR","JUN_NEXT_YEAR","JUL_NEXT_YEAR","AUG_NEXT_YEAR",
                    "SEP_NEXT_YEAR","OCT_NEXT_YEAR","NOV_NEXT_YEAR","DECE_NEXT_YEAR"]
ALL_FIELD_LABELS = MONTHS_LABELS + NEXT_YEAR_LABELS

MONTH_MAP = {
    "JAN":"Jan 2026","FEB":"Feb 2026","MAR":"Mar 2026","APR":"Apr 2026",
    "MAY":"May 2026","JUN":"Jun 2026","JUL":"Jul 2026","AUG":"Aug 2026",
    "SEP":"Sep 2026","OCT":"Oct 2026","NOV":"Nov 2026","DECE":"Dec 2026",
    "JAN_NEXT_YEAR":"Jan 2027","FEB_NEXT_YEAR":"Feb 2027","MAR_NEXT_YEAR":"Mar 2027",
    "APR_NEXT_YEAR":"Apr 2027","MAY_NEXT_YEAR":"May 2027","JUN_NEXT_YEAR":"Jun 2027",
    "JUL_NEXT_YEAR":"Jul 2027","AUG_NEXT_YEAR":"Aug 2027","SEP_NEXT_YEAR":"Sep 2027",
    "OCT_NEXT_YEAR":"Oct 2027","NOV_NEXT_YEAR":"Nov 2027","DECE_NEXT_YEAR":"Dec 2027",
}
ALL_MONTHS = list(MONTH_MAP.values())

COUNTRY_CODES = {
    "Andorra":"AD","Australia":"AU","Austria":"AT","Belgium":"BE",
    "Bulgaria":"BG","Cameroon":"CM","China":"CN","Croatia":"HR",
    "Cyprus":"CY","Czech Republic":"CZ","Estonia":"EE","France":"FR",
    "Georgia":"GE","Germany":"DE","Ghana":"GH","Gibraltar":"GI",
    "Greece":"GR","Guam":"GU","Hong Kong":"HK","Hungary":"HU",
    "Iceland":"IS","India":"IN","Indonesia":"ID","Iraq":"IQ",
    "Ireland":"IE","Israel":"IL","Italy":"IT","Japan":"JP",
    "Jordan":"JO","Kenya":"KE","Kuwait":"KW","Latvia":"LV",
    "Lebanon":"LB","Lithuania":"LT","Luxembourg":"LU","Malaysia":"MY",
    "Malta":"MT","Mexico":"MX","Morocco":"MA","Netherlands":"NL",
    "New Zealand":"NZ","Nigeria":"NG","Norway":"NO","Oman":"OM",
    "Pakistan":"PK","Philippines":"PH","Poland":"PL","Portugal":"PT",
    "Qatar":"QA","Romania":"RO","Saudi Arabia":"SA","Serbia":"RS",
    "Singapore":"SG","Slovakia":"SK","Slovenia":"SI","South Africa":"ZA",
    "Spain":"ES","Sri Lanka":"LK","Sweden":"SE","Switzerland":"CH",
    "Taiwan":"TW","Thailand":"TH","Tunisia":"TN","Turkey":"TR",
    "United Arab Emirates":"AE","United Kingdom":"GB","Vietnam":"VN",
    # Aera-specific country name variants
    "Utd.Arab Emir.":"AE","Türkiye":"TR","Republic Serbia":"RS",
    "Moldavia":"MD","Russian Fed.":"RU","South Korea":"KR","Vanuatu":"VU",
    "Ukraine":"UA",
}

OUT_FILE        = os.path.join(DIR, "sf_9lc_grain.parquet")
CHECKPOINT_FILE = os.path.join(DIR, "sf_9lc_pair_checkpoint.parquet")
REINIT_EVERY    = 200   # reinit SSR session every N calls
SLEEP_SECS      = 0.2   # polite delay between calls


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_cookies(tok):
    t = tok["access_token"]
    c = {"JSESSIONID": tok.get("jsessionid", ""), "accessToken": t, "token": t}
    lb = tok.get("lb_instance_id", "")
    if lb:
        c["lb-instance-id"] = lb
    return c


def make_headers(tok):
    return {
        "Authorization": tok["access_token"],
        "Origin": BASE, "Referer": BASE,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0",
    }


def init_session(tok):
    r = requests.get(START_URL, cookies=make_cookies(tok),
                     headers=make_headers(tok), timeout=60)
    r.raise_for_status()
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*</script>',
                  r.text, re.DOTALL)
    if not m:
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+)', r.text, re.DOTALL)
        if m:
            raw = m.group(1)
            raw = raw[:raw.rfind("}")+1]
        else:
            raise RuntimeError("Could not find __INITIAL_STATE__")
    else:
        raw = m.group(1)
    state   = json.loads(raw)
    node_id = state["nodeId"]
    nav_id  = state["navigatorId"]
    print(f"  ✓ Session: nodeId={node_id[:16]}... navigatorId={nav_id[:16]}...")
    return node_id, nav_id


def fetch_pair(tok, node_id, nav_id, sku_code, country_code):
    """POST handler=Next with both SKU + Country filters active.
    Returns {month_year: {sf, tpd, src}} or None on failure."""
    url = (f"{BASE}/ispring/Default"
           f"?handler=Next&nodeID={node_id}&navigatorID={nav_id}&__requestjson__=true")

    ui_state = json.dumps([
        {"tableId": "072DC3FC_4D1E_4D43_B408_EEF949D103C8", "page": 1},
        {"tableId": "D7457551_71E8_4906_8E25_249D5D95D9BE", "page": 1},
        {"tableId": "9EE297C8_8DB8_4A97_A3DF_437A0BEB6680", "page": 1},
        {"tableId": "454A9E9F_EE97_49D7_94F4_26BF18AB4662", "page": 1},
    ])

    sku_val     = f"[{base64.b64encode(sku_code.encode()).decode()}]"
    country_val = f"[{base64.b64encode(country_code.encode()).decode()}]"

    form_fields = {
        SKU_FILTER_FIELD:     (None, sku_val),
        COUNTRY_FILTER_FIELD: (None, country_val),
        "uiObjectState":      (None, ui_state),
    }
    headers = {k: v for k, v in make_headers(tok).items()
               if k.lower() != "content-type"}

    for attempt in range(4):
        try:
            r = requests.post(url, files=form_fields, cookies=make_cookies(tok),
                              headers=headers, timeout=60)
        except requests.exceptions.RequestException as e:
            if attempt < 3:
                time.sleep(10 * (2 ** attempt))
                continue
            print(f"    ✗ Connection error: {e}")
            return None

        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(10 * (2 ** attempt))
            continue
        print(f"    ✗ HTTP {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"    ✗ JSON parse error")
        return None

    table = data.get("components", {}).get("072DC3FC_4D1E_4D43_B408_EEF949D103C8", {})
    rows  = table.get("props", {}).get("dataSet", {}).get("value", [])

    if len(rows) < 3:
        return {}

    def row_values(row_idx):
        out = {}
        for cell in rows[row_idx]:
            fn  = cell.get("fieldName", "")
            raw = str(cell.get("value", "0") or "0").replace(",", "")
            try:
                out[fn] = float(raw)
            except ValueError:
                out[fn] = 0.0
        return out

    sf_v, tpd_v, src_v = row_values(0), row_values(1), row_values(2)

    result = {}
    for fl in ALL_FIELD_LABELS:
        my = MONTH_MAP[fl]
        result[my] = {
            "sf":  sf_v.get(fl, 0.0),
            "tpd": tpd_v.get(fl, 0.0),
            "src": src_v.get(fl, 0.0),
        }
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Step 1: Authenticate ===")
    tok = ensure_token(min_seconds=300)

    # ── Step 2: Build SKU × Country pairs from forecast_3yr ─────────────────
    print("\n=== Step 2: Build non-zero SKU × Country pairs from forecast_3yr ===")
    f3yr = pd.read_parquet(os.path.join(DIR, "forecast_3yr_full.parquet"))
    f3yr["Month Year"] = pd.to_datetime(f3yr["Date"], format="%d %b %Y").dt.strftime("%b %Y")
    for c in ["Statistical Forecast Quantity", "3PD Forecast", "Source Forecast"]:
        f3yr[c] = pd.to_numeric(f3yr[c], errors="coerce").fillna(0)

    ea = f3yr[
        f3yr["Region"].isin(["EMEA", "APAC"]) &
        f3yr["Month Year"].isin(ALL_MONTHS) &
        (
            (f3yr["Statistical Forecast Quantity"] > 0) |
            (f3yr["3PD Forecast"] > 0) |
            (f3yr["Source Forecast"] > 0)
        )
    ]

    pairs = (ea[["Material Number", "Country Name"]]
             .drop_duplicates()
             .reset_index(drop=True))

    # Attach country codes — skip countries not in our code map
    pairs["country_code"] = pairs["Country Name"].map(COUNTRY_CODES)
    missing = pairs[pairs["country_code"].isna()]["Country Name"].unique()
    if len(missing):
        print(f"  ⚠ No country code for: {missing} — skipping")
    pairs = pairs.dropna(subset=["country_code"]).reset_index(drop=True)

    print(f"  {len(pairs):,} pairs  |  "
          f"{pairs['Material Number'].nunique():,} SKUs  |  "
          f"{pairs['Country Name'].nunique():,} countries")

    # ── Step 3: Load checkpoint ──────────────────────────────────────────────
    if os.path.exists(CHECKPOINT_FILE):
        cp = pd.read_parquet(CHECKPOINT_FILE)
        done_keys = set(zip(cp["Material Number"], cp["Country Name"]))
        print(f"\n  Checkpoint: {len(done_keys)} pairs done, resuming...")
        pair_rows = cp.to_dict("records")
    else:
        done_keys = set()
        pair_rows = []

    remaining = pairs[
        ~pairs.apply(lambda r: (r["Material Number"], r["Country Name"]) in done_keys, axis=1)
    ].reset_index(drop=True)
    print(f"  {len(remaining):,} pairs remaining")

    # ── Step 4: Init SSR session ─────────────────────────────────────────────
    print("\n=== Step 3: Init SSR session ===")
    node_id, nav_id = init_session(tok)

    # ── Step 5: Fetch each SKU × Country pair ────────────────────────────────
    print(f"\n=== Step 4: Fetching {len(remaining):,} pairs ===")
    total = len(remaining)

    for idx, row in remaining.iterrows():
        sku         = row["Material Number"]
        country     = row["Country Name"]
        cc          = row["country_code"]
        pair_num    = idx + 1
        pct         = int(pair_num / total * 100)

        print(f"  [{pct:3d}%] {pair_num}/{total}  {sku} × {country}...",
              end=" ", flush=True)

        # Reinit session periodically
        if pair_num > 1 and (pair_num - 1) % REINIT_EVERY == 0:
            print("\n  [Reinit session + save checkpoint...]")
            tok = ensure_token(min_seconds=300)
            node_id, nav_id = init_session(tok)
            pd.DataFrame(pair_rows).to_parquet(CHECKPOINT_FILE, index=False)

        result = fetch_pair(tok, node_id, nav_id, sku, cc)
        if result is None:
            print("SKIPPED")
            time.sleep(SLEEP_SECS)
            continue

        sf_24 = sum(v["sf"] for v in result.values())
        print(f"SF_24mo={sf_24:,.1f}")

        for month_year, vals in result.items():
            pair_rows.append({
                "Material Number": sku,
                "Country Name":    country,
                "Month Year":      month_year,
                "SF_exact":        vals["sf"],
                "TPD_exact":       vals["tpd"],
                "SRC_exact":       vals["src"],
            })
        time.sleep(SLEEP_SECS)

    pair_df = pd.DataFrame(pair_rows)
    pair_df.to_parquet(CHECKPOINT_FILE, index=False)
    print(f"\n  ✓ Pair totals: {len(pair_df):,} rows  "
          f"({pair_df['Material Number'].nunique():,} SKUs × "
          f"{pair_df['Country Name'].nunique():,} countries)")

    # Spot-check 730487 × Australia
    spot = pair_df[(pair_df["Material Number"]=="730487") &
                   (pair_df["Country Name"]=="Australia")]
    if not spot.empty:
        print("\n  Spot check 730487 × Australia (Aera Jun 2026 = 39):")
        print(f"  Fetched Jun 2026: {spot[spot['Month Year']=='Jun 2026']['SF_exact'].sum():.1f}")

    # ── Step 6: Allocate to Sub-Segments (within SKU × Country) ─────────────
    print("\n=== Step 5: Split to Sub-Segments (within SKU × Country) ===")
    GRAIN = ["Material Number", "Country Name", "Sub-Segments"]

    ea_sub = f3yr[
        f3yr["Region"].isin(["EMEA", "APAC"]) &
        f3yr["Month Year"].isin(ALL_MONTHS)
    ].copy()

    ea_agg = ea_sub.groupby(GRAIN + ["Month Year"], dropna=False)[
        ["Statistical Forecast Quantity", "3PD Forecast", "Source Forecast"]
    ].sum().reset_index()

    # Per SKU × Country raw totals (for share computation)
    sku_ctry_tot = (
        ea_agg.groupby(["Material Number", "Country Name", "Month Year"])[
            ["Statistical Forecast Quantity", "3PD Forecast", "Source Forecast"]
        ].sum().reset_index()
        .rename(columns={
            "Statistical Forecast Quantity": "sf_raw_tot",
            "3PD Forecast":                 "tpd_raw_tot",
            "Source Forecast":              "src_raw_tot",
        })
    )

    ea_agg = ea_agg.merge(sku_ctry_tot, on=["Material Number", "Country Name", "Month Year"], how="left")

    # Sub-segment shares within SKU × Country (UOM-safe: same SKU × country = same factor)
    ea_agg["sf_share"]  = ea_agg["Statistical Forecast Quantity"] / ea_agg["sf_raw_tot"].replace(0, 1)
    ea_agg["tpd_share"] = ea_agg["3PD Forecast"]                  / ea_agg["tpd_raw_tot"].replace(0, 1)
    ea_agg["src_share"] = ea_agg["Source Forecast"]               / ea_agg["src_raw_tot"].replace(0, 1)

    # Join exact pair totals
    ea_agg = ea_agg.merge(
        pair_df,
        on=["Material Number", "Country Name", "Month Year"],
        how="left"
    )
    ea_agg[["SF_exact", "TPD_exact", "SRC_exact"]] = (
        ea_agg[["SF_exact", "TPD_exact", "SRC_exact"]].fillna(0)
    )

    # Apply shares to exact totals
    ea_agg["Statistical Forecast"] = (ea_agg["sf_share"]  * ea_agg["SF_exact"]).round(4)
    ea_agg["3PD Forecast"]         = (ea_agg["tpd_share"] * ea_agg["TPD_exact"]).round(4)
    ea_agg["Source Forecast"]      = (ea_agg["src_share"] * ea_agg["SRC_exact"]).round(4)

    out = ea_agg[GRAIN + ["Month Year", "Statistical Forecast", "3PD Forecast", "Source Forecast"]].copy()
    out = out[out[["Statistical Forecast", "3PD Forecast", "Source Forecast"]].sum(axis=1) > 0]

    out.to_parquet(OUT_FILE, index=False)
    print(f"  ✓ Saved {len(out):,} rows → {OUT_FILE}")

    # Final spot-check 730487 × Australia
    spot_out = out[(out["Material Number"]=="730487") & (out["Country Name"]=="Australia")]
    if not spot_out.empty:
        print("\n  Final spot-check 730487 × Australia (Aera: Jan=10,Jun=39,Jul=39):")
        aera_check = {"Jan 2026":10,"Jun 2026":39,"Jul 2026":39}
        for m, a in aera_check.items():
            v = spot_out[spot_out["Month Year"]==m]["Statistical Forecast"].sum()
            print(f"    {m}: Aera={a}  Fetched={v:.1f}  diff={v-a:+.1f}")

    print("\n✓ Done — sf_9lc_grain.parquet updated with exact SKU × Country values.")


if __name__ == "__main__":
    main()
