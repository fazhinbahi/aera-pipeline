"""
DR Deck Builder — pick Business Segment / Sub-Segment / Countries and generate
the Demand Review slides (Unconstrained Demand Plan, NPD tracking, N-3 drivers)
with the latest Aera numbers from BigQuery.
"""
import datetime
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib

import dr_deck_data, dr_deck_accuracy, dr_deck_pptx, dr_deck_build  # noqa: E401
# Streamlit Cloud re-runs this page on every request but keeps imported modules
# cached in the process, so a push that only touches the deck modules would not
# be live until a manual reboot. Reloading them here (dependency order) keeps
# the hosted page on the pushed code; the cost is a few milliseconds.
for _m in (dr_deck_data, dr_deck_accuracy, dr_deck_pptx, dr_deck_build):
    importlib.reload(_m)
from dr_deck_build import build, build_xlsx
from dr_deck_data import Cycle, data_freshness, fetch_aera_cycle, fetch_scope_map
from dr_deck_pptx import fmt_int, fmt_pct_paren

st.set_page_config(page_title="DR Deck Builder", page_icon="🖥️", layout="wide")

MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@st.cache_data(ttl=3600, show_spinner=False)
def _scope_map(consolidated: tuple):
    # `consolidated` (the CONSOLIDATED scope names) is part of the cache key, so
    # adding a virtual sub-segment such as APAC shows up at once instead of
    # after the hour-long cache expiry.
    return fetch_scope_map()


@st.cache_data(ttl=900, show_spinner=False)
def _freshness():
    return data_freshness()


@st.cache_data(ttl=900, show_spinner=False)
def _aera_cycle():
    return fetch_aera_cycle()


_ac = _aera_cycle()
cycle = _ac[0] if _ac else Cycle.from_today()

st.title("Demand Review deck builder")
if _ac:
    st.caption(
        f"Aera cycle: **{cycle.label}** · closed through **{cycle.last_closed_label}** "
        f"(the Forecast Adjustments screen's shaded months, fetched {_ac[1]:%d %b %Y}) · "
        f"plan years {cycle.year} / {cycle.next_year}"
    )
else:
    st.caption(
        f"Calendar cycle: **{cycle.label}** · last closed month **{cycle.last_closed_label}** · "
        f"plan years {cycle.year} / {cycle.next_year} (no screen fetch available yet)"
    )
fresh = _freshness()
if fresh:
    st.caption(f"Aera report data in BigQuery as of **{fresh:%d %b %Y %H:%M}**.")

from dr_deck_data import CONSOLIDATED
try:
    scope = _scope_map(tuple(sorted(CONSOLIDATED)))
except Exception as e:
    st.error(f"Could not load the market list from BigQuery: {e}")
    st.stop()

with st.sidebar:
    st.header("Scope")
    bseg = st.selectbox("Business segment", options=sorted(scope.keys()), index=None,
                        placeholder="Select…", key="dr_bseg")
    subs = sorted(scope.get(bseg, {}).keys()) if bseg else []
    subseg = st.selectbox("Sub-segment", options=subs, index=None, placeholder="Select…",
                          key="dr_subseg", disabled=not bseg)
    all_countries = scope.get(bseg, {}).get(subseg, []) if (bseg and subseg) else []
    # Every sub-segment is reviewed as a whole by default (16 Nov 2026): the
    # country filter is optional and empty means the whole sub-segment on the
    # screen's total rows. Keyed per sub-segment so the widget is recreated
    # (empty again) whenever the sub-segment changes.
    countries = st.multiselect("Countries (optional filter)", options=all_countries, default=[],
                               placeholder=f"Whole sub-segment ({len(all_countries)} markets)" if subseg else "Pick a sub-segment first",
                               key=f"dr_countries_{bseg}_{subseg}",
                               disabled=not subseg,
                               help="Leave empty for the whole sub-segment. Pick countries to narrow the deck "
                                    "to those markets combined.")
    narrowed = bool(countries) and set(countries) < set(all_countries)
    per_country = st.checkbox("One section per selected country", value=False, disabled=not countries,
                              key=f"dr_per_country_{subseg}",
                              help="Only with a country filter: after the combined section, one section "
                                   "per selected country (category bridge, NPD, accuracy, drivers).")
    from dr_deck_data import excluded_next_year, is_consolidated, members_of
    if subseg and is_consolidated(subseg):
        excl = excluded_next_year(subseg)
        st.caption(f"{subseg} = {' + '.join(members_of(subseg))}, summed"
                   + (f"; {cycle.next_year} excludes {', '.join(excl)}." if excl else "."))
    if subseg and not countries:
        st.caption(f"{subseg} as a whole: one section on the screen's sub-segment total rows "
                   "(demand plan, RTDs, bridges, splits, NPD, accuracy, drivers).")
    elif narrowed:
        st.caption(f"Narrowed to {len(countries)} of {len(all_countries)} markets"
                   + (": one section per country as well." if per_country else " combined."))
    chain = st.checkbox("Chain retired pack codes (N-3 table)", value=True,
                        help="Pre-work convention: a pack changeover shows as one line "
                             "(needed to reproduce the Power BI Kraken 375ml line). "
                             "Off: raw Power BI view, every code on its own.")
    baseline = None      # N-1 comes from Aera's own Previous Month Consensus row (screen fetch);
                         # month-start snapshots are only the fallback when that fetch is missing
    st.divider()
    go = st.button("Create slide deck", type="primary", disabled=not (bseg and subseg),
                   use_container_width=True)

