"""
fetch_missing_lags.py — add the Lag-2, Lag-5 and Lag-6 snapshots to lag1_data.

The table was built for the accuracy conventions we actually report (n-1, n-3 and
n-4), so the lag-vs-accuracy dashboard has three holes in it. This pulls the
missing pairs from Aera's snapshot dimension, merges them onto the existing rows
and writes the table back.

Safe to re-run: it only adds columns, and a lag already present is skipped.
"""
import datetime
import os
import sys
import time

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
sys.path.insert(0, os.path.join(DIR, "chatbot"))

from aera_auth import ensure_token
from build_lag1_table import fetch_lag_pair, _shift, FORECAST_MONTHS, detect_doubled
import prework_queries as Q

WANTED = [2, 5, 6]                       # the lags missing from the table
TABLE  = f"{Q.GCP_PROJECT}.{Q.DATASET}.lag1_data"
KEY    = ["Material_Number", "Country_Name", "Customer_Number"]


def main():
    client   = Q._client()
    existing = {f.name for f in client.get_table(TABLE).schema}
    todo = [(lag, snap, fc)
            for lag in WANTED
            for snap, fc in [(_shift(m, -lag), m) for m in FORECAST_MONTHS]
            if f"Fcst{lag}M_{fc.replace(' ', '_')}" not in existing]
    if not todo:
        print("nothing to fetch — all lags already present")
        return

    print(f"{len(todo)} pairs to fetch across lags {sorted({l for l,_,_ in todo})}")
    tok = ensure_token(min_seconds=300)
    token, jsess, lb = (tok["access_token"], tok.get("jsessionid", ""),
                        tok.get("lb_instance_id", ""))

    # Same runtime check the main builder uses: halve a snapshot only once it is
    # confirmed to read ~2x a clean reference, so the fix self-disables if Aera
    # repairs the feed.
    doubled = detect_doubled(token, jsess, lb)
    print(f"snapshots confirmed doubled: {sorted(doubled) if doubled else 'none'}")

    frames, t0 = [], time.time()
    for i, (lag, snap, fc) in enumerate(todo, 1):
        if time.time() - t0 > 1800:      # token lives ~45 min; refresh well inside it
            tok = ensure_token(min_seconds=300)
            token, jsess, lb = (tok["access_token"], tok.get("jsessionid", ""),
                                tok.get("lb_instance_id", ""))
            t0 = time.time()
        col = f"Fcst{lag}M_{fc.replace(' ', '_')}"
        print(f"[{i}/{len(todo)}] lag-{lag}  {fc}  (snapshot {snap})", flush=True)
        df = fetch_lag_pair(snap, fc, token, jsess, lb)
        if snap in doubled:
            df["Adjusted_FC"] = df["Adjusted_FC"] / 2.0
        frames.append(df.rename(columns={"Adjusted_FC": col}))

    print("\nmerging onto the existing table...")
    base = client.query(f"SELECT * FROM `{TABLE}`").to_dataframe()
    for k in KEY:
        base[k] = base[k].astype(str)
    out = base
    for f in frames:
        for k in KEY:
            f[k] = f[k].astype(str)
        out = out.merge(f, on=KEY, how="left")

    new_cols = [c for f in frames for c in f.columns if c not in KEY]
    for c in new_cols:
        out[c] = out[c].fillna(0)

    out.to_parquet(os.path.join(DIR, "lag1_vs_actuals_full.parquet"), index=False)
    from google.cloud import bigquery
    client.load_table_from_dataframe(
        out, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE"),
    ).result()
    print(f"\n✓ {TABLE} now has {len(out):,} rows and {len(out.columns)} columns")
    for c in sorted(new_cols):
        print(f"    {c:24s} {out[c].sum():>14,.1f}")


if __name__ == "__main__":
    main()
