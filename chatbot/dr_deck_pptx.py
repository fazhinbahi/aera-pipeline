"""
PPTX renderer for the Demand Review deck.

Draws native PowerPoint tables onto the Becle/VP template (masters and layouts
lifted from the October 2026 DR deck, slides removed), so the output keeps the
deck's title font, footer and logos and every number stays editable.

Readability rules: the demand plan is shown as two stacked blocks (plan year,
next year) instead of one 21-column strip; the NPD table is compact on the
main slide (next year as quarters, at most 14 rows per slide) with a clickable
"Detail" button that jumps to monthly detail slides and back. Row heights are
budgeted at what PowerPoint/Google Slides actually render for the font size,
so nothing runs off the slide.
"""
import io
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_TICK_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

from dr_deck_data import Bridge, DemandPlan, N3Drivers, NpdTracking

GREEN_UP   = RGBColor(0xA9, 0xD1, 0x8E)
ORANGE_DN  = RGBColor(0xF4, 0xB1, 0x83)
NAVY_TOTAL = RGBColor(0x1F, 0x4E, 0x79)
GRID_LINE  = RGBColor(0xD9, 0xD9, 0xD9)

ASSETS   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
TEMPLATE = os.path.join(ASSETS, "dr_template.pptx")
LOGO_VP     = os.path.join(ASSETS, "title_picture_2.png")
LOGO_BECLE  = os.path.join(ASSETS, "title_picture_3.png")

TEAL      = RGBColor(0x00, 0x5E, 0x64)
NAVY      = RGBColor(0x1F, 0x38, 0x64)
BLUE_HDR  = RGBColor(0x44, 0x72, 0xC4)
BLUE_BODY = RGBColor(0xD9, 0xE2, 0xF3)
INK       = RGBColor(0x33, 0x33, 0x33)
MUTED     = RGBColor(0x66, 0x66, 0x66)
RED       = RGBColor(0xC0, 0x00, 0x00)
WHITE     = RGBColor(0xFF, 0xFF, 0xFF)
GRID_HDR  = RGBColor(0xE4, 0xEC, 0xF0)
GRID_TOT  = RGBColor(0xEE, 0xF3, 0xF6)
YELLOW_HDR  = RGBColor(0xF2, 0xC9, 0x4C)
YELLOW_BODY = RGBColor(0xFF, 0xF4, 0xD6)
TEAL_HDR    = RGBColor(0x1B, 0x7F, 0x7F)
TOTAL_ROW   = RGBColor(0xD6, 0xE4, 0xF0)
BODY_FONT = "Avenir Next LT Pro"
TITLE_FONT = "Georgia Pro Cond Semibold"

SLIDE_W = 13.33
MARGIN_L = 0.34
CONTENT_W = 12.65
FOOTER_TOP = 6.95          # layout footer sits at 7.07"; keep content above this


def row_height(font_pt: float) -> float:
    """Inches a renderer gives a single-line table row at this font size
    (line box ~1.2x the font plus the 1pt cell margins and a little slack)."""
    return round((font_pt * 1.25 + 2) / 72 + 0.07, 3)


@dataclass
class Section:
    """One market's set of slides."""
    name: str                               # e.g. 'IMC Australia'
    demand_plan: Optional[DemandPlan] = None
    npd: Optional[NpdTracking] = None
    drivers: Optional[N3Drivers] = None
    bridges: list = field(default_factory=list)   # Bridge objects, in display order
    accuracy: Optional[object] = None             # AccuracySeries (dr_deck_accuracy) – monthly WMAPE / BIAS
    rca: Optional[object] = None                  # RcaDrivers (dr_deck_accuracy) – top-10 offenders, replaces drivers when present
    baseline_note: str = ""                       # why a different snapshot was used, if so
    cycle: Optional[object] = None                # Cycle the section was built on
    figures_note: str = "*Figures in 9LCS"


# ── formatting ────────────────────────────────────────────────────────────────
def _is_nan(v) -> bool:
    return v is None or (isinstance(v, float) and np.isnan(v))


def fmt_int(v) -> str:
    if _is_nan(v):
        return "-"
    return f"{int(round(float(v))):,}"


def fmt_pct_paren(v) -> str:
    """Deck style: negatives in parentheses, e.g. (17.6%), positives 4.8%."""
    if _is_nan(v):
        return "-"
    v = float(v)
    return f"({abs(v):.1f}%)" if v < 0 else f"{v:.1f}%"


def fmt_dec(v, nd=2) -> str:
    return "-" if _is_nan(v) else f"{float(v):,.{nd}f}"


def fmt_pct2(v) -> str:
    return "-" if _is_nan(v) else f"{float(v):.2f}%"


def fmt_1(v) -> str:
    return "-" if _is_nan(v) or v == "" else f"{float(v):,.1f}"


# ── low-level helpers ─────────────────────────────────────────────────────────
def _layout(prs: Presentation, name: str):
    for l in prs.slide_layouts:
        if l.name == name:
            return l
    raise KeyError(f"layout '{name}' not in template")


def _no_bullet(paragraph):
    """Suppress the layout's bullet on a body-placeholder paragraph."""
    pPr = paragraph._p.get_or_add_pPr()
    pPr.set("marL", "0")
    pPr.set("indent", "0")
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    pPr.append(OxmlElement("a:buNone"))


