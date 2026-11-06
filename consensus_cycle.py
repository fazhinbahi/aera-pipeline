"""
consensus_cycle.py — which demand-planning cycle each consensus approval belongs to.

Aera stores consensus snapshots by Approval Date only. Forecast accuracy by lag
needs a *cycle month* per approval and per group (IMC / 3PD / GTR), because the
groups close on different months: an approval on 10 Jan 2025 is the January
cycle for IMC but the December cycle for 3PD and GTR. Enrique keeps that
mapping in CALENDARIO_CIERRES.xlsx; its content (as exported in
datos_reducidos.csv, CAL rows) is the seed of consensus_calendar.csv here.

Columns of consensus_calendar.csv:
  approval_date  YYYY-MM-DD   consensus approval date in Aera
  snapshot_date  YYYY-MM-DD   previous approval date (the state the cycle started from)
  group_name     IMC | 3PD | GTR
  cycle_month    YYYY-MM
  role           Primary | Alternate   (a second approval for the same cycle is Alternate;
                                        the dashboards use the earliest Primary)
  note           provenance ("Confirmado (Enrique)", default rule …)

New approvals found by fetch_consensus_snapshots.py are added with the default
rule observed in Enrique's calendar (2025-2026):
  IMC      : approval day >= 5  → cycle = approval month, else the month before
  3PD, GTR : approval day >= 20 → cycle = approval month, else the month before
and are marked "default rule (review)" so a planner can confirm them.
"""
import datetime as dt
import os

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
CALENDAR_CSV = os.path.join(DIR, "consensus_calendar.csv")
GROUPS = ("IMC", "3PD", "GTR")
COLUMNS = ["approval_date", "snapshot_date", "group_name", "cycle_month", "role", "note"]
DEFAULT_NOTE = "default rule (review)"


def group_of(sub_segment: str) -> str:
    """IMC / 3PD / GTR from a sub-segment name (APAC IMC → IMC, EMEA GTR → GTR, else 3PD)."""
    s = (sub_segment or "").upper()
    if "GTR" in s:
        return "GTR"
    if "IMC" in s:
        return "IMC"
    return "3PD"


def _prev_month(first: dt.date) -> dt.date:
    return (first - dt.timedelta(days=1)).replace(day=1)


def default_cycle_month(approval: dt.date, group: str) -> dt.date:
    first = approval.replace(day=1)
    threshold = 5 if group == "IMC" else 20
    return first if approval.day >= threshold else _prev_month(first)


def load_calendar(path: str = CALENDAR_CSV) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    cal = pd.read_csv(path, dtype=str).fillna("")
    cal["approval_date"] = pd.to_datetime(cal["approval_date"]).dt.date
    cal["snapshot_date"] = pd.to_datetime(cal["snapshot_date"], errors="coerce").dt.date
    cal["cycle_month"] = pd.to_datetime(cal["cycle_month"] + "-01").dt.date
    return cal[COLUMNS]


def save_calendar(cal: pd.DataFrame, path: str = CALENDAR_CSV):
    out = cal.copy().sort_values(["approval_date", "group_name"])
    out["approval_date"] = out["approval_date"].map(lambda d: d.isoformat())
    out["snapshot_date"] = out["snapshot_date"].map(lambda d: d.isoformat() if pd.notna(d) else "")
    out["cycle_month"] = out["cycle_month"].map(lambda d: d.strftime("%Y-%m"))
    out[COLUMNS].to_csv(path, index=False)


def update_calendar(cal: pd.DataFrame, approvals, path: str = CALENDAR_CSV) -> pd.DataFrame:
    """Add rows for approval dates missing from the calendar, using the default rule."""
    have = set(cal["approval_date"]) if not cal.empty else set()
    all_dates = sorted(set(approvals) | have)
    rows = []
    for a in all_dates:
        if a in have:
            continue
        earlier = [d for d in all_dates if d < a]
        snapshot = earlier[-1] if earlier else None
        for g in GROUPS:
            cyc = default_cycle_month(a, g)
            taken = cal[(cal["group_name"] == g) & (cal["cycle_month"] == cyc) & (cal["role"] == "Primary")] \
                if not cal.empty else pd.DataFrame()
            dup = [r for r in rows if r["group_name"] == g and r["cycle_month"] == cyc]
            role = "Alternate" if (len(taken) or dup) else "Primary"
            rows.append({"approval_date": a, "snapshot_date": snapshot, "group_name": g,
                         "cycle_month": cyc, "role": role, "note": DEFAULT_NOTE})
    if rows:
        cal = pd.concat([cal, pd.DataFrame(rows)], ignore_index=True)
        print(f"  calendar: added {len(rows) // len(GROUPS)} approval(s) with the default rule — "
              f"{', '.join(sorted({r['approval_date'].isoformat() for r in rows}))}")
    save_calendar(cal, path)
    return cal


if __name__ == "__main__":
    c = load_calendar()
    print(c.to_string())