if go:
    log = st.status("Building the deck from BigQuery…", expanded=True)
    try:
        data, sections = build(bseg, subseg, countries, all_countries, per_country, cycle,
                               progress=lambda m: log.write(m), chain_successors=chain,
                               baseline=baseline)
    except Exception as e:
        log.update(label="Build failed", state="error")
        st.exception(e)
        st.stop()
    log.update(label=f"Deck ready: {len(sections)} section(s), {len(data)//1024} KB", state="complete",
               expanded=False)
    if sections and sections[0].cycle is not None and sections[0].cycle.first_open != cycle.first_open:
        st.info(f"Built on Aera's cycle: {sections[0].cycle.label}, closed through "
                f"{sections[0].cycle.last_closed_label} (the screen's shaded months), not today's calendar.")
    scope_tag = "whole" if not countries else (
        "_".join(c.replace(" ", "") for c in countries) if len(countries) <= 3 else "multi")
    stem = f"DR_{subseg.replace(' ', '_')}_{scope_tag}_{cycle.first_open:%Y-%m}"
    st.session_state["dr_deck"] = {
        "bytes": data, "sections": sections, "name": f"{stem}.pptx",
        "xlsx": build_xlsx(sections), "xlsx_name": f"{stem}_tables.xlsx",
    }