def _cell(cell, text, *, size=8, bold=False, color=INK, fill=None,
          align=PP_ALIGN.RIGHT, font=BODY_FONT, italic=False):
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    # An empty run makes Google Slides size the line at the 18pt default and
    # the whole row grows to ~0.3"; a single space keeps the row at `size`.
    text = " " if text is None or str(text) == "" else str(text)
    p.font.size = Pt(size)
    r = p.add_run()
    r.text = text
    f = r.font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.name = font
    f.color.rgb = color
    cell.margin_left = cell.margin_right = Pt(3)
    cell.margin_top = cell.margin_bottom = Pt(1)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    if fill is None:
        cell.fill.background()
    else:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill


def _table(slide, n_rows, n_cols, left, top, col_widths, row_h):
    width = sum(col_widths)
    shape = slide.shapes.add_table(n_rows, n_cols, Inches(left), Inches(top),
                                   Inches(width), Inches(row_h * n_rows))
    tbl = shape.table
    tbl.first_row = False
    tbl.horz_banding = False
    tbl.vert_banding = False
    tbl.last_row = False
    tbl.first_col = False
    tbl.last_col = False
    for i, w in enumerate(col_widths):
        tbl.columns[i].width = Inches(w)
    for r in tbl.rows:
        r.height = Inches(row_h)
    return tbl


def _textbox(slide, text, left, top, width, height, *, size=8, bold=False,
             color=INK, font=BODY_FONT, align=PP_ALIGN.LEFT, italic=False):
    tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line in enumerate(str(text).split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.name = font
        r.font.color.rgb = color
    return tb


def _link_button(slide, text, left, top, width=1.25, height=0.3):
    """A small teal pill; the caller sets click_action.target_slide."""
    shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top),
                                 Inches(width), Inches(height))
    shp.fill.solid()
    shp.fill.fore_color.rgb = TEAL
    shp.line.fill.background()
    tf = shp.text_frame
    tf.margin_top = tf.margin_bottom = Pt(0)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(9)
    r.font.bold = True
    r.font.name = BODY_FONT
    r.font.color.rgb = WHITE
    return shp


def _content_slide(prs: Presentation, title: str, figures_note: Optional[str]):
    slide = prs.slides.add_slide(_layout(prs, "5_Blanco"))
    ph = slide.placeholders[11]
    ph.text = ""
    p = ph.text_frame.paragraphs[0]
    _no_bullet(p)
    r = p.add_run()
    r.text = title
    r.font.name = TITLE_FONT
    r.font.size = Pt(24)
    r.font.bold = True
    r.font.color.rgb = TEAL
    if figures_note:
        _textbox(slide, figures_note, 11.4, 0.11, 1.8, 0.3, size=8, align=PP_ALIGN.RIGHT)
    return slide


def _footnote(slide, text, top=None):
    """Small italic notes pinned just above the layout footer."""
    top = FOOTER_TOP - 0.42 if top is None else top
    _textbox(slide, text, MARGIN_L, top, CONTENT_W, 0.4, size=7, color=MUTED, italic=True)


def _key_highlights(slide, bullets: list[str], top=0.95, height=0.62):
    """The deck's three-cell KEY HIGHLIGHTS box, one line per cell."""
    widths = [4.4, 4.0, 4.25]
    tbl = _table(slide, 2, 3, MARGIN_L, top, widths, 0.3)
    tbl.rows[1].height = Inches(height - 0.3)
    hdr = tbl.cell(0, 0)
    hdr.merge(tbl.cell(0, 2))
    _cell(hdr, "KEY HIGHLIGHTS", size=12, color=WHITE, fill=BLUE_HDR,
          align=PP_ALIGN.CENTER, font="Calibri")
    for i, b in enumerate(bullets[:3]):
        c = tbl.cell(1, i)
        c.text = ""
        c.fill.solid(); c.fill.fore_color.rgb = BLUE_BODY
        c.margin_left = Pt(6); c.margin_top = Pt(2); c.margin_bottom = Pt(2)
        c.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf = c.text_frame
        tf.word_wrap = True
        for j, line in enumerate(str(b).split("\n")):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            r = p.add_run()
            r.text = "•  " + line
            r.font.size = Pt(10.5)
            r.font.name = "Calibri"
            r.font.color.rgb = RGBColor(0, 0, 0)


# ── slides ────────────────────────────────────────────────────────────────────
def add_title_slide(prs: Presentation, section_name: str, cycle_label: str):
    slide = prs.slides.add_slide(_layout(prs, "1_Portada"))
    slide.placeholders[0].text = f"{section_name} Demand Review"
    slide.placeholders[15].text = "BECLE"
    _textbox(slide, cycle_label, 0.75, 6.33, 5.36, 0.33, size=14)
    if os.path.exists(LOGO_BECLE):
        slide.shapes.add_picture(LOGO_BECLE, Inches(10.48), Inches(1.16), Inches(1.93), Inches(0.86))
    if os.path.exists(LOGO_VP):
        slide.shapes.add_picture(LOGO_VP, Inches(11.87), Inches(6.02), Inches(0.71), Inches(0.65))
    return slide


