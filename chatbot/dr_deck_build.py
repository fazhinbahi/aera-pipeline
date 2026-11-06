"""
Orchestration for the DR deck builder: scope -> tables -> PPTX bytes.

CLI (local, uses gcloud auth like the pre-work):
    python3 chatbot/dr_deck_build.py --sub-segment "APAC IMC" --countries Australia --out /tmp/au.pptx
    python3 chatbot/dr_deck_build.py --sub-segment "EMEA DEVELOP" --out /tmp/emea_dev.pptx   # all countries
"""
import argparse
import datetime
import os
import sys
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dr_deck_data import (Cycle, bridge_from_fa, demand_plan_from_fa, fetch_bridge,
                          fetch_demand_plan, fetch_fa_rows, fetch_n3_drivers,
                          fetch_npd_tracking, fetch_scope_map, pick_baseline)
from dr_deck_pptx import Section, build_deck

# How the deck names things: "IMC Australia Demand Review", "EMEA Develop",
# "Unconstrained Demand Plan _ Poland", "GTR US"
PRETTY_SUB = {
    "EMEA DEVELOP": "EMEA Develop", "EMEA ENTERP": "EMEA Enterprise",
    "APAC DEVELOP": "APAC Develop", "APAC ENTERP": "APAC Enterprise",
    "EMEA GTR": "GTR EMEA", "US GTR": "GTR US", "APAC GTR": "GTR APAC",
    "APAC IMC": "IMC APAC", "EMEA IMC": "IMC EMEA",
}


def pretty_sub(sub_segment: str) -> str:
    return PRETTY_SUB.get(sub_segment, sub_segment.title())


def section_name(business_segment: str, sub_segment: str, country: Optional[str]) -> str:
    if country:
        return f"IMC {country}" if business_segment == "IMC" else f"{pretty_sub(sub_segment)} _ {country}"
    return pretty_sub(sub_segment)


def deck_title(business_segment: str, sub_segment: str, countries: list[str]) -> str:
    if business_segment == "IMC" and len(countries) == 1:
        return f"IMC {countries[0]}"
    return pretty_sub(sub_segment)


def build_sections(business_segment: str, sub_segment: str, countries: list[str],
                   all_countries: list[str], per_country: bool = True,
                   cycle: Optional[Cycle] = None,
                   progress: Optional[Callable[[str], None]] = None,
                   chain_successors: bool = True,
                   baseline: Optional[datetime.date] = None) -> list[Section]:
    """One Section per selected country, preceded by a sub-segment total when
    more than one country is selected. Sub-segment sections get a country
    bridge and a category bridge; country sections a category bridge."""
    cycle = cycle or Cycle.from_today()
    log = progress or (lambda msg: None)
    scopes: list[tuple[str, list[str], bool]] = []       # (label, countries, is_subsegment_level)
    if len(countries) > 1 or not per_country:
        label = section_name(business_segment, sub_segment, None)
        if len(countries) != len(all_countries):
            label += f" ({len(countries)} markets)"
        scopes.append((label, list(countries), True))
    if per_country:
        for c in countries:
            scopes.append((section_name(business_segment, sub_segment, c), [c], False))

    sections: list[Section] = []
    for name, cs, is_region in scopes:
        fa = fetch_fa_rows(sub_segment, cs, all_countries)
        why = ""
        dp_rtd = None
        if fa is not None:
            # Preferred: the Forecast Adjustments screen rows, Aera's own cycle
            cycle = Cycle.from_aera(fa.closed_through, fa.year)
            log(f"{name}: demand plan from the Aera screen (fetched {fa.fetch_date:%d %b %Y})")
            dp = demand_plan_from_fa(fa, cycle, sub_segment)
            fa_rtd = fetch_fa_rows(sub_segment, cs, all_countries, category="RTDS")
            if fa_rtd is not None:
                dp_rtd = demand_plan_from_fa(fa_rtd, cycle, sub_segment, category="RTDS")
                log(f"{name}: RTDs demand plan from the Aera screen")
            else:
                log(f"{name}: no RTDs rows on the Aera screen — RTDs slide carries a note instead of the table")
            bridges = []
            if is_region:
                bridges.append(bridge_from_fa(fa, "country", cycle))
            bridges.append(bridge_from_fa(fa, "category", cycle))
        else:
            # Fallback: report tables + month-start snapshots
            snap, why = pick_baseline(sub_segment, cs, cycle, baseline)
            log(f"{name}: demand plan from report tables" + (f" (baseline {snap:%d %b %Y})" if snap else ""))
            dp = fetch_demand_plan(sub_segment, cs, cycle, snap)
            if why:
                dp.footnotes.append(why)
            bridges = []
            if is_region:
                bridges.append(fetch_bridge(sub_segment, cs, "country", cycle, snap))
            bridges.append(fetch_bridge(sub_segment, cs, "category", cycle, snap))
        bridges = [b for b in bridges if b is not None]
        log(f"{name}: NPD tracking")
        npd = fetch_npd_tracking(sub_segment, cs, cycle)
        log(f"{name}: N-3 drivers")
        drv = fetch_n3_drivers(sub_segment, cs, chain_successors)
        log(f"{name}: forecast accuracy (monthly WMAPE / BIAS)")
        acc = rca = None
        try:
            from dr_deck_accuracy import fetch_accuracy_series, fetch_rca_drivers
            acc = fetch_accuracy_series(sub_segment, cs)
            rca = fetch_rca_drivers(sub_segment, cs)
        except Exception as e:              # the datos_reducidos tables are optional
            log(f"{name}: accuracy series unavailable ({e})")
        sections.append(Section(name=name, demand_plan=dp, demand_plan_rtd=dp_rtd, npd=npd, drivers=drv,
                                bridges=bridges, accuracy=acc, rca=rca, baseline_note=why, cycle=cycle))
    return sections