deck = st.session_state.get("dr_deck")
if deck:
    c1, c2 = st.columns([1, 1])
    c1.download_button("Download .pptx", data=deck["bytes"], file_name=deck["name"], mime=MIME_PPTX,
                       type="primary", use_container_width=True)
    c2.download_button("Download tables (.xlsx)", data=deck["xlsx"], file_name=deck["xlsx_name"],
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
    st.caption("One Innovations slide per market with the month-by-month NPD table. "
               "The Excel file holds every table (with a Note column for commentary) for drilling during the review.")
    st.divider()
    st.subheader("Preview")
    tabs = st.tabs([s.name for s in deck["sections"]])
    for tab, s in zip(tabs, deck["sections"]):
        with tab:
            if s.demand_plan is not None:
                st.markdown("**Unconstrained Demand Plan** (9LC)")
                st.markdown("  \n".join(f"• {h.replace(chr(10), ' · ')}" for h in s.demand_plan.highlights))
                dp = s.demand_plan.table.copy()
                shown = dp.copy().astype(object)
                for r in dp.index:
                    for c in dp.columns:
                        v = dp.at[r, c]
                        shown.at[r, c] = fmt_pct_paren(v) if r in s.demand_plan.pct_rows else fmt_int(v)
                st.dataframe(shown, use_container_width=True)
                for n in s.demand_plan.footnotes:
                    st.caption(n)
            if s.demand_plan_rtd is not None:
                st.markdown("**Unconstrained Demand Plan – RTDs** (9LC)")
                st.markdown("  \n".join(f"• {h.replace(chr(10), ' · ')}" for h in s.demand_plan_rtd.highlights))
                dr = s.demand_plan_rtd.table.copy()
                shown_r = dr.copy().astype(object)
                for r in dr.index:
                    for c in dr.columns:
                        v = dr.at[r, c]
                        shown_r.at[r, c] = fmt_pct_paren(v) if r in s.demand_plan_rtd.pct_rows else fmt_int(v)
                st.dataframe(shown_r, use_container_width=True)
                for n in s.demand_plan_rtd.footnotes:
                    st.caption(n)
            for b in s.bridges:
                st.markdown(f"**{b.title}**")
                rows = [(b.start_label, b.start)] + list(b.steps) + [(b.end_label, b.end)]
                bdf = pd.DataFrame(rows, columns=["Step", "9LC"])
                bdf["9LC"] = bdf["9LC"].map(lambda v: f"{v:+,.0f}" if abs(v) < abs(b.start) * 0.5 else f"{v:,.0f}")
                st.dataframe(bdf, use_container_width=True, hide_index=True)
                st.caption(b.note)
            for c in s.compositions:
                st.markdown(f"**{c.title}**")
                rows = [(c.total_label.replace("\n", " "), c.total, 1.0)] + \
                       [(lbl, v, (v / c.total if c.total else 0.0)) for lbl, v in c.parts]
                cdf = pd.DataFrame(rows, columns=["Part", "9LC", "Share"])
                cdf["9LC"] = cdf["9LC"].map(lambda v: f"{v:,.0f}"); cdf["Share"] = cdf["Share"].map(lambda v: f"{v:.0%}")
                st.dataframe(cdf, use_container_width=True, hide_index=True)
                st.caption(c.note + (f" {c.grouped} smaller parts are folded into the 'Other' bar on the slide; "
                                     "the appendix slide and the Excel split sheet list every one." if c.grouped else ""))
            if s.npd is not None:
                st.markdown("**NPD tracking**")
                st.caption(s.npd.note)
                st.dataframe(s.npd.table, use_container_width=True, hide_index=True)
            for a, tag in ((s.accuracy, ""), (s.accuracy_rtd, " · RTDs only")):
                if a is None:
                    if tag:
                        st.caption("No RTD units for this market in the accuracy file — the RTD accuracy slide carries a note.")
                    continue
                st.markdown(f"**Forecast accuracy{tag} — monthly WMAPE and BIAS (N-{a.lag})**")
                k1, k2, k3, k4 = st.columns(4)
                k1.metric(f"WMAPE · {a.window} months pooled", f"{a.pooled_wmape:.1%}" if a.pooled_wmape is not None else "n/a")
                k2.metric("BIAS · pooled", f"{a.pooled_bias:+.1%}" if a.pooled_bias is not None else "n/a")
                k3.metric("Actual orders · window", f"{a.sum_actual:,.0f}")
                k4.metric(f"Consensus N-{a.lag} · window", f"{a.sum_forecast:,.0f}")
                at = a.table[["month", "wmape", "bias", "actual", "forecast", "items", "used"]].copy()
                at["wmape"] = at["wmape"].map(lambda v: "" if pd.isna(v) else f"{v:.1%}")
                at["bias"] = at["bias"].map(lambda v: "" if pd.isna(v) else f"{v:+.1%}")
                st.dataframe(at.round(0), use_container_width=True, hide_index=True)
                st.caption(a.note)
            if s.rca is not None and not s.rca.table.empty:
                r = s.rca
                st.markdown(f"**N-{r.lag} drivers — biggest offenders by absolute error, {r.month_label}** · "
                            f"{r.used} · {r.items} items · Σ|F−A| {r.abs_err_total:,.0f}")
                st.dataframe(s.rca.table.drop(columns=["Root Cause / Action", "country", "sub_segment", "group"]).round(2),
                             use_container_width=True, hide_index=True)
                st.caption(r.note)
            elif s.drivers is not None:
                st.markdown(f"**N-3 drivers — {s.drivers.month_label}** · {s.drivers.lag_note}")
                if s.drivers.month_label != cycle.last_closed_label:
                    when = f" (last built {s.drivers.lag_table_date:%d %b %Y})" if s.drivers.lag_table_date else ""
                    st.warning(f"The lag table in BigQuery stops at {s.drivers.month_label}{when}; "
                               f"rebuild it to report {cycle.last_closed_label}.")
                st.dataframe(s.drivers.table.drop(columns=["Root Cause / Action"]).round(2),
                             use_container_width=True, hide_index=True)
            else:
                st.info("No closed-month accuracy data for this scope.")
else:
    st.info("Pick a business segment and sub-segment in the sidebar, then click **Create slide deck**. "
            "The deck covers the whole sub-segment unless you narrow it with the optional country filter.")
    st.markdown(
        "Each section produces:\n"
        "1. **Unconstrained Demand Plan** — the rows of Aera's Forecast Adjustments screen for the scope "
        "(Source FC, Adjusted FC, RR Actuals + Adj FC, Previous Month Consensus, Last Year, Sales Orders, "
        "Budget, LE) plus the two % rows, as two year blocks, on Aera's own month cycle; "
        "the whole sub-segment uses the screen's own total rows.\n"
        "   A second slide repeats the table for the RTDs category (with a note when the scope has no RTDs).\n"
        "2. **MoM Bridge** and **Adj. Forecast Split** — waterfalls from the Previous Month Consensus (N-1) to the "
        "Adjusted Forecast (N) and the split of the Adjusted Forecast, this year over next year: "
        "by country and by category for a sub-segment, by category for a single country.\n"
        "3. **NPD tracking** — one Innovations slide: launches of the last 12 months and upcoming NMIs, month by month.\n"
        "4. **Forecast accuracy** — monthly WMAPE and BIAS at N-3 (last 18 closed months) with the last month's KPIs, "
        "from Enrique's datos_reducidos export.\n"
        "5. **N-3 drivers** — the top-10 biggest offenders by absolute error for the last closed month (RCA columns); "
        "the Root Cause / Action column is left empty for manual input."
    )
