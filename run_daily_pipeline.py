"""
Daily Aera data pipeline.

Steps:
  0. download_prev_adjfc.py — download yesterday's adjfc_raw from BQ → prev_month parquet (PMCF)
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

# launchd starts agents with a minimal PATH that lacks gcloud — the BQ steps
# (download_prev_adjfc, load_to_bq) shell out to `gcloud auth print-access-token`
# and die with FileNotFoundError in <1s without this. (Sep 14-16 incident.)
_EXTRA_PATH = "/usr/local/share/google-cloud-sdk/bin:/usr/local/bin:/opt/homebrew/bin"
os.environ["PATH"] = os.environ.get("PATH", "") + ":" + _EXTRA_PATH


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


def archive_sf_vintage():
    """Keep a copy of each distinct SF snapshot in sf_vintages/ (deduped by content hash).
    The monthly SF run overwrites sf_9lc_grain.parquet in place; without an archive,
    run-over-run comparisons (vintage analysis) become impossible."""
    import hashlib
    import shutil

    src = os.path.join(DIR, "sf_9lc_grain.parquet")
    if not os.path.exists(src):
        return
    vdir = os.path.join(DIR, "sf_vintages")
    os.makedirs(vdir, exist_ok=True)
    with open(src, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:12]
    existing = [fn for fn in os.listdir(vdir) if digest in fn]
    if existing:
        _log(f"  SF vintage unchanged (hash {digest}) — no new archive")
        return
    dest = os.path.join(vdir, f"sf_9lc_grain_{datetime.now():%Y-%m-%d}_{digest}.parquet")
    shutil.copy2(src, dest)
    _log(f"  Archived SF vintage → {os.path.basename(dest)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-fetch", action="store_true", help="Skip Aera API fetch steps")
    parser.add_argument("--skip-bq",    action="store_true", help="Skip BigQuery upload")
    parser.add_argument("--skip-gqo",   action="store_true", help="Skip GQO agent step")
    args = parser.parse_args()

    t_start = time.time()
    _log("════════ Daily pipeline started ════════")

    # Step 0: download yesterday's adjfc_raw from BQ → adjfc_nz_prev_month.parquet (for PMCF)
    # Must run before fetch_adjfc.py overwrites adjfc_nz.parquet, and before load_to_bq.py
    # overwrites adjfc_raw in BQ. Parquets are gitignored so this is the only persistence path.
    run_step("download_prev_adjfc.py", "Step 0/8 — Download prev AdjFC from BQ (for PMCF)", critical=False)

    if not args.skip_fetch:
        run_step("fetch_adjfc.py",         "Step 1/8 — Fetch AdjFC from Aera")
        run_step("fetch_order_history.py", "Step 2/8 — Fetch Order History from Aera")
        run_step("fetch_pmcf.py",          "Step 3/8 — Fetch PMCF from Aera")

        # Clear pair checkpoint so each daily run fetches fresh data from scratch
        sf_checkpoint = os.path.join(DIR, "sf_9lc_pair_checkpoint.parquet")
        if os.path.exists(sf_checkpoint):
            os.remove(sf_checkpoint)
            _log("  Cleared SF pair checkpoint for fresh daily run")
        run_step("fetch_sf_sku_country_9lc.py",
                 "Step 4/8 — Fetch SF/3PD/Source Forecast (SKU × Country exact, ~10 min)",
                 critical=False)
        archive_sf_vintage()
    else:
        _log("Steps 1-5 skipped (--skip-fetch) — using existing parquets")

    if not args.skip_fetch:
        # Rolling 13-month lag table. Incremental: a snapshot is frozen once
        # taken, so this fetches only the pairs it does not already hold plus a
        # refresh of the most recently closed month — roughly 4 minutes, rising
        # to ~6 the day a new month closes. It must run before load_to_bq,
        # which truncates lag1_data from the parquet this writes.
        run_step("build_lag1_table.py",
                 "Step 5/8 — Rebuild rolling 13-month lag table", critical=False)

    if not args.skip_bq:
        run_step("load_to_bq.py",                  "Step 6/8 — Load all tables → BigQuery (incl. stat_3pd_forecast)")
        run_step("fetch_adjfc_2028.py",             "Step 7/8 — Fetch 2028 AdjFC from Aera")
        run_step("add_2028_to_customer_analysis.py","Step 8/8 — Append 2028 AdjFC columns to customer_analysis")
    else:
        _log("Steps 6-8 skipped (--skip-bq)")

    # ── GQO Agent — generate + submit adjustments where orders > adjFC ─────────
    if not args.skip_gqo:
        run_step(
            "gqo_agent.py", "Step 9 — GQO Agent: generate & submit order-vs-forecast adjustments",
            extra_args=["--generate", "--max-rounds", "5"],
            critical=False,  # GQO failure should not abort the rest of the pipeline
        )
    else:
        _log("Step 9 skipped (--skip-gqo)")

    elapsed = (time.time() - t_start) / 60
    _log(f"════════ Pipeline complete in {elapsed:.1f} min ════════\n")


if __name__ == "__main__":
    main()