def _plan_block(slide, df: pd.DataFrame, cols: list[str], pct_rows: list[str],
                corner: str, top: float, font: float) -> float:
    """One block of the demand plan (a year's columns). Returns its bottom."""
    label_w = 1.9
    col_w = (CONTENT_W - label_w) / len(cols)
    widths = [label_w] + [col_w] * len(cols)
    rh = row_height(font)
    tbl = _table(slide, len(df) + 1, len(widths), MARGIN_L, top, widths, rh)
    bold_cols = {c for c in cols if c.startswith("Q") or c in ("YTD", "YTG") or c.endswith("FY")}
    _cell(tbl.cell(0, 0), corner, size=font, bold=True, color=TEAL, fill=GRID_HDR, align=PP_ALIGN.CENTER)
    for j, c in enumerate(cols, start=1):
        _cell(tbl.cell(0, j), c, size=font, bold=c in bold_cols, color=TEAL, fill=GRID_HDR,
              align=PP_ALIGN.CENTER)
    for i, (label, row) in enumerate(df.iterrows(), start=1):
        is_pct = label in pct_rows
        _cell(tbl.cell(i, 0), label, size=font, bold=True, color=TEAL, align=PP_ALIGN.LEFT,
              fill=GRID_HDR if is_pct else None)
        for j, c in enumerate(cols, start=1):
            v = row[c]
            if is_pct:
                txt = fmt_pct_paren(v)
                col = RED if (not _is_nan(v) and v < 0) else INK
            else:
                txt, col = fmt_int(v), INK
            _cell(tbl.cell(i, j), txt, size=font, bold=c in bold_cols, color=col,
                  fill=GRID_TOT if (c in bold_cols and not is_pct) else (GRID_HDR if is_pct else None))
    return top + rh * (len(df) + 1)


def add_demand_plan_slide(prs: Presentation, section: Section, scope_code: str,
                          cycle_year: int, next_year: int):
    """Two stacked blocks, plan year and next year, instead of one 21-column strip."""
    dp = section.demand_plan
    slide = _content_slide(prs, f"Unconstrained Demand Plan _ {section.name}", section.figures_note)
    _key_highlights(slide, dp.highlights, top=0.92, height=0.62)

    df = dp.table
    cols = list(df.columns)
    yy, nyy = str(cycle_year)[2:], str(next_year)[2:]
    cur_cols = [c for c in cols if c.endswith(f"-{yy}") or c in ("YTD", "YTG") or c == f"{cycle_year} FY"]
    nxt_cols = [c for c in cols if c.endswith(f"-{nyy}") or c == f"{next_year} FY"]

    font = 8.5
    top = 1.62
    bottom = _plan_block(slide, df, cur_cols, dp.pct_rows, f"{scope_code} · {cycle_year}", top, font)
    bottom = _plan_block(slide, df, nxt_cols, dp.pct_rows, f"{scope_code} · {next_year}", bottom + 0.08, font)

    notes = ["* Source Forecast = Aera SF/3PD source; Adj. Forecast = current Adjusted Forecast; "
             "Last Year = prior-year sales orders (next year: this year's orders + forecast)."] + dp.footnotes
    _textbox(slide, "  ".join(notes), MARGIN_L, min(bottom + 0.03, FOOTER_TOP - 0.36), CONTENT_W, 0.34,
             size=6.5, color=MUTED, italic=True)
    return slide


def _paginate(df: pd.DataFrame, per_page: int) -> list[pd.DataFrame]:
    if len(df) <= per_page:
        return [df]
    body, last = df.iloc[:-1], df.iloc[-1:]          # keep the subtotal on the last page
    pages = [body.iloc[i:i + per_page] for i in range(0, len(body), per_page)]
    pages[-1] = pd.concat([pages[-1], last])
    return pages


def _npd_header(tbl, fixed, groups, font):
    """Two header rows: group bands, then column names."""
    lead = tbl.cell(0, 0)
    lead.merge(tbl.cell(0, len(fixed) - 1))
    _cell(lead, "NMIs — New Market Inputs", size=font, bold=True, color=WHITE, fill=TEAL, align=PP_ALIGN.LEFT)
    j = len(fixed)
    for title, cols, fill, fc in groups:
        if not cols:
            continue
        c0 = tbl.cell(0, j)
        if len(cols) > 1:
            c0.merge(tbl.cell(0, j + len(cols) - 1))
        _cell(c0, title, size=font, bold=True, color=fc, fill=fill, align=PP_ALIGN.CENTER)
        j += len(cols)
    for j, (name, _, al) in enumerate(fixed):
        _cell(tbl.cell(1, j), name, size=font, bold=True, color=TEAL, fill=GRID_HDR, align=al)
    j = len(fixed)
    for _, cols, fill, _ in groups:
        for c in cols:
            _cell(tbl.cell(1, j), c, size=font, bold=True, color=TEAL,
                  fill=YELLOW_BODY if fill is YELLOW_HDR else GRID_HDR, align=PP_ALIGN.CENTER)
            j += 1


def _npd_table_slides(prs, section: Section, df: pd.DataFrame, groups, fixed, title: str,
                      font: float, per_page: int, note: str, figures="*Figures in 9LC"):
    """Render df over as many slides as needed; returns the slides."""
    slides = []
    pages = _paginate(df, per_page)
    rh = row_height(font)
    num_cols = [c for _, cols, _, _ in groups for c in cols]
    fixed_w = sum(w for _, w, _ in fixed)
    num_w = (CONTENT_W - fixed_w) / max(len(num_cols), 1)
    widths = [w for _, w, _ in fixed] + [num_w] * len(num_cols)
    for k, page in enumerate(pages, start=1):
        t = title if len(pages) == 1 else f"{title} ({k}/{len(pages)})"
        slide = _content_slide(prs, "Innovations", figures)
        _textbox(slide, t, MARGIN_L, 0.9, 8, 0.35, size=14, bold=True, color=TEAL, font="Calibri")
        tbl = _table(slide, len(page) + 2, len(widths), MARGIN_L, 1.3, widths, rh)
        _npd_header(tbl, fixed, groups, font)
        for i, (_, row) in enumerate(page.iterrows(), start=2):
            is_total = str(row["Sub brand"]).startswith("Subtotal")
            for j, (name, _, al) in enumerate(fixed):
                _cell(tbl.cell(i, j), row[name], size=font, bold=is_total, align=al,
                      fill=TOTAL_ROW if is_total else None)
            j = len(fixed)
            for _, cols, fill, _ in groups:
                for c in cols:
                    _cell(tbl.cell(i, j), fmt_1(row[c]), size=font, bold=is_total,
                          fill=TOTAL_ROW if is_total else (YELLOW_BODY if fill is YELLOW_HDR else None))
                    j += 1
        _footnote(slide, note)
        slides.append(slide)
    return slides


