"""
load_datos_reducidos.py — load Enrique's datos_reducidos.csv into BigQuery as reference tables.

datos_reducidos.csv is the long-format file produced by his reducir_exports.py
from the Aera "Consensus Forecast Report - EMEA & APAC" and "Order History EMEA
& APAC - Daily" exports plus CALENDARIO_CIERRES.xlsx; the Forecast Accuracy
dashboards (Forecast_Accuracy_Review.html …) read it. The automated equivalent
is fetch_consensus_snapshots.py → consensus_snapshots (+ views); this loader
keeps his exact numbers next to it for reconciliation and for the EVO / NS
sections that the API fetch does not reproduce.

Row types → tables (dataset aera_demand_planning, WRITE_TRUNCATE):
  META → datos_reducidos_meta   key, value, generated_at
  CAL  → datos_reducidos_cal    approval_date, snapshot_date, group_name, cycle_month, role, note
  DIM  → datos_reducidos_dim    group_name, sub_segment, country, upc, sub_brand, category, size_l
  SKU  → datos_reducidos_sku    group_name, sub_segment, country, upc, material   (one row per material)
  FC   → datos_reducidos_fc     approval_date, snapshot_date, group_name, sub_segment, country, upc, month, consensus, statistical
  ACT  → datos_reducidos_act    group_name, sub_segment, country, upc, month, actual
  EVO  → datos_reducidos_evo    approval_date, snapshot_date, group_name, sub_segment, country, upc, month, consensus  (Note exploded)
  NS   → datos_reducidos_ns     reason, country, consensus, actual, sub_brand, size_l, n_rows
Every table carries source_file and generated_at (from META).

Usage:
  python3 load_datos_reducidos.py --file "~/Downloads/datos_reducidos (2).csv"
"""
import argparse
import datetime as dt
import os
import sys

import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from load_to_bq import _client, _upload_frame  # noqa: E402

RENAME = {"Row Type": "row_type", "Approval Date": "approval_date", "Snapshot Date": "snapshot_date",
          "Group": "group_name", "Sub-Segment": "sub_segment", "Country": "country", "UPC": "upc",
          "Month": "month", "Cycle Month": "cycle_month", "Role": "role", "Consensus": "consensus",
          "Statistical": "statistical", "Actual": "actual", "Sub-Brand": "sub_brand",
          "Category": "category", "Size (L)": "size_l", "Note": "note"}


def _month(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s + "-01", errors="coerce").dt.date


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def split_file(path: str) -> dict[str, pd.DataFrame]:
    raw = pd.read_csv(path, dtype=str, keep_default_na=False).rename(columns=RENAME)
    meta = raw[raw.row_type == "META"]["note"].str.split("=", n=1, expand=True)
    meta.columns = ["key", "value"]
    generated = meta.loc[meta.key == "generated_at", "value"].iloc[0] if (meta.key == "generated_at").any() else ""
    gen_ts = pd.to_datetime(generated, errors="coerce")
    out = {"datos_reducidos_meta": meta.reset_index(drop=True)}

    cal = raw[raw.row_type == "CAL"][["approval_date", "snapshot_date", "group_name", "cycle_month", "role", "note"]].copy()
    cal["approval_date"], cal["snapshot_date"], cal["cycle_month"] = _date(cal.approval_date), _date(cal.snapshot_date), _month(cal.cycle_month)
    out["datos_reducidos_cal"] = cal

    dim = raw[raw.row_type == "DIM"][["group_name", "sub_segment", "country", "upc", "sub_brand", "category", "size_l"]].copy()
    dim["size_l"] = _num(dim.size_l)
    out["datos_reducidos_dim"] = dim

    sku = raw[raw.row_type == "SKU"][["group_name", "sub_segment", "country", "upc", "note"]].copy()
    sku = sku.assign(material=sku.note.str.split("|")).explode("material").drop(columns="note")
    out["datos_reducidos_sku"] = sku[sku.material != ""]

    fc = raw[raw.row_type == "FC"][["approval_date", "snapshot_date", "group_name", "sub_segment", "country", "upc", "month", "consensus", "statistical"]].copy()
    fc["approval_date"], fc["snapshot_date"], fc["month"] = _date(fc.approval_date), _date(fc.snapshot_date), _month(fc.month)
    fc["consensus"], fc["statistical"] = _num(fc.consensus), _num(fc.statistical)
    out["datos_reducidos_fc"] = fc

    act = raw[raw.row_type == "ACT"][["group_name", "sub_segment", "country", "upc", "month", "actual"]].copy()
    act["month"], act["actual"] = _month(act.month), _num(act.actual)
    out["datos_reducidos_act"] = act

    evo = raw[raw.row_type == "EVO"][["approval_date", "snapshot_date", "group_name", "sub_segment", "country", "upc", "note"]].copy()
    evo = evo.assign(pair=evo.note.str.split("|")).explode("pair").drop(columns="note")
    evo = evo[evo.pair.str.contains(":", na=False)]
    evo[["month", "consensus"]] = evo.pair.str.split(":", n=1, expand=True)
    evo = evo.drop(columns="pair")
    evo["approval_date"], evo["snapshot_date"], evo["month"], evo["consensus"] = _date(evo.approval_date), _date(evo.snapshot_date), _month(evo.month), _num(evo.consensus)
    out["datos_reducidos_evo"] = evo

    ns = raw[raw.row_type == "NS"][["sub_segment", "country", "consensus", "actual", "sub_brand", "size_l", "note"]].rename(
        columns={"sub_segment": "reason", "note": "n_rows"}).copy()
    for c in ("consensus", "actual", "size_l", "n_rows"):
        ns[c] = _num(ns[c])
    out["datos_reducidos_ns"] = ns

    for name, df in out.items():
        df["source_file"] = os.path.basename(path)
        df["generated_at"] = gen_ts.to_pydatetime() if pd.notna(gen_ts) else None
        df["loaded_at"] = dt.datetime.now()
        out[name] = df.reset_index(drop=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="path to datos_reducidos*.csv")
    ap.add_argument("--dry-run", action="store_true", help="parse and print row counts only")
    args = ap.parse_args()
    tables = split_file(os.path.expanduser(args.file))
    for name, df in tables.items():
        print(f"  {name:24s} {len(df):>9,} rows × {df.shape[1]} cols")
    if args.dry_run:
        return
    client = _client()
    for name, df in tables.items():
        _upload_frame(client, df, name)
    print("✓ datos_reducidos loaded")


if __name__ == "__main__":
    main()