def build(business_segment: str, sub_segment: str, countries: list[str], all_countries: list[str],
          per_country: bool = True, cycle: Optional[Cycle] = None,
          progress: Optional[Callable[[str], None]] = None,
          chain_successors: bool = True,
          baseline: Optional[datetime.date] = None) -> tuple[bytes, list[Section]]:
    cycle = cycle or Cycle.from_today()
    sections = build_sections(business_segment, sub_segment, countries, all_countries,
                              per_country, cycle, progress, chain_successors, baseline)
    if sections and sections[0].cycle is not None:
        cycle = sections[0].cycle            # Aera's cycle when the screen rows were available
    codes = {s.name: sub_segment for s in sections}
    title = deck_title(business_segment, sub_segment, countries)
    data = build_deck(title, sections, cycle.label, cycle.year, cycle.next_year, codes)
    return data, sections


def build_xlsx(sections: list[Section]) -> bytes:
    """Every table of every section as a sheet, for drilling during the review."""
    import io
    import re

    import pandas as pd

    buf = io.BytesIO()
    used: set[str] = set()

    def _sheet(name: str) -> str:
        base = re.sub(r"[\[\]:*?/\\]", "", name)[:31]
        out, k = base, 2
        while out in used:
            out = f"{base[:28]} {k}"
            k += 1
        used.add(out)
        return out

    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for s in sections:
            stem = s.name[:22]
            if s.demand_plan is not None:
                s.demand_plan.table.round(1).to_excel(xw, sheet_name=_sheet(f"{stem} plan"))
            if s.demand_plan_rtd is not None:
                s.demand_plan_rtd.table.round(1).to_excel(xw, sheet_name=_sheet(f"{stem} plan RTDs"))
            if s.npd is not None and not s.npd.table.empty:
                s.npd.table.to_excel(xw, sheet_name=_sheet(f"{stem} NPD"), index=False)
            if s.drivers is not None:
                s.drivers.table.round(2).to_excel(xw, sheet_name=_sheet(f"{stem} N-3"), index=False)
            if s.rca is not None and not s.rca.table.empty:
                s.rca.table.round(2).to_excel(xw, sheet_name=_sheet(f"{stem} RCA"), index=False)
            if s.accuracy is not None:
                t = s.accuracy.table.drop(columns=["label"]).copy()
                t["wmape"] = t["wmape"].round(4); t["bias"] = t["bias"].round(4)
                t.round(1).to_excel(xw, sheet_name=_sheet(f"{stem} accuracy"), index=False)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Build a DR deck from BigQuery")
    ap.add_argument("--sub-segment", required=True)
    ap.add_argument("--countries", nargs="*", default=None, help="default: every country in the sub-segment")
    ap.add_argument("--no-per-country", action="store_true")
    ap.add_argument("--no-chain", action="store_true",
                    help="raw Power BI view in the N-3 table (no successor chaining)")
    ap.add_argument("--baseline", default=None,
                    help="snapshot month for N-1 / M-1, e.g. 2026-10-01 (default: latest on or before this month)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    baseline = datetime.date.fromisoformat(args.baseline) if args.baseline else None

    scope = fetch_scope_map()
    bseg = next((b for b, subs in scope.items() if args.sub_segment in subs), None)
    if bseg is None:
        sys.exit(f"sub-segment {args.sub_segment!r} not found; known: "
                 + ", ".join(s for subs in scope.values() for s in subs))
    all_c = scope[bseg][args.sub_segment]
    countries = args.countries or all_c
    data, sections = build(bseg, args.sub_segment, countries, all_c,
                           per_country=not args.no_per_country, progress=print,
                           chain_successors=not args.no_chain, baseline=baseline)
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"✓ {len(sections)} section(s) → {args.out} ({len(data)//1024} KB)")


if __name__ == "__main__":
    main()