NPD_TABLE_TOP = 1.3


def _rows_that_fit(font: float) -> int:
    """Body rows a single Innovations slide can hold at this font (two header rows)."""
    return int((FOOTER_TOP - 0.46 - NPD_TABLE_TOP) / row_height(font)) - 2


def add_npd_slides(prs: Presentation, section: Section, cycle_year: int, next_year: int):
    """ONE Innovations slide per section: the month-by-month NPD table (actuals,
    frozen months, next year by month + FY). Faizan, 6 Nov 2026: the monthly
    layout is the one to keep, and only one Innovations slide. The font steps
    down (7 → 6 pt) so every row fits; a second slide is only produced if the
    table still does not fit at 6 pt."""
    npd = section.npd
    if npd.table.empty:
        slide = _content_slide(prs, "Innovations", "*Figures in 9LC")
        _textbox(slide, "NPD tracking", MARGIN_L, 0.9, 8, 0.35, size=14, bold=True, color=TEAL, font="Calibri")
        _textbox(slide, npd.note, MARGIN_L, 1.5, 12, 0.5, size=11)
        return [slide]

    df = npd.table.copy()
    fy = f"{next_year} FY"
    df[fy] = df[npd.forecast_cols].apply(pd.to_numeric, errors="coerce").sum(axis=1, min_count=1)
    a0, a1 = (npd.actual_cols[0], npd.actual_cols[-1]) if npd.actual_cols else ("", "")
    groups = [(f"ACTUALS · {a0} to {a1}", npd.actual_cols, YELLOW_HDR, INK),
              ("FROZEN", npd.frozen_cols, TEAL_HDR, WHITE),
              (f"FORECAST {next_year} · by month", npd.forecast_cols + [fy], TEAL_HDR, WHITE)]
    fixed = [("Sub brand", 1.6, PP_ALIGN.LEFT), ("Size (L)", 0.45, PP_ALIGN.RIGHT),
             ("SKU", 0.95, PP_ALIGN.LEFT)]
    font = next((f for f in (7.0, 6.5, 6.0) if len(df) <= _rows_that_fit(f)), 6.0)
    return _npd_table_slides(prs, section, df, groups, fixed, "NPD tracking", font, _rows_that_fit(font),
                             npd.note + "  Commentary per line: see the Note column of the Excel export.")


def _nice_step(span: float) -> float:
    """Axis step: 1/2/5 x 10^k that gives roughly 6-8 gridlines over `span`."""
    import math
    if span <= 0:
        return 1.0
    raw = span / 7
    mag = 10 ** math.floor(math.log10(raw))
    for k in (1, 2, 5, 10):
        if raw <= k * mag:
            return k * mag
    return 10 * mag


def add_bridge_chart(slide, bridge: Bridge, left: float, top: float, width: float, height: float):
    """Waterfall as a native stacked-column chart: an invisible base series
    carries each step to its level; increases are green, decreases orange and
    the two totals navy. Editable in PowerPoint and imported by Google Slides."""
    import math
    # Whole numbers, and None where a series has nothing to draw: Google Slides
    # ignores label number formats, so blanks (no point, no label) are the
    # only reliable way to keep "0" off the chart and decimals out of labels.
    cats = [bridge.start_label] + [lbl for lbl, _ in bridge.steps] + [bridge.end_label]
    base, inc, dec, tot = [None], [None], [None], [round(bridge.start)]
    level = round(bridge.start)
    levels = [level, round(bridge.end)]
    for _, d in bridge.steps:
        d = round(d)
        if d > 0:
            base.append(level); inc.append(d); dec.append(None)
            level += d
        elif d < 0:
            level += d
            base.append(level); inc.append(None); dec.append(-d)
        else:
            base.append(level); inc.append(None); dec.append(None)
        tot.append(None)
        levels.append(level)
    base.append(None); inc.append(None); dec.append(None); tot.append(round(bridge.end))

    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series("Base", base)
    cd.add_series("Increase", inc)
    cd.add_series("Decrease", dec)
    cd.add_series("Total", tot)
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_STACKED, Inches(left), Inches(top),
                                Inches(width), Inches(height), cd)
    ch = gf.chart
    ch.has_legend = False
    ch.has_title = True
    ch.chart_title.text_frame.text = bridge.title
    tp = ch.chart_title.text_frame.paragraphs[0]
    tp.runs[0].font.size = Pt(12)
    tp.runs[0].font.bold = False
    tp.runs[0].font.name = "+mn-lt"
    tp.runs[0].font.color.rgb = INK

    plot = ch.plots[0]
    plot.gap_width = 45
    plot.overlap = 100
    s_base, s_inc, s_dec, s_tot = plot.series
    s_base.format.fill.background()
    s_base.format.line.fill.background()
    for s, vals, color, font_color in ((s_inc, inc, GREEN_UP, INK), (s_dec, dec, ORANGE_DN, INK),
                                       (s_tot, tot, NAVY_TOTAL, WHITE)):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = color
        s.format.line.fill.background()
        # Labels as literal text per point ("23,194"): Google Slides ignores
        # number formats on value labels but renders rich-text labels as given.
        for i, v in enumerate(vals):
            if v is None:
                continue
            dl = s.points[i].data_label
            dl.position = XL_LABEL_POSITION.CENTER
            tf = dl.text_frame
            tf.text = f"{int(v):,}"
            r = tf.paragraphs[0].runs[0]
            r.font.size = Pt(9)
            r.font.bold = True
            r.font.name = "+mn-lt"
            r.font.color.rgb = font_color

    lo, hi = min(levels), max(levels)
    span = max(hi - lo, abs(hi) * 0.05, 1.0)
    step = _nice_step(span * 1.3)
    va = ch.value_axis
    va.minimum_scale = max(0.0, math.floor((lo - span * 0.15) / step) * step)
    va.maximum_scale = math.ceil((hi + span * 0.12) / step) * step
    va.major_unit = step
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = GRID_LINE
    va.format.line.fill.background()
    va.tick_labels.font.size = Pt(9)
    va.tick_labels.font.name = "+mn-lt"
    va.tick_labels.number_format = "#,##0"
    va.tick_labels.number_format_is_linked = False
    ca = ch.category_axis
    ca.tick_labels.font.size = Pt(9)
    ca.tick_labels.font.name = "+mn-lt"
    ca.tick_label_position = XL_TICK_LABEL_POSITION.LOW
    ca.format.line.color.rgb = GRID_LINE
    return gf


