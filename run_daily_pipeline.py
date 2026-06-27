"""
Daily Aera data pipeline.

Steps:
  1. fetch_adjfc.py       — refresh AdjFC parquet from Aera (~12 min)
  2. fetch_order_history.py — refresh Order History parquet from Aera
  3. fetch_pmcf.py        — refresh PMCF parquet from Aera
  4. fetch_sf_sku_country_9lc.py — refresh SF/3PD/Source Forecast (SKU × Country exact, ~10 min)
  5. load_to_bq.py        — push all tables to BigQuery (incl. stat_3pd_forecast)

Usage:
  python3.13 run_daily_pipeline.py
  python3.13 run_daily_pipeline.py --skip-fetch  # skip Aera fetch, use existing parquets
  python3.13 run_daily_pipeline.py --skip-bq     # skip BigQuery upload
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

DIR    = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable  # use same python that launched this script
LOG    = os.path.join(DIR, "pipeline_daily.log")


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run_step(script: str, label: str, extra_args: list[str] = [], critical: bool = True):
    _log(f"▶ {label}…")
    t0 = time.time()
    result = subprocess.run(
        [PYTHON, os.path.join(DIR, script)] + extra_args,
        cwd=DIR,
        capture_output=False,
    )
    elapsed = time.time() - t0
    if result.returncode != 0:
        if critical:
            _log(f"✗ {label} failed (exit {result.returncode}) after {elapsed:.0f}s — aborting pipeline")
            sys.exit(result.returncode)
        else:
            _log(f"⚠ {label} failed (exit {result.returncode}) after {elapsed:.0f}s — continuing anyway")
            return
    _log(f"✓ {label} done ({elapsed:.0f}s)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-fetch", action="store_true", help="Skip Aera API fetch steps")
    parser.add_argument("--skip-bq",    action="store_true", help="Skip BigQuery upload")
    args = parser.parse_args()

    t_start = time.time()
    _log("════════ Daily pipeline started ════════")

    if not args.skip_fetch:
        run_step("fetch_adjfc.py",         "Step 1/7 — Fetch AdjFC from Aera")
        run_step("fetch_order_history.py", "Step 2/7 — Fetch Order History from Aera")
        run_step("fetch_pmcf.py",          "Step 3/7 — Fetch PMCF from Aera")

        # Clear pair checkpoint so each daily run fetches fresh data from scratch
        sf_checkpoint = os.path.join(DIR, "sf_9lc_pair_checkpoint.parquet")
        if os.path.exists(sf_checkpoint):
            os.remove(sf_checkpoint)
            _log("  Cleared SF pair checkpoint for fresh daily run")
        run_step("fetch_sf_sku_country_9lc.py",
                 "Step 4/7 — Fetch SF/3PD/Source Forecast (SKU × Country exact, ~10 min)",
                 critical=False)
    else:
        _log("Steps 1-4 skipped (--skip-fetch) — using existing parquets")

    if not args.skip_bq:
        run_step("load_to_bq.py", "Step 5/5 — Load all tables → BigQuery (incl. stat_3pd_forecast)")
    else:
        _log("Step 5 skipped (--skip-bq)")

    elapsed = (time.time() - t_start) / 60
    _log(f"════════ Pipeline complete in {elapsed:.1f} min ════════\n")


if __name__ == "__main__":
    main()
