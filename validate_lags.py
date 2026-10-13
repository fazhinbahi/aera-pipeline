"""
validate_lags.py — prove the n-k numbers on the accuracy dashboard are real.

Run this before trusting a change to the lag pipeline, and again whenever Aera
touches the snapshot feed. Everything here is read-only.

Five things can each be wrong on their own, and none of them is visible from
looking at the dashboard:

  1. MAPPING — Fcst3M_Sep_2026 must be the JUNE snapshot forecasting September.
     We had this off by one once, from a comment that asserted the wrong
     convention, so it is re-proved against live Aera rather than against the
     code that wrote the table.

  2. DOUBLING — Aera returns some snapshots at ~2x and we halve them.
     September's n-3 IS one of those snapshots, so the headline n-3 number is
     only right if the correction is. A blanket halve is safe only if every
     grain is inflated; were it bimodal, halving would wreck the clean half.

  3. JOIN GRAIN — the forecast must arrive per customer. If it were coarser
     than lag1_data's key, the value would repeat across customers and every
     total would be inflated.

  4. WEIGHTING — the dashboard splits each customer's forecast across
     sub-segments by actual volume, since customer_analysis carries a
     sub-segment and lag1_data does not. That must conserve volume exactly.

  5. END TO END — SQL written independently of the page's pandas must land on
     the KPIs the page displays.
"""
import os
import sys

import numpy as np
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
sys.path.insert(0, os.path.join(DIR, "chatbot"))

from aera_auth import ensure_token
from build_lag1_table import fetch_lag_pair, _shift
import prework_queries as Q

P, D   = Q.GCP_PROJECT, Q.DATASET
TABLE  = f"{P}.{D}.lag1_data"
TARGET  = "Sep 2026"
# Same month a year earlier: the window now reaches into 2025, where no snapshot
# had ever been checked against Aera.
TARGETS = ["Sep 2026", "Sep 2025"]
LAGS   = [1, 2, 3, 4, 5, 6]
KEY    = ["Material_Number", "Country_Name", "Customer_Number"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]

# The scope the page was showing when these KPIs were read off it.
CHECK_SEG, CHECK_MONTHS = "EMEA ENTERP", ["Aug", "Sep"]
CHECK_KPI = "actuals 91,064 · best n-1 18.6% · worst n-6 181.7% · spread 163.1pp"

TOT_ACT = " + ".join(f"COALESCE(Actual_{m}_2026,0)" for m in MONTHS)
W = f"""
    WITH ca AS (
        SELECT Material_Number, Country_Name, Customer_Number, Sub_Segments,
               SUM({TOT_ACT}) AS seg_actual,
               {", ".join(f"SUM(COALESCE(Actual_{m}_2026,0)) AS act_{m}" for m in MONTHS)}
        FROM `{P}.{D}.customer_analysis` GROUP BY 1,2,3,4
    ),
    tri AS (
        SELECT Material_Number, Country_Name, Customer_Number,
               SUM(seg_actual) AS tri_actual, COUNT(*) AS n_seg
        FROM ca GROUP BY 1,2,3
    ),
    w AS (
        SELECT ca.*, tri.tri_actual, tri.n_seg,
               CASE WHEN tri.tri_actual > 0 THEN ca.seg_actual / tri.tri_actual
                    ELSE 1.0 / tri.n_seg END AS wt
        FROM ca JOIN tri USING (Material_Number, Country_Name, Customer_Number)
    )
"""
JOIN = (f"FROM w LEFT JOIN `{TABLE}` l "
        f"USING (Material_Number, Country_Name, Customer_Number)")


def run(sql):
    return Q._client().query(sql).to_dataframe()


def hdr(s):
    print(f"\n{'=' * 76}\n{s}\n{'=' * 76}", flush=True)