BRIDGE_AS_IMAGE = True      # Google Slides rasterises PowerPoint charts at low resolution


def render_bridge_png(bridge: Bridge, width_in: float, height_in: float, dpi: int = 220) -> bytes:
    """The bridge as a crisp PNG (matplotlib), styled like the agreed mock-up."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    import textwrap
    raw_cats = [bridge.start_label] + [lbl for lbl, _ in bridge.steps] + [bridge.end_label]
    n = len(raw_cats)
    slot_in = width_in * 0.88 / n                    # horizontal room per bar
    label_pt = 12 if slot_in >= 1.2 else (11 if slot_in >= 0.9 else 9.5)
    char_in = label_pt * 0.58 / 72                   # average character width
    wrap_at = max(8, int(slot_in * 0.95 / char_in))
    wrapped = ["\n".join(line.replace("/ ", "/")
                         for line in textwrap.wrap(c.replace("/", "/ "), wrap_at, break_long_words=False))
               for c in raw_cats]
    # Rotate when a wrapped line still would not fit its slot (long single words)
    rot = 0 if all(len(line) * char_in <= slot_in * 0.98 for c in wrapped for line in c.split("\n")) else 30
    cats = raw_cats if rot else wrapped
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    navy, green, orange = "#1F4E79", "#A9D18E", "#F4B183"
    level = bridge.start
    bottoms, heights, colors, labels, levels = [], [], [], [], [bridge.start, bridge.end]
    bottoms.append(0); heights.append(bridge.start); colors.append(navy); labels.append(f"{bridge.start:,.0f}")
    for _, d in bridge.steps:
        if d >= 0:
            bottoms.append(level); heights.append(d); colors.append(green)
        else:
            bottoms.append(level + d); heights.append(-d); colors.append(orange)
        level += d
        levels.append(level)
        labels.append(f"{abs(d):,.0f}")
    bottoms.append(0); heights.append(bridge.end); colors.append(navy); labels.append(f"{bridge.end:,.0f}")

    x = list(range(n))
    bars = ax.bar(x, heights, bottom=bottoms, color=colors, width=0.62, zorder=3)
    lo, hi = min(levels), max(levels)
    span = max(hi - lo, abs(hi) * 0.05, 1.0)
    step = _nice_step(span * 1.3)
    import math
    y0 = max(0.0, math.floor((lo - span * 0.18) / step) * step)
    y1 = math.ceil((hi + span * 0.14) / step) * step
    ax.set_ylim(y0, y1)
    # connector lines between consecutive levels, as Excel draws them
    for i in range(1, n):
        prev_top = bottoms[i - 1] + heights[i - 1] if i - 1 == 0 else levels[i - 1]
        ax.plot([i - 1 + 0.31, i - 0.31], [prev_top, prev_top], color="#9E9E9E", lw=0.8, zorder=2)
    big = 14 if slot_in >= 1.2 else (12 if slot_in >= 0.9 else 10)
    small = 12 if slot_in >= 1.2 else (11 if slot_in >= 0.9 else 9.5)
    bar_in = slot_in * 0.62

    def _fits(text: str, size: float) -> bool:
        return len(text) * size * 0.62 / 72 <= bar_in     # bold digits ≈ 0.62em wide

    for i, (bar, lab, col) in enumerate(zip(bars, labels, colors)):
        top = bar.get_y() + bar.get_height()
        visible_bottom = max(bar.get_y(), y0)          # the axis clips the totals' base
        visible_h = top - visible_bottom
        size = big if _fits(lab, big) else small
        if col == navy and _fits(lab, size):
            ax.text(i, visible_bottom + visible_h / 2, lab, ha="center", va="center",
                    fontsize=size, fontweight="bold", color="white", zorder=4)
        elif col == navy:
            ax.text(i, top + (y1 - y0) * 0.012, lab, ha="center", va="bottom",
                    fontsize=size, fontweight="bold", color=navy, zorder=4)
        elif visible_h >= (y1 - y0) * 0.07 and _fits(lab, size):
            ax.text(i, visible_bottom + visible_h / 2, lab, ha="center", va="center",
                    fontsize=size, fontweight="bold", color="#222222", zorder=4)
        elif round(bar.get_height()) > 0:
            ax.text(i, top + (y1 - y0) * 0.012, lab, ha="center", va="bottom",
                    fontsize=small, fontweight="bold", color="#222222", zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels(cats, fontsize=small, rotation=rot, ha="right" if rot else "center",
                       rotation_mode="anchor" if rot else "default")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.tick_params(axis="y", labelsize=small, length=0)
    ax.tick_params(axis="x", length=0, pad=6)
    ax.grid(axis="y", color="#D9D9D9", lw=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#BFBFBF")
    ax.set_title(bridge.title, fontsize=15 if width_in >= 9 else 12, color="#333333", pad=12)
    ytick_in = len(f"{y1:,.0f}") * small * 0.6 / 72 + 0.25         # widest y label + padding
    lines = max(c.count("\n") + 1 for c in cats)
    longest_in = max(len(c) for c in raw_cats) * char_in
    xlab_in = (longest_in * 0.5 + 0.2 if rot else 0.25 * lines) + 0.15   # sin(30°) of the label length
    fig.subplots_adjust(left=min(0.2, ytick_in / width_in), right=0.985, top=0.9,
                        bottom=min(0.4, xlab_in / height_in))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def add_bridge_slide(prs: Presentation, section: Section, as_image: bool = BRIDGE_AS_IMAGE):
    """One full-width slide per bridge (by country, then by category for a
    sub-segment; by category alone for a country)."""
    bridges = [b for b in section.bridges if b is not None]
    if not bridges:
        slide = _content_slide(prs, f"MoM Bridge _ {section.name}", section.figures_note)
        _textbox(slide, "No previous-cycle snapshot available yet; the bridge fills from the next cycle.",
                 MARGIN_L, 1.4, 12, 0.5, size=11)
        return [slide]
    slides = []
    for b in bridges:
        suffix = b.title.split("–")[-1].strip() if "–" in b.title else ""
        slide = _content_slide(prs, f"MoM Bridge _ {section.name}" + (f" – {suffix}" if suffix else ""),
                               section.figures_note)
        if as_image:
            png = render_bridge_png(b, CONTENT_W, 5.45)
            slide.shapes.add_picture(io.BytesIO(png), Inches(MARGIN_L), Inches(0.95),
                                     Inches(CONTENT_W), Inches(5.45))
        else:
            add_bridge_chart(slide, b, MARGIN_L, 0.95, CONTENT_W, 5.45)
        note = b.note + " Green = increase, orange = decrease vs the previous cycle."
        if section.baseline_note:
            note += " " + section.baseline_note
        _footnote(slide, note)
        slides.append(slide)
    return slides


def _tile(slide, left, top, width, height, big: str, caption: str, *, fill=None, color=INK):
    """A KPI tile as on the accuracy dashboard: large figure over a small caption."""
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill or RGBColor(0xF7, 0xF8, 0xFA)
    shp.line.color.rgb = RGBColor(0xE3, 0xE6, 0xEA)
    shp.line.width = Pt(0.75)
    shp.shadow.inherit = False
    tf = shp.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Pt(8)
    tf.margin_top = Pt(4); tf.margin_bottom = Pt(2)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run(); r.text = big
    r.font.size = Pt(18); r.font.bold = True; r.font.name = BODY_FONT; r.font.color.rgb = color
    p2 = tf.add_paragraph(); p2.alignment = PP_ALIGN.LEFT
    r2 = p2.add_run(); r2.text = caption
    r2.font.size = Pt(7.5); r2.font.name = BODY_FONT
    r2.font.color.rgb = WHITE if fill is not None and color == WHITE else MUTED
    return shp


def add_accuracy_slide(prs: Presentation, section: Section):
    """Enrique's 'Monthly WMAPE and BIAS' dashboard for the section's scope:
    KPI tiles (pooled WMAPE, bias, best / worst month, window volumes) over the
    two-panel chart, each point one closed month measured at N-3."""
    from dr_deck_accuracy import render_accuracy_png
    acc = section.accuracy
    lag = acc.lag if acc is not None else 3
    slide = _content_slide(prs, f"{section.name} KPIs: Forecast accuracy (N-{lag})", None)
    if acc is None or acc.table.empty:
        _textbox(slide, "No forecast-accuracy series for this scope in the current datos_reducidos export.",
                 MARGIN_L, 1.4, 12, 0.5, size=11)
        return slide
    pooled = f" · {acc.window} months pooled WMAPE {acc.pooled_wmape:.1%}" if acc.pooled_wmape is not None else ""
    _textbox(slide, f"Monthly WMAPE and BIAS · each point is one closed month measured at N-{lag} "
                    f"(consensus forecast vs sales orders, 9LC, UPC level){pooled}",
             MARGIN_L, 0.9, CONTENT_W, 0.28, size=9, color=MUTED, italic=True)
    # KPI tiles = the dashboard's month review for the last closed month
    last = acc.table.iloc[-1]
    mon = last["month"].strftime("%b %Y")
    ytd = acc.table[acc.table["month"].map(lambda d: d.year) == last["month"].year]
    wm, bi = last["wmape"], last["bias"]
    wm_txt = "n/a" if pd.isna(wm) else f"{wm:.1%}"
    bi_txt = "n/a" if pd.isna(bi) else f"{bi:+.1%}"
    fa_txt = "" if pd.isna(wm) else f"FA {1 - wm:.1%} · "
    tiles = [
        (wm_txt, f"WMAPE · {mon} · {fa_txt}Σ|F−A| = {last['abs_err']:,.0f} ÷ ΣA", TEAL, WHITE),
        (bi_txt, f"BIAS · {mon} · " + ("under-forecast" if (0 if pd.isna(bi) else bi) < 0 else "over-forecast"),
         None, TEAL if (0 if pd.isna(bi) else bi) < 0 else RED),
        (f"{last['actual']:,.0f}", f"ordered ΣA · {mon} · YTD {last['month'].year}: {ytd['actual'].sum():,.0f}", None, INK),
        (f"{last['forecast']:,.0f}", f"forecast ΣF · {mon} · N-{lag} · YTD {last['month'].year}: {ytd['forecast'].sum():,.0f}", None, INK),
        (f"{int(last['items'])}", f"items evaluated · {mon} · UPC level", None, INK),
    ]
    gap, tw = 0.14, (CONTENT_W - 4 * 0.14) / 5
    for i, (big, cap, fill, color) in enumerate(tiles):
        _tile(slide, MARGIN_L + i * (tw + gap), 1.22, tw, 0.78, big, cap, fill=fill, color=color)
    chart_top, chart_h = 2.12, FOOTER_TOP - 0.46 - 2.12
    png = render_accuracy_png(acc, CONTENT_W, chart_h)
    slide.shapes.add_picture(io.BytesIO(png), Inches(MARGIN_L), Inches(chart_top), Inches(CONTENT_W), Inches(chart_h))
    _footnote(slide, acc.note)
    return slide


def add_rca_drivers_slide(prs: Presentation, section: Section):
    """The dashboard's 'Biggest offenders by absolute error' top 10 for the last
    closed month at N-3 (RCA Excel columns); Root Cause / Action left blank."""
    d = section.rca
    slide = _content_slide(prs, f"{section.name} KPIs: N-{d.lag} drivers", None)
    wm = f"WMAPE {d.wmape:.1%}" if d.wmape is not None else ""
    bi = f"BIAS {d.bias:+.1%}" if d.bias is not None else ""
    _textbox(slide, f"Biggest offenders by absolute error · {d.month_label} · N-{d.lag} ({d.used}) · "
                    f"{d.items} items · ordered {d.sum_actual:,.0f} · forecast {d.sum_forecast:,.0f} · "
                    f"Σ|F−A| {d.abs_err_total:,.0f} · {wm} · {bi}",
             MARGIN_L, 0.9, CONTENT_W, 0.3, size=8.5, color=MUTED, italic=True)
    df = d.table
    cols = ["#", "UPC", "SKU", "Sub-brand", "Size", "Type", "Ordered", "Forecast", "F − A", "|F − A|",
            "% of total error", "WMAPE", "BIAS", "Root Cause / Action"]
    widths = [0.3, 1.1, 1.05, 2.15, 0.5, 0.95, 0.75, 0.75, 0.75, 0.75, 0.7, 0.65, 0.65, 1.6]
    font, rh = 8.5, 0.36
    tbl = _table(slide, len(df) + 1, len(cols), MARGIN_L, 1.3, widths, rh)
    for j, h in enumerate(cols):
        _cell(tbl.cell(0, j), h, size=font, bold=True, color=WHITE, fill=NAVY,
              align=PP_ALIGN.LEFT if j in (1, 2, 3, 5, 13) else PP_ALIGN.CENTER)
    dash = lambda v, f: "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f(v)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        vals = [str(row["#"]), row["UPC"], row["SKU"], row["Sub-brand"], row["Size"], row["Type"],
                f"{row['Ordered']:,.1f}", f"{row['Forecast']:,.1f}", f"{row['F − A']:+,.1f}", f"{row['|F − A|']:,.1f}",
                f"{row['% of total error']:.1%}", dash(row["WMAPE"], lambda v: f"{v:.1%}"),
                dash(row["BIAS"], lambda v: f"{v:+.1%}"), ""]
        for j, v in enumerate(vals):
            color = INK
            if j == 8 and row["F − A"]:
                color = RED if row["F − A"] > 0 else TEAL
            _cell(tbl.cell(i, j), v, size=font, bold=(j == 9), color=color,
                  align=PP_ALIGN.LEFT if j in (1, 2, 3, 5, 13) else PP_ALIGN.RIGHT if j >= 6 else PP_ALIGN.CENTER)
    _footnote(slide, d.note)
    return slide


def add_drivers_slide(prs: Presentation, section: Section):
    d = section.drivers
    slide = _content_slide(prs, f"{section.name} KPIs: N-3 drivers", None)
    if d is None or d.table.empty:
        _textbox(slide, "No closed-month accuracy data available for this scope yet.", 0.6, 1.4, 12, 0.5, size=11)
        return slide
    _textbox(slide, f"Last closed month: {d.month_label} · {d.lag_note}", 0.6, 0.92, 11.5, 0.3,
             size=9, color=MUTED, italic=True)
    df = d.table
    headers = list(df.columns)
    widths = [2.5, 0.85, 0.9, 0.9, 1.0, 0.8, 0.85, 0.85, 3.5]
    font = 9
    rh = 0.4
    tbl = _table(slide, len(df) + 1, len(headers), 0.6, 1.25, widths, rh)
    for j, h in enumerate(headers):
        _cell(tbl.cell(0, j), h, size=font, bold=True, color=WHITE, fill=NAVY,
              align=PP_ALIGN.LEFT if j in (0, 8) else PP_ALIGN.CENTER)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        is_total = row["Sub Brand / Size"] == "TOTAL"
        fill = TOTAL_ROW if is_total else None
        _cell(tbl.cell(i, 0), row["Sub Brand / Size"], size=font, bold=is_total, align=PP_ALIGN.LEFT, fill=fill)
        _cell(tbl.cell(i, 1), fmt_int(row["IBP"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 2), fmt_int(row["Actual Sales"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 3), fmt_int(row["Forecast Error"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 4), fmt_dec(row["Error ABS UPC"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 5), fmt_pct2(row["% VOL"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 6), fmt_pct2(row["MAPE UPC"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 7), fmt_pct2(row["BIAS"]), size=font, bold=is_total, fill=fill)
        _cell(tbl.cell(i, 8), "", size=font, align=PP_ALIGN.LEFT, fill=fill)   # manual root cause
    return slide


# ── placeholder slides carried in the template ────────────────────────────────
# The template keeps three slides from the original deck, in this order:
# Agenda, "Agreements Previous Demand Review", "Current Month Highlights".
# They are placeholders for the planner to fill; the builder only retitles
# them for the market, blanks the market-specific text and prefills the
# highlights bullets with the computed FY numbers.
TEMPLATE_PLACEHOLDER_SLIDES = 3


def _set_paragraphs(shape, lines: list[str]):
    """Replace a text box's paragraph texts, keeping each paragraph's first
    run formatting; surplus paragraphs are emptied."""
    paras = shape.text_frame.paragraphs
    for i, para in enumerate(paras):
        text = lines[i] if i < len(lines) else ""
        runs = para.runs
        if runs:
            runs[0].text = text
            for r in runs[1:]:
                r.text = ""
        elif text:
            para.add_run().text = text


def _customise_placeholders(prs: Presentation, deck_title: str, highlight_lines: list[str],
                            cycle_year: int, next_year: int):
    def _find(marker: str):
        for s in list(prs.slides)[:TEMPLATE_PLACEHOLDER_SLIDES + 1]:
            for sh in s.shapes:
                if sh.has_text_frame and sh.text_frame.text.strip().startswith(marker):
                    return s
        return None

    agreements = _find("Agreements Previous Demand Review")
    highlights = _find("Current Month Highlights")
    if agreements is None or highlights is None:
        return
    for sh in agreements.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip().startswith("Agreements Previous Demand Review"):
            _set_paragraphs(sh, [f"Agreements Previous Demand Review _ {deck_title}"])
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in list(sh.table.rows)[1:]:          # keep the header row
                for cell in row.cells:
                    for para in cell.text_frame.paragraphs:
                        for r in para.runs:
                            r.text = ""
    for sh in highlights.shapes:
        if not sh.has_text_frame:
            continue
        txt = sh.text_frame.text.strip()
        if txt.startswith("Current Month Highlights"):
            _set_paragraphs(sh, [f"Current Month Highlights _ {deck_title}"])
        elif txt.startswith("FY") and "vs Budget" in txt:
            _set_paragraphs(sh, highlight_lines)
        elif txt.startswith("Alignment on"):
            _set_paragraphs(sh, [f"Alignment on Q1 {next_year} (short term / N-3 period)",
                                 f"Alignment on FY {cycle_year} and FY {next_year}"])


# ── deck ──────────────────────────────────────────────────────────────────────
def build_deck(deck_title: str, sections: list[Section], cycle_label: str, cycle_year: int,
               next_year: int, scope_codes: Optional[dict[str, str]] = None,
               template: str = TEMPLATE) -> bytes:
    """Title slide, the template's three placeholder slides (agenda,
    agreements, current-month highlights), then per section: demand plan,
    bridge(s), one Innovations slide, forecast accuracy, N-3 drivers."""
    prs = Presentation(template)
    scope_codes = scope_codes or {}
    title_slide = add_title_slide(prs, deck_title, cycle_label)
    # the template's placeholder slides come first; put the title in front of them
    sldIdLst = prs.slides._sldIdLst
    for sid in list(sldIdLst):
        if int(sid.id) == title_slide.slide_id:
            sldIdLst.remove(sid)
            sldIdLst.insert(0, sid)
    lines: list[str] = []
    if sections and sections[0].demand_plan is not None:
        h = sections[0].demand_plan.highlights
        lines = h[0].split("\n") + ([h[2]] if len(h) > 2 else [])
    _customise_placeholders(prs, deck_title, lines, cycle_year, next_year)
    for s in sections:
        if s.demand_plan is not None:
            add_demand_plan_slide(prs, s, scope_codes.get(s.name, ""), cycle_year, next_year)
        if s.bridges:
            add_bridge_slide(prs, s)
        if s.npd is not None:
            add_npd_slides(prs, s, cycle_year, next_year)      # one monthly Innovations slide
        add_accuracy_slide(prs, s)
        if s.rca is not None and not s.rca.table.empty:
            add_rca_drivers_slide(prs, s)
        else:
            add_drivers_slide(prs, s)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
