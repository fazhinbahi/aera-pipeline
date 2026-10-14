"""
snapshot_total.py — the whole Adjusted Forecast held in one Aera snapshot.

The lag table only ever asks a snapshot about one target month at a time, which
is right for accuracy work and useless for "what did the plan total on the day
of the August run". This asks the same BI object for one snapshot across its
entire horizon in a single call, by making the forecast month a row dimension
instead of a filter.

Usage:  .venv/bin/python snapshot_total.py "Aug 2026"
"""
import math
import os
import sys

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

from aera_auth import ensure_token
import build_lag1_table as B

# The snapshot carries three measures. Which one answers the question matters:
# Adjusted FC is zero for a closed month, so a calendar-year total taken from it
# alone counts only the months still open.
MEASURES = {
    "actuals":  "FC42CB8B-8B96-4966-9EB0-ED0EEB078E62|SUM|||||",
    "adjfc":    "E3524740-47C7-4C30-A381-333FC13DEBD6|SUM|||||",
    "combined": "7C3F4114-0B27-451E-8FD4-CFB36349AAA9|SUM|||||",   # actual if closed, else AdjFC
}


def snapshot_horizon(snapshot, token, jsess, lb, measure="adjfc"):
    """One row per forecast month in `snapshot`, for one measure."""
    base = {
        "sheetid": B.SHEETID, "bioid": B.BIOID, "fid": B.FID,
        "row": f"{B.FORECAST_DIM}|",          # month as the only row dimension
        "col": "", "mea": MEASURES[measure],
        "filter": f"{B.SNAPSHOT_DIM}~cnt|{snapshot}~EN",   # snapshot only, no month filter
        "pot": "0", "sort": B.FORECAST_DIM, "dir": "ASC",
        "in_val": "[]", "source": "report",
        "uom": "", "currency": "", "rate": "", "currencyDate": "T",
        "plimit": str(B.PAGE_LIMIT),
    }
    raw = B._post(token, jsess, {**base, "pstart": "0", "page": "1"}, lb)
    total = raw.get("totalRows", 0)
    if not total:
        return pd.DataFrame(columns=["Month", "Adjusted_FC"])
    rows = list(raw["data"])
    for p in range(1, max(1, math.ceil(total / B.PAGE_LIMIT))):
        rows.extend(B._post(token, jsess,
                            {**base, "pstart": str(p * B.PAGE_LIMIT),
                             "page": str(p + 1)}, lb)["data"])
    cols = [f["name"] for f in raw["metaData"]["fields"]]
    # The value column's id changes with the measure, so take whatever is not
    # the month dimension rather than naming one measure's id.
    mcol = next(c for c in cols if c != B.FORECAST_DIM)
    df = pd.DataFrame(rows, columns=cols).rename(
        columns={B.FORECAST_DIM: "Month", mcol: "Adjusted_FC"})
    df["Adjusted_FC"] = pd.to_numeric(df["Adjusted_FC"], errors="coerce").fillna(0)
    return df.groupby("Month", as_index=False)["Adjusted_FC"].sum()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    measure = next((a[10:] for a in sys.argv[1:] if a.startswith("--measure=")), "adjfc")
    snaps = args or ["Aug 2026"]
    tok = ensure_token(min_seconds=600)
    token, jsess, lb = (tok["access_token"], tok.get("jsessionid", ""),
                        tok.get("lb_instance_id", ""))
    for snap in snaps:
        df = snapshot_horizon(snap, token, jsess, lb, measure)
        if df.empty:
            print(f"\n{snap}: no rows returned"); continue
        df["_o"] = pd.to_datetime(df.Month, format="%b %Y", errors="coerce")
        df = df.sort_values("_o")
        print(f"\n=== snapshot {snap} · {measure} — {len(df)} months, "
              f"{df._o.min():%b %Y} to {df._o.max():%b %Y} ===")
        for _, r in df.iterrows():
            print(f"  {r.Month:10} {r.Adjusted_FC:>14,.1f}")
        print(f"  {'TOTAL':10} {df.Adjusted_FC.sum():>14,.1f}")
        for y in sorted(df._o.dt.year.unique()):
            s = df[df._o.dt.year == y]
            print(f"    {y}: {s.Adjusted_FC.sum():>12,.1f}  ({len(s)} months)")


if __name__ == "__main__":
    main()