def main():
    tok = ensure_token(min_seconds=600)
    token, jsess, lb = (tok["access_token"], tok.get("jsessionid", ""),
                        tok.get("lb_instance_id", ""))
    tgt = TARGET.replace(" ", "_")

    # ── 1. mapping ───────────────────────────────────────────────────────────
    hdr("1. MAPPING — each stored column vs the snapshot it claims")
    live = {}
    for target in TARGETS:
        t = target.replace(" ", "_")
        bq = run(f"SELECT {', '.join(KEY)}, "
                 f"{', '.join(f'Fcst{k}M_{t}' for k in LAGS)} FROM `{TABLE}`")
        for k in KEY:
            bq[k] = bq[k].astype(str)
        print(f"\n  target {target}")
        print(f"  {'column':22s} {'snapshot':9s} {'Aera live':>12s} {'stored':>12s} "
              f"{'ratio':>6s}  grains matching")
        for k in LAGS:
            snap, col = _shift(target, -k), f"Fcst{k}M_{t}"
            a = fetch_lag_pair(snap, target, token, jsess, lb)
            for c in KEY:
                a[c] = a[c].astype(str)
            if target == TARGET:
                live[k] = a
            m = a.merge(bq[KEY + [col]], on=KEY, how="outer").fillna(0)
            raw, got = m["Adjusted_FC"].sum(), m[col].sum()
            at1 = np.isclose(m["Adjusted_FC"], m[col], rtol=1e-6, atol=0.01).sum()
            at2 = np.isclose(m["Adjusted_FC"] / 2, m[col], rtol=1e-6, atol=0.01).sum()
            # zero rows satisfy both tests, so the larger count is the real verdict
            verdict = "1:1" if at1 >= at2 else "halved"
            print(f"  {col:22s} {snap:9s} {raw:>12,.0f} {got:>12,.0f} "
                  f"{raw / got if got else 0:>6.3f}  {max(at1, at2):,}/{len(m):,} {verdict}")

    # ── 2. doubling ──────────────────────────────────────────────────────────
    hdr("2. DOUBLING — is every grain inflated, or only some?")
    print("Per-grain ratio against a clean snapshot, same target month. Jul is the")
    print("control: it should sit at 1.0 and show the same spread from ordinary")
    print("forecast revision. A blanket halve is only safe if almost nothing in a")
    print("suspect snapshot sits near 1.0.\n")
    CAL = "Aug 2026"
    clean = fetch_lag_pair("Apr 2026", CAL, token, jsess, lb)
    for c in KEY:
        clean[c] = clean[c].astype(str)
    clean = clean.rename(columns={"Adjusted_FC": "clean"})
    for snap in ["May 2026", "Jun 2026", "Jul 2026"]:
        s = fetch_lag_pair(snap, CAL, token, jsess, lb)
        for c in KEY:
            s[c] = s[c].astype(str)
        m = s.merge(clean, on=KEY, how="inner")
        m = m[(m["clean"] > 0) & (m["Adjusted_FC"] > 0)]
        r = m["Adjusted_FC"] / m["clean"]
        print(f"  {snap} vs Apr 2026  (n={len(m):,})   MEDIAN {r.median():.3f}   "
              f"near 2.0: {((r > 1.9) & (r < 2.1)).mean() * 100:4.1f}%   "
              f"near 1.0: {((r > .9) & (r < 1.1)).mean() * 100:4.1f}%")

    # ── 3. external anchor ───────────────────────────────────────────────────
    hdr("3. ANCHOR — do the corrected snapshots bracket actuals?")
    print("The strongest evidence the halving fixes rather than breaks: a corrected")
    print("snapshot should land in the same band as the five others and near actuals.\n")
    for target in TARGETS:
        t = target.replace(" ", "_")
        print(run(f"""
            SELECT 'actual {target}' AS k, ROUND(SUM(Actual_{t}),0) AS v
            FROM `{P}.{D}.customer_analysis`
            {''.join(f"UNION ALL SELECT 'n-{k} ({_shift(target,-k)})', "
                     f"ROUND(SUM(Fcst{k}M_{t}),0) FROM `{TABLE}` " for k in LAGS)}
        """).to_string(index=False))
        print()

    # ── 4. join grain and weighting ──────────────────────────────────────────
    hdr("4. GRAIN & WEIGHTING — no duplication, no leakage")
    a = live[3]
    print(f"  Aera n-3 rows {len(a):,} · duplicate keys {a.duplicated(KEY).sum()} "
          f"· blank customers {(a['Customer_Number'].isin(['', 'nan', 'None'])).sum()}")
    print(f"  lag1_data duplicate keys: {bq.duplicated(KEY).sum()}  (must be 0)")
    print(run(f"""
    {W}
    SELECT COUNT(*) AS triples, COUNTIF(ABS(sw-1) > 1e-9) AS weights_not_summing_to_1
    FROM (SELECT Material_Number, Country_Name, Customer_Number, SUM(wt) AS sw
          FROM w GROUP BY 1,2,3)
    """).to_string(index=False))
    print("\n  volume conserved through the weighted join, and what the join drops:")
    print(run(f"""
    {W}
    {' UNION ALL '.join(f'''
      SELECT 'n-{k}' AS lag, ROUND(SUM(COALESCE(l.Fcst{k}M_{tgt},0)*w.wt),1) AS weighted,
             (SELECT ROUND(SUM(Fcst{k}M_{tgt}),1) FROM `{TABLE}`) AS in_table {JOIN}'''
      for k in LAGS)}
    ORDER BY lag
    """).to_string(index=False))
    print("\n  forecast riding on the even-split fallback (triples with no actuals):")
    print(run(f"""
    {W}
    SELECT ROUND(SUM(CASE WHEN w.tri_actual <= 0 AND w.n_seg > 1
                     THEN COALESCE(l.Fcst3M_{tgt},0)*w.wt ELSE 0 END),1) AS arbitrary,
           ROUND(SUM(COALESCE(l.Fcst3M_{tgt},0)*w.wt),1) AS total {JOIN}
    """).to_string(index=False))

    # ── 5. end to end ────────────────────────────────────────────────────────
    hdr(f"5. END TO END — {CHECK_SEG}, {' + '.join(CHECK_MONTHS)}, un-netted")
    parts = []
    for lag in LAGS:
        cells = " UNION ALL ".join(
            f"SELECT COALESCE(l.Fcst{lag}M_{m}_2026,0)*w.wt AS f, w.act_{m} AS a "
            f"{JOIN} WHERE w.Sub_Segments = '{CHECK_SEG}'" for m in CHECK_MONTHS)
        parts.append(f"""
          SELECT 'n-{lag}' AS lag, ROUND(SUM(f),1) AS fcst, ROUND(SUM(a),1) AS act,
                 ROUND(SUM(ABS(f-a))/NULLIF(SUM(a),0)*100,1) AS wmape,
                 ROUND((SUM(f)-SUM(a))/NULLIF(SUM(a),0)*100,1) AS bias
          FROM ({cells})""")
    print(run(f"{W}\n{' UNION ALL '.join(parts)}\nORDER BY lag").to_string(index=False))
    print(f"\n  page displayed: {CHECK_KPI}")


if __name__ == "__main__":
    main()
