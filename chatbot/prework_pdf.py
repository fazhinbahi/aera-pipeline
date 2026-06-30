"""
Pre-Alignment PDF builder — generates a professional pre-work document
for any Country + Sub-Segment from live BigQuery data.
"""
import io
import os
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from openai import OpenAI
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image, HRFlowable,
)

from prework_queries import CLOSED_2026, OPEN_2026, LAST_CLOSED, _ALL_MONTHS

# ── Colours ───────────────────────────────────────────────────────────────────
NAVY    = colors.HexColor('#1B2B4B')
NAVY2   = colors.HexColor('#2E4272')
LBLUE   = colors.HexColor('#D6E4F0')
ALTROW  = colors.HexColor('#F4F6FA')
TOTROW  = colors.HexColor('#D6E4F0')
AMBER   = colors.HexColor('#FFF3CC')
WHITE   = colors.white
BODY_C  = colors.HexColor('#333333')
GREY    = colors.HexColor('#888888')

# ── Styles ────────────────────────────────────────────────────────────────────
def _S(name, **kw):
    return ParagraphStyle(name, **kw)

_base = dict(fontName='Helvetica', fontSize=10, textColor=BODY_C,
             leading=14, spaceAfter=4, spaceBefore=2)

ST = {
    'body':   _S('body',   **_base),
    'bullet': _S('bullet', **{**_base, 'leftIndent': 14, 'firstLineIndent': -10,
                               'spaceBefore': 1, 'spaceAfter': 3}),
    'sub':    _S('sub',    **{**_base, 'fontName': 'Helvetica-Bold', 'fontSize': 11,
                               'textColor': NAVY, 'spaceBefore': 10, 'spaceAfter': 4}),
    'source': _S('source', **{**_base, 'fontSize': 8.5, 'textColor': GREY,
                               'fontName': 'Helvetica-Oblique'}),
    'cover1': _S('cover1',  fontName='Helvetica-Bold', fontSize=24,
                 textColor=WHITE, alignment=TA_CENTER, leading=30),
    'cover2': _S('cover2',  fontName='Helvetica', fontSize=14,
                 textColor=LBLUE, alignment=TA_CENTER, leading=20),
    'cell_h': _S('cell_h', fontName='Helvetica-Bold', fontSize=8.5,
                 textColor=WHITE, alignment=TA_CENTER, leading=11),
    'cell':   _S('cell',   fontName='Helvetica', fontSize=8.5,
                 textColor=BODY_C, alignment=TA_LEFT, leading=11),
    'cell_c': _S('cell_c', fontName='Helvetica', fontSize=8.5,
                 textColor=BODY_C, alignment=TA_CENTER, leading=11),
    'cell_t': _S('cell_t', fontName='Helvetica-Bold', fontSize=8.5,
                 textColor=NAVY, alignment=TA_LEFT, leading=11),
    'cell_tc':_S('cell_tc',fontName='Helvetica-Bold', fontSize=8.5,
                 textColor=NAVY, alignment=TA_CENTER, leading=11),
    'box':    _S('box',    fontName='Helvetica', fontSize=9, textColor=NAVY,
                 leading=13, spaceBefore=3, spaceAfter=3),
    'boxB':   _S('boxB',   fontName='Helvetica-Bold', fontSize=9, textColor=NAVY,
                 leading=13, spaceBefore=3, spaceAfter=1),
    'footer': _S('footer', fontName='Helvetica', fontSize=7.5,
                 textColor=GREY, alignment=TA_CENTER),
    'meta':   _S('meta',   fontName='Helvetica', fontSize=10, textColor=NAVY),
    'meta_r': _S('meta_r', fontName='Helvetica', fontSize=10,
                 textColor=NAVY, alignment=TA_RIGHT),
}

W = A4[0] - 2 * 1.7 * cm  # usable page width in points


# ── Layout helpers ────────────────────────────────────────────────────────────
def sp(h=6):
    return Spacer(1, h)


def section_hdr(title):
    tbl = Table(
        [[Paragraph(title, ParagraphStyle('sh', fontName='Helvetica-Bold',
                                          fontSize=13, textColor=WHITE, leading=17))]],
        colWidths=[W + 0.4 * cm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), NAVY),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('TOPPADDING',    (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    return [tbl, sp(4)]


def callout(lines, bg=AMBER):
    content = [Paragraph(text, ST[style]) for text, style in lines]
    inner = Table([[content]], colWidths=[W])
    inner.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), bg),
        ('LEFTPADDING',   (0, 0), (-1, -1), 10),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 0.5, NAVY),
    ]))
    return [inner, sp(5)]


def dtbl(headers, rows, col_w, center_from=1, font_size=8.5):
    """Styled data table. Columns >= center_from are center-aligned."""
    def _st(base_key):
        """Return style, optionally with font_size override."""
        if font_size == 8.5:
            return ST[base_key]
        s = ST[base_key]
        return ParagraphStyle(base_key + '_s', parent=s, fontSize=font_size, leading=font_size + 2.5)

    def pc(text, bold=False, center=False, total=False):
        if total:
            return Paragraph(str(text), _st('cell_tc') if center else _st('cell_t'))
        if bold:
            return Paragraph(str(text), _st('cell_h'))
        return Paragraph(str(text), _st('cell_c') if center else _st('cell'))

    data = [[pc(h, bold=True, center=True) for h in headers]]
    for ri, row in enumerate(rows):
        is_tot = str(row[0]).strip().lower().startswith('total')
        data.append([pc(v, center=(ci >= center_from), total=is_tot)
                     for ci, v in enumerate(row)])

    styles = [
        ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, ALTROW]),
        ('GRID',          (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
        ('LINEBELOW',     (0, 0), (-1, 0),  1,   NAVY),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
    ]
    for ri, row in enumerate(rows):
        if str(row[0]).strip().lower().startswith('total'):
            styles += [
                ('BACKGROUND', (0, ri+1), (-1, ri+1), TOTROW),
                ('LINEABOVE',  (0, ri+1), (-1, ri+1), 0.8, NAVY),
            ]
    t = Table(data, colWidths=col_w)
    t.setStyle(TableStyle(styles))
    return [t, sp(5)]


def bul(text):
    return Paragraph(f'• {text}', ST['bullet'])


# ── Number formatting ─────────────────────────────────────────────────────────
def _fmt(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '—'
    return f"{int(round(v)):,}"


def _pct(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '—'
    sign = '+' if v > 0 else ''
    return f"{sign}{v:.1f}%"


def _col_sum(df, col):
    return df[col].sum() if col in df.columns else 0


# ── GPT-4.1 commentary ────────────────────────────────────────────────────────
def _gpt(prompt: str, client: OpenAI) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1",
            max_tokens=160,
            messages=[
                {"role": "system", "content": (
                    "You are a concise demand planning analyst. Write 2-3 sentences "
                    "of professional insight. No bullet points. No headers. Be direct and specific."
                )},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return ""


# ── Accuracy bar chart ────────────────────────────────────────────────────────
def _accuracy_chart(monthly_stats: list[dict]) -> io.BytesIO:
    months = [d['month']   for d in monthly_stats]
    acts   = [d['Actuals'] for d in monthly_stats]
    lag3s  = [d['Lag3FC']  for d in monthly_stats]
    wmapes = [d['wMAPE']   for d in monthly_stats]
    biases = [d['Bias']    for d in monthly_stats]

    x = np.arange(len(months))
    w = 0.35

    fig, ax1 = plt.subplots(figsize=(6.5, 3.2))

    # Left axis — volume bars
    ax1.bar(x - w/2, acts,  w, label='Actuals (9LC)',    color='#1B2B4B', alpha=0.88)
    ax1.bar(x + w/2, lag3s, w, label='Lag-3 Fcst (9LC)', color='#90B4D4', alpha=0.90)
    ax1.set_ylabel('Volume (9LC)', fontsize=8, color='#333333')
    ax1.tick_params(axis='y', labelsize=8)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax1.spines[['top']].set_visible(False)

    # Right axis — wMAPE and Bias dotted lines
    ax2 = ax1.twinx()
    ax2.plot(x, wmapes, color='#E05252', linewidth=1.8, linestyle='--',
             marker='o', markersize=5, label='wMAPE %')
    ax2.plot(x, biases, color='#F5A623', linewidth=1.8, linestyle=':',
             marker='s', markersize=5, label='Bias %')
    ax2.axhline(0, color='#AAAAAA', linewidth=0.5)
    ax2.set_ylabel('wMAPE / Bias %', fontsize=8, color='#333333')
    ax2.tick_params(axis='y', labelsize=8)
    ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter('%.0f%%'))
    ax2.spines[['top']].set_visible(False)

    ax1.set_xticks(x)
    ax1.set_xticklabels(months, fontsize=8)

    # Combined legend
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, fontsize=8, framealpha=0.8, loc='upper right')

    ax1.set_title('Lag-3 Forecast Accuracy — 2026 YTD', fontsize=9,
                  fontweight='bold', color='#1B2B4B', pad=8)
    fig.tight_layout(pad=0.8)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf


# ── OpenAI client ─────────────────────────────────────────────────────────────
def _openai_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.strip().startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return OpenAI(api_key=key) if key else None


# ══════════════════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════════════════
def build_prework_pdf(
    ca: pd.DataFrame,
    acc: pd.DataFrame,
    country: str,
    sub_segment: str,
) -> bytes:
    """
    Build the pre-alignment PDF and return as bytes.
    ca  : customer_analysis DataFrame for this market
    acc : lag1 accuracy DataFrame (lag1_data JOIN customer_analysis)
    """
    gpt_client = _openai_client()
    today      = datetime.date.today()
    today_str  = today.strftime("%d %B %Y")
    cycle      = today.strftime("%B %Y")

    # Precompute YTD 2026 volumes (used in multiple sections)
    ytd_cols = [f"Actual_{m}_2026" for m in CLOSED_2026 if f"Actual_{m}_2026" in ca.columns]
    if ytd_cols and not ca.empty:
        ca = ca.copy()
        ca["_YTD_2026"] = ca[ytd_cols].sum(axis=1)
    else:
        ca = ca.copy()
        ca["_YTD_2026"] = 0

    # Fill string columns that might have NaN
    for col in ["Sub_Brand_Description", "Brand_Family", "Category_Grouper_Description_Z"]:
        if col in ca.columns:
            ca[col] = ca[col].fillna("Unknown")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.7*cm, rightMargin=1.7*cm,
        topMargin=1.8*cm,  bottomMargin=1.8*cm,
        title=f'{country} {sub_segment} Pre-Alignment — {cycle}',
        author='Demand Planning Team',
    )
    story = []

    # ══ COVER ═════════════════════════════════════════════════════════════════
    cover_top = Table(
        [[Paragraph(f'{country.upper()}  ·  {sub_segment}\nPRE-ALIGNMENT MEETING', ST['cover1'])]],
        colWidths=[W + 0.4*cm])
    cover_top.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), NAVY),
        ('TOPPADDING',    (0, 0), (-1, -1), 30),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
    ]))
    cover_sub = Table(
        [[Paragraph(f'Preparation Guide — {cycle} Cycle', ST['cover2'])]],
        colWidths=[W + 0.4*cm])
    cover_sub.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), NAVY2),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    meta = Table(
        [[Paragraph('Demand Planning Team', ST['meta']),
          Paragraph(today_str, ST['meta_r'])]],
        colWidths=[W/2, W/2])
    meta.setStyle(TableStyle([
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    # Cover — key at-a-glance metrics
    ytd_total   = ca["_YTD_2026"].sum() if not ca.empty else 0
    fc_cols_all = [f"AdjFC_{m}_2026" for m in OPEN_2026 if f"AdjFC_{m}_2026" in ca.columns]
    h2_total    = ca[fc_cols_all].sum().sum() if fc_cols_all and not ca.empty else 0
    act25_total = _col_sum(ca, "Actual_Total_2025") if not ca.empty else 0
    n_skus      = ca["Material_Number"].nunique() if "Material_Number" in ca.columns and not ca.empty else 0
    n_customers = ca["Customer_Number"].nunique() if "Customer_Number" in ca.columns and not ca.empty else 0
    region      = sub_segment.split()[0]  # "APAC" or "EMEA"

    kpi_labels = ['2026 YTD Actuals (9LC)', 'H2 2026 AdjFC (9LC)',
                  '2025 Full Year (9LC)', 'Active SKUs', 'Active Customers']
    kpi_values = [_fmt(ytd_total), _fmt(h2_total), _fmt(act25_total),
                  f"{n_skus:,}", f"{n_customers:,}"]
    _kl = ParagraphStyle('kl', fontName='Helvetica', fontSize=8, textColor=GREY,
                         alignment=TA_CENTER, leading=11)
    _kv = ParagraphStyle('kv', fontName='Helvetica-Bold', fontSize=15, textColor=NAVY,
                         alignment=TA_CENTER, leading=19)
    kpi_col_w = [(W + 0.4*cm) / 5] * 5
    kpi_tbl = Table(
        [[Paragraph(l, _kl) for l in kpi_labels],
         [Paragraph(v, _kv) for v in kpi_values]],
        colWidths=kpi_col_w,
    )
    kpi_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), LBLUE),
        ('TOPPADDING',    (0, 0), (  -1, 0),  8),
        ('BOTTOMPADDING', (0, 0), (  -1, 0),  2),
        ('TOPPADDING',    (0, 1), (  -1,-1),  2),
        ('BOTTOMPADDING', (0, 1), (  -1,-1), 10),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('LINEAFTER',     (0, 0), (-2, -1), 0.5, colors.HexColor('#AABDD4')),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    # Document contents overview
    _sec_style = ParagraphStyle('sc', fontName='Helvetica', fontSize=9.5,
                                textColor=BODY_C, leading=16, leftIndent=10)
    _sec_bold  = ParagraphStyle('scb', fontName='Helvetica-Bold', fontSize=9.5,
                                textColor=NAVY, leading=16, leftIndent=10)
    sections_left = [
        ('1', 'Introduction & Key Metrics'),
        ('2', 'Confirmed Orders vs Adjusted Forecast'),
        ('3', 'Forecast Accuracy — Lag-3 (M-3)'),
        ('4', 'Monthly Year-on-Year Comparison'),
    ]
    sections_right = [
        ('5', 'Deviation Flags — Top 5 Sub-Brands'),
        ('6', 'Category & Brand Family Sanity Check'),
        ('B', 'Appendix B — Monthly Historical Sales'),
        ('C', 'Appendix C — Top-10 Product Rankings'),
        ('D', 'Appendix D — Category & Brand Family Breakdown'),
    ]
    def _sec_rows(items):
        return [[Paragraph(f'<b>{n}.</b>', _sec_bold), Paragraph(t, _sec_style)]
                for n, t in items]

    _half = W / 2
    contents_tbl = Table(
        [[
            Table(_sec_rows(sections_left),  colWidths=[0.7*cm, _half - 0.9*cm]),
            Table(_sec_rows(sections_right), colWidths=[0.7*cm, _half - 0.9*cm]),
        ]],
        colWidths=[_half, _half],
    )
    contents_tbl.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 0),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ('TOPPADDING',    (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))

    scope_style = ParagraphStyle('sc2', fontName='Helvetica', fontSize=9,
                                 textColor=GREY, leading=14)
    scope_line  = (f'<b>Region:</b> {region}  ·  <b>Market:</b> {country}  ·  '
                   f'<b>Sub-Segment:</b> {sub_segment}  ·  '
                   f'<b>Data period:</b> Jan 2024 – Dec 2027  ·  '
                   f'<b>Prepared:</b> {today_str}')

    story += [
        sp(20), cover_top, cover_sub, sp(14), meta, sp(18),
        kpi_tbl, sp(16),
        Paragraph('DOCUMENT CONTENTS', ParagraphStyle('dch', fontName='Helvetica-Bold',
                  fontSize=10, textColor=NAVY, leading=14, spaceAfter=8)),
        HRFlowable(width=W + 0.4*cm, thickness=0.5, color=NAVY),
        sp(6),
        contents_tbl,
        sp(20),
        HRFlowable(width=W + 0.4*cm, thickness=0.5, color=colors.HexColor('#CCCCCC')),
        sp(6),
        Paragraph(scope_line, scope_style),
        PageBreak(),
    ]

    # ══ SECTION 1 — INTRODUCTION ══════════════════════════════════════════════
    story += section_hdr('1   INTRODUCTION AND KEY METRICS')
    story += [
        Paragraph(
            f'This document prepares the <b>Pre-Alignment Meeting</b> for '
            f'<b>{country} {sub_segment}</b> — <b>{cycle} forecast cycle</b>. '
            f'Exception-based review covering:', ST['body']),
        bul('Confirmed Sales Orders vs Adjusted Forecast — remaining open months'),
        bul('Forecast Accuracy (Lag-3) — wMAPE &amp; Bias across all closed 2026 months'),
        bul('Top-10 Product Performance — last closed month'),
        bul('Monthly Year-on-Year Comparison — 2026 vs 2025'),
        bul('Deviation Flags — Top 5 Sub-Brands'),
        bul('Category &amp; Brand Family Sanity Check'),
        sp(8),
    ]
    story += callout([
        ('📊  KEY ACCURACY METRICS', 'boxB'),
        ('wMAPE = Σ|Actuals − Forecast| / Σ(Actuals) × 100  |  Company target: <b>17.12%</b>', 'box'),
        ('Bias% = Σ(Forecast − Actuals) / Σ(Actuals) × 100  |  Bias &gt; 0 → over-forecast  '
         '|  Bias &lt; 0 → under-forecast  |  Target = 0%', 'box'),
    ], bg=LBLUE)

    # ══ SECTION 2 — CONFIRMED ORDERS vs ADJ FORECAST ═════════════════════════
    story += [sp(6)] + section_hdr('2   CONFIRMED ORDERS vs ADJUSTED FORECAST')
    story += [
        Paragraph(
            'Confirmed Sales Orders (SO) are actual customer orders already booked in the system '
            'for the remaining open months of 2026. Compared against the Adjusted Forecast (AdjFC) '
            'to identify demand/supply alignment. '
            '<b>SO &gt; AdjFC</b> = demand exceeding plan. '
            '<b>SO &lt;&lt; AdjFC</b> = risk of shortfall.',
            ST['body']),
        sp(4),
    ]

    so_cols = [f"SO_{m}_2026"    for m in OPEN_2026 if f"SO_{m}_2026"    in ca.columns]
    fc_cols = [f"AdjFC_{m}_2026" for m in OPEN_2026 if f"AdjFC_{m}_2026" in ca.columns]
    open_ms_avail = [m for m in OPEN_2026 if f"AdjFC_{m}_2026" in ca.columns]

    if fc_cols and not ca.empty:
        grp = ca.groupby("Sub_Brand_Description")[so_cols + fc_cols].sum().reset_index()
        grp = grp[(grp[so_cols].sum(axis=1) > 0) | (grp[fc_cols].sum(axis=1) > 0)]
        grp["_FC_Total"] = grp[fc_cols].sum(axis=1)
        grp = grp.sort_values("_FC_Total", ascending=False).head(10)
        grp["_SO_Total"] = grp[so_cols].sum(axis=1) if so_cols else 0
        grp["_Delta"]    = grp["_SO_Total"] - grp["_FC_Total"]

        # Monthly AdjFC table
        story.append(Paragraph('Adjusted Forecast (AdjFC) by Sub-Brand', ST['sub']))
        n_m = len(open_ms_avail)
        sub_w  = 5.5 * cm
        m_w    = (W - sub_w - 2.0*cm) / max(n_m, 1)
        cw_mo  = [sub_w] + [m_w] * n_m + [2.0*cm]

        fc_hdrs = ['Sub-Brand'] + open_ms_avail + ['H2 Total']
        fc_rows = []
        for _, r in grp.iterrows():
            row = [r['Sub_Brand_Description']]
            for m in open_ms_avail:
                row.append(_fmt(_col_sum(r.to_frame().T, f"AdjFC_{m}_2026")))
            row.append(_fmt(r['_FC_Total']))
            fc_rows.append(row)
        fc_rows.append(
            ['TOTAL'] +
            [_fmt(grp[f"AdjFC_{m}_2026"].sum() if f"AdjFC_{m}_2026" in grp.columns else 0)
             for m in open_ms_avail] +
            [_fmt(grp['_FC_Total'].sum())]
        )
        story += dtbl(fc_hdrs, fc_rows, cw_mo, font_size=8.0)

        # Monthly SO table
        story.append(Paragraph('Confirmed Sales Orders (SO) by Sub-Brand', ST['sub']))
        story.append(Paragraph(
            'Booked customer orders already in the system. Zeros indicate no orders placed yet for that month.',
            ST['source']))
        so_hdrs = ['Sub-Brand'] + open_ms_avail + ['H2 Total']
        so_rows = []
        for _, r in grp.iterrows():
            row = [r['Sub_Brand_Description']]
            for m in open_ms_avail:
                row.append(_fmt(_col_sum(r.to_frame().T, f"SO_{m}_2026")))
            row.append(_fmt(r['_SO_Total']))
            so_rows.append(row)
        so_rows.append(
            ['TOTAL'] +
            [_fmt(grp[f"SO_{m}_2026"].sum() if f"SO_{m}_2026" in grp.columns else 0)
             for m in open_ms_avail] +
            [_fmt(grp['_SO_Total'].sum())]
        )
        story += dtbl(so_hdrs, so_rows, cw_mo, font_size=8.0)

        # SO vs AdjFC summary table
        story.append(Paragraph('SO vs AdjFC — Coverage Summary', ST['sub']))
        sum_hdrs = ['Sub-Brand', 'AdjFC H2 Total', 'SO Total', 'Delta (SO−AdjFC)', 'Coverage %']
        sum_rows = []
        for _, r in grp.iterrows():
            cov = (r['_SO_Total'] / r['_FC_Total'] * 100) if r['_FC_Total'] > 0 else 0
            sum_rows.append([
                r['Sub_Brand_Description'],
                _fmt(r['_FC_Total']),
                _fmt(r['_SO_Total']),
                _fmt(r['_Delta']),
                f"{cov:.0f}%",
            ])
        tot_fc = grp['_FC_Total'].sum()
        tot_so = grp['_SO_Total'].sum()
        tot_cov = (tot_so / tot_fc * 100) if tot_fc > 0 else 0
        sum_rows.append(['TOTAL', _fmt(tot_fc), _fmt(tot_so), _fmt(tot_so - tot_fc),
                          f"{tot_cov:.0f}%"])
        story += dtbl(sum_hdrs, sum_rows, [5.0*cm, 2.5*cm, 2.5*cm, 3.0*cm, 2.0*cm])

        if gpt_client:
            top_brand = grp.nlargest(1, '_FC_Total')['Sub_Brand_Description'].values[0] if not grp.empty else 'N/A'
            commentary = _gpt(
                f"Market: {country} {sub_segment}. "
                f"AdjFC H2 2026: {tot_fc:,.0f} 9LC. SO confirmed: {tot_so:,.0f} 9LC. "
                f"SO coverage: {tot_cov:.1f}%. Top brand by AdjFC: {top_brand}. "
                f"Write 2-3 sentences of analyst insight on the SO vs AdjFC position for this market.",
                gpt_client,
            )
            if commentary:
                story += callout([('💡  ANALYST INSIGHT', 'boxB'), (commentary, 'box')], bg=AMBER)
    else:
        story.append(Paragraph('No open forecast data available for this market.', ST['source']))

    story.append(PageBreak())

    # ══ SECTION 3 — FORECAST ACCURACY ════════════════════════════════════════
    story += section_hdr('3   FORECAST ACCURACY — LAG-3 (M-3)')
    story += [
        Paragraph(
            f'Lag-3 forecast accuracy across all closed months of 2026 '
            f'({", ".join(CLOSED_2026) or "none yet"}). '
            f'wMAPE and Bias calculated at UPC level and aggregated to market.',
            ST['body']),
        sp(4),
    ]

    monthly_stats = []
    if not acc.empty and CLOSED_2026:
        for m in CLOSED_2026:
            fc_c  = f"Fcst3M_{m}_2026"
            act_c = f"Actual_{m}_2026"
            if fc_c not in acc.columns or act_c not in acc.columns:
                continue
            sub = acc[[fc_c, act_c]].copy().fillna(0)
            sub = sub[sub[act_c] > 0]
            if sub.empty:
                continue
            tot_act  = sub[act_c].sum()
            tot_fc   = sub[fc_c].sum()
            tot_err  = (sub[fc_c] - sub[act_c]).abs().sum()
            tot_bias = (sub[fc_c] - sub[act_c]).sum()
            monthly_stats.append({
                'month':   m,
                'Actuals': round(tot_act),
                'Lag3FC':  round(tot_fc),
                'wMAPE':   round(tot_err  / tot_act * 100, 1),
                'Bias':    round(tot_bias / tot_act * 100, 1),
            })

    if monthly_stats:
        chart_buf = _accuracy_chart(monthly_stats)
        chart_h   = W * 3.2 / 6.5   # updated aspect ratio for taller chart
        story.append(Image(chart_buf, width=W, height=chart_h))
        story.append(sp(8))

        # Top-10 table for last closed month
        if LAST_CLOSED and not acc.empty:
            story.append(Paragraph(
                f'3.1  Top-10 Sub-Brands — {LAST_CLOSED} 2026  '
                f'(Lag-3 Forecast vs Actuals)',
                ST['sub']))
            fc_c  = f"Fcst3M_{LAST_CLOSED}_2026"
            act_c = f"Actual_{LAST_CLOSED}_2026"

            if fc_c in acc.columns and act_c in acc.columns:
                top10 = (acc.groupby("Sub_Brand_Description")
                           .agg(IBP=(fc_c, "sum"), Actuals=(act_c, "sum"))
                           .reset_index())
                top10 = top10[top10["Actuals"] > 0]
                top10["Error"] = (top10["IBP"] - top10["Actuals"]).abs()
                top10["MAPE_v"] = top10["Error"] / top10["Actuals"] * 100
                top10["Bias_v"] = (top10["IBP"] - top10["Actuals"]) / top10["Actuals"] * 100
                top10 = top10.nlargest(10, "Actuals")

                tot_ibp  = top10["IBP"].sum()
                tot_act  = top10["Actuals"].sum()
                tot_err  = top10["Error"].sum()
                tot_mape = tot_err / tot_act * 100 if tot_act > 0 else 0
                tot_bias = (tot_ibp - tot_act) / tot_act * 100 if tot_act > 0 else 0

                acc_hdrs = ['Sub-Brand', 'IBP (Lag-3)', 'Actuals', 'Abs Error', 'wMAPE', 'Bias%']
                acc_rows = []
                for _, r in top10.iterrows():
                    acc_rows.append([
                        r["Sub_Brand_Description"],
                        _fmt(r["IBP"]),
                        _fmt(r["Actuals"]),
                        _fmt(r["Error"]),
                        f"{r['MAPE_v']:.1f}%",
                        _pct(r["Bias_v"]),
                    ])
                acc_rows.append(['TOTAL', _fmt(tot_ibp), _fmt(tot_act),
                                  _fmt(tot_err), f"{tot_mape:.1f}%", _pct(tot_bias)])
                story += dtbl(acc_hdrs, acc_rows,
                               [5.0*cm, 2.0*cm, 2.0*cm, 2.0*cm, 1.8*cm, 1.8*cm])

                if gpt_client:
                    worst = top10.nlargest(1, "MAPE_v")
                    commentary = _gpt(
                        f"Market: {country} {sub_segment}. Month: {LAST_CLOSED} 2026. "
                        f"Overall wMAPE: {tot_mape:.1f}%, Bias: {_pct(tot_bias)}. "
                        f"Top error driver: {worst.iloc[0]['Sub_Brand_Description']} "
                        f"(MAPE {worst.iloc[0]['MAPE_v']:.1f}%, Bias {_pct(worst.iloc[0]['Bias_v'])}). "
                        f"Write 2-3 sentences of demand planning insight on this forecast accuracy.",
                        gpt_client,
                    )
                    if commentary:
                        story += callout([('⚠  KEY TAKEAWAY — ACCURACY', 'boxB'),
                                          (commentary, 'box')], bg=AMBER)
    else:
        story.append(Paragraph(
            'No lag-3 accuracy data available for this market.', ST['source']))

    story.append(PageBreak())

    # ══ SECTION 5 — MONTHLY YoY COMPARISON ════════════════════════════════════
    story += section_hdr('5   MONTHLY VOLUME — 2026 vs 2025')
    story += [
        Paragraph(
            f'2026: actuals for closed months (Jan–{LAST_CLOSED or "—"}), '
            f'Adjusted Forecast for open months '
            f'({OPEN_2026[0] if OPEN_2026 else "—"}–Dec). '
            'Compared against 2025 actuals. (A) = Actual, (F) = Forecast.',
            ST['body']),
        sp(4),
    ]

    s5_rows = []
    for m in _ALL_MONTHS:
        if m in CLOSED_2026:
            col26 = f"Actual_{m}_2026"
            tag   = '(A)'
        else:
            col26 = f"AdjFC_{m}_2026"
            tag   = '(F)'
        col25 = f"Actual_{m}_2025"
        v26   = _col_sum(ca, col26)
        v25   = _col_sum(ca, col25)
        yoy   = (v26 - v25) / v25 * 100 if v25 > 0 else None
        s5_rows.append([f"{m} {tag}", _fmt(v25), _fmt(v26), _pct(yoy)])

    tot25 = sum(_col_sum(ca, f"Actual_{m}_2025") for m in _ALL_MONTHS)
    tot26 = sum(
        _col_sum(ca, f"Actual_{m}_2026" if m in CLOSED_2026 else f"AdjFC_{m}_2026")
        for m in _ALL_MONTHS
    )
    s5_rows.append(['TOTAL', _fmt(tot25), _fmt(tot26),
                     _pct((tot26 - tot25) / tot25 * 100 if tot25 > 0 else None)])

    story += dtbl(
        ['Month', '2025 Actuals', '2026 Act/FC', 'YoY %'],
        s5_rows,
        [3.2*cm, 4.0*cm, 4.0*cm, 4.0*cm],
        center_from=1,
    )

    # ══ SECTION 5.3 — DEVIATION FLAGS (TOP 5 SUB-BRANDS) ═════════════════════
    story += [sp(6)] + section_hdr('5.3   DEVIATION FLAGS — TOP 5 SUB-BRANDS')
    story += [
        Paragraph(
            'Top 5 sub-brands by 2026 YTD actual sales. '
            'For each: Adjusted Forecast, Confirmed Sales Orders (SO), and 2025 Actuals '
            'for all remaining open months.',
            ST['body']),
        sp(4),
    ]

    if not ca.empty and OPEN_2026:
        brand_ytd = ca.groupby("Sub_Brand_Description")["_YTD_2026"].sum()
        top5 = brand_ytd.nlargest(5).index.tolist()
        n_open = len(OPEN_2026)
        brand_col_w = 3.5*cm
        month_col_w = (W - brand_col_w) / max(n_open, 1)
        dev_cw = [brand_col_w] + [month_col_w] * n_open

        for brand in top5:
            story.append(Paragraph(f'<b>{brand}</b>', ST['sub']))
            bdf = ca[ca["Sub_Brand_Description"] == brand]

            fc_row  = ['AdjFC 2026']
            so_row  = ['SO 2026']
            a25_row = ['Actuals 2025']
            for m in OPEN_2026:
                fc_row.append( _fmt(_col_sum(bdf, f"AdjFC_{m}_2026")))
                so_row.append( _fmt(_col_sum(bdf, f"SO_{m}_2026")))
                a25_row.append(_fmt(_col_sum(bdf, f"Actual_{m}_2025")))

            story += dtbl(
                ['Metric'] + OPEN_2026,
                [fc_row, so_row, a25_row],
                dev_cw,
            )

        if gpt_client:
            commentary = _gpt(
                f"Market: {country} {sub_segment}. "
                f"Reviewing AdjFC vs confirmed SO vs 2025 actuals for the top 5 sub-brands "
                f"across the open months {', '.join(OPEN_2026)}. "
                f"Write 2-3 sentences on key risks or signals to watch in the upcoming months.",
                gpt_client,
            )
            if commentary:
                story += callout([('💡  ANALYST INSIGHT — DEVIATION FLAGS', 'boxB'),
                                   (commentary, 'box')], bg=AMBER)

    story.append(PageBreak())

    # ══ SECTION 5.6 — CATEGORY SANITY CHECK ══════════════════════════════════
    story += section_hdr('5.6   CATEGORY SANITY CHECK')
    story += [
        Paragraph(
            '2024 actuals, 2025 actuals, 2026 combined (actuals + AdjFC), '
            '2027 full-year AdjFC — by Category. YoY growth % for each transition.',
            ST['body']),
        sp(4),
    ]

    if not ca.empty:
        cat_grp = ca.groupby("Category_Grouper_Description_Z").agg({
            c: "sum" for c in
            ["Actual_Total_2024", "Actual_Total_2025", "Total_2026", "AdjFC_Total_2027"]
            if c in ca.columns
        }).reset_index().rename(columns={"Category_Grouper_Description_Z": "Category"})

        for col in ["Actual_Total_2024", "Actual_Total_2025", "Total_2026", "AdjFC_Total_2027"]:
            if col not in cat_grp.columns:
                cat_grp[col] = 0.0

        cat_grp["26vs25"] = ((cat_grp["Total_2026"] - cat_grp["Actual_Total_2025"]) /
                              cat_grp["Actual_Total_2025"].replace(0, np.nan) * 100)
        cat_grp["27vs26"] = ((cat_grp["AdjFC_Total_2027"] - cat_grp["Total_2026"]) /
                              cat_grp["Total_2026"].replace(0, np.nan) * 100)
        cat_grp = cat_grp.sort_values("Total_2026", ascending=False)

        cat_rows = []
        for _, r in cat_grp.iterrows():
            cat_rows.append([
                r["Category"],
                _fmt(r["Actual_Total_2024"]),
                _fmt(r["Actual_Total_2025"]),
                _fmt(r["Total_2026"]),
                _pct(r["26vs25"]),
                _fmt(r["AdjFC_Total_2027"]),
                _pct(r["27vs26"]),
            ])
        tot24 = cat_grp["Actual_Total_2024"].sum()
        tot25 = cat_grp["Actual_Total_2025"].sum()
        tot26 = cat_grp["Total_2026"].sum()
        tot27 = cat_grp["AdjFC_Total_2027"].sum()
        cat_rows.append([
            'TOTAL', _fmt(tot24), _fmt(tot25), _fmt(tot26),
            _pct((tot26-tot25)/tot25*100 if tot25 > 0 else None),
            _fmt(tot27),
            _pct((tot27-tot26)/tot26*100 if tot26 > 0 else None),
        ])
        story += dtbl(
            ['Category', '2024 Act', '2025 Act', '2026 (A+F)', '26 vs 25', '2027 AdjFC', '27 vs 26'],
            cat_rows,
            [4.0*cm, 1.8*cm, 1.8*cm, 2.0*cm, 1.8*cm, 2.2*cm, 1.9*cm],
        )

        # Quarterly growth 2027 vs 2026
        story += [sp(4), Paragraph('5.6.1  Quarterly Growth — 2027 AdjFC vs 2026', ST['sub'])]
        qtr_map = {'Q1': ['Jan','Feb','Mar'], 'Q2': ['Apr','May','Jun'],
                   'Q3': ['Jul','Aug','Sep'], 'Q4': ['Oct','Nov','Dec']}
        qtr_rows = []
        fy26 = fy27 = 0
        for qtr, months in qtr_map.items():
            q26 = sum(_col_sum(ca, f"Actual_{m}_2026" if m in CLOSED_2026 else f"AdjFC_{m}_2026")
                      for m in months)
            q27 = sum(_col_sum(ca, f"AdjFC_{m}_2027") for m in months)
            fy26 += q26
            fy27 += q27
            pct = (q27 - q26) / q26 * 100 if q26 > 0 else None
            qtr_rows.append([qtr, _fmt(q26), _fmt(q27), _fmt(q27 - q26), _pct(pct)])
        fy_pct = (fy27 - fy26) / fy26 * 100 if fy26 > 0 else None
        qtr_rows.append(['TOTAL (Full Year)', _fmt(fy26), _fmt(fy27),
                          _fmt(fy27 - fy26), _pct(fy_pct)])
        story += dtbl(
            ['Quarter', '2026 (9LC)', '2027 AdjFC (9LC)', 'Delta', 'Growth %'],
            qtr_rows,
            [3.5*cm, 3.0*cm, 3.8*cm, 2.8*cm, 2.4*cm],
            center_from=1,
        )

    story.append(PageBreak())

    # ══ APPENDIX B — MONTHLY HISTORICAL (TOP 3 SUB-BRANDS) ═══════════════════
    story += section_hdr('APPENDIX B   MONTHLY HISTORICAL — TOP 3 SUB-BRANDS')
    story += [
        Paragraph(
            'Monthly order history for the top 3 sub-brands by 2026 YTD volume. '
            '(A) = confirmed actuals. 2026 shows actuals for closed months only.',
            ST['body']),
        sp(4),
    ]

    if not ca.empty:
        top3 = ca.groupby("Sub_Brand_Description")["_YTD_2026"].sum().nlargest(3).index.tolist()
        hist_hdrs = ['Year'] + _ALL_MONTHS + ['Total']
        hist_cw   = [1.0*cm] + [1.25*cm]*12 + [1.45*cm]

        for brand in top3:
            story.append(Paragraph(f'<b>{brand}</b>', ST['sub']))
            bdf = ca[ca["Sub_Brand_Description"] == brand]
            hist_rows = []

            for yr, tot_col in [('2024', 'Actual_Total_2024'), ('2025', 'Actual_Total_2025')]:
                row = [yr]
                for m in _ALL_MONTHS:
                    row.append(_fmt(_col_sum(bdf, f"Actual_{m}_{yr}")))
                row.append(_fmt(_col_sum(bdf, tot_col)))
                hist_rows.append(row)

            row26 = ['2026 (A)']
            for m in _ALL_MONTHS:
                row26.append(_fmt(_col_sum(bdf, f"Actual_{m}_2026")) if m in CLOSED_2026 else '—')
            row26.append(_fmt(bdf["_YTD_2026"].sum()))
            hist_rows.append(row26)

            story += dtbl(hist_hdrs, hist_rows, hist_cw, font_size=7.5)

    story.append(PageBreak())

    # ══ APPENDIX C — TOP-10 PRODUCT RANKINGS ═════════════════════════════════
    story += section_hdr('APPENDIX C   TOP-10 PRODUCT RANKINGS')

    if not ca.empty:
        # YTD 2025 = same closed months as 2026 YTD (apples-to-apples comparison)
        ytd25_cols = [f"Actual_{m}_2025" for m in CLOSED_2026 if f"Actual_{m}_2025" in ca.columns]
        ca_r = ca.copy()
        ca_r["_YTD_2025"] = ca_r[ytd25_cols].sum(axis=1) if ytd25_cols else 0.0

        rank_grp = ca_r.groupby("Sub_Brand_Description").agg(
            Vol_2026=("_YTD_2026", "sum"),
            Vol_2025=("_YTD_2025", "sum"),
            Vol_2025_Full=("Actual_Total_2025", "sum") if "Actual_Total_2025" in ca_r.columns
                          else ("_YTD_2026", "count"),
        ).reset_index()

        if "Actual_Total_2025" not in ca_r.columns:
            rank_grp["Vol_2025_Full"] = 0

        ytd_label = f"Jan–{LAST_CLOSED} 2025" if LAST_CLOSED else "2025 YTD"

        # 2026 YTD ranking
        story.append(Paragraph('Top-10 Sub-Brands by 2026 YTD Actual Volume', ST['sub']))
        rank26 = rank_grp.nlargest(10, "Vol_2026").reset_index(drop=True)
        rank26["Rank"] = range(1, len(rank26)+1)
        rank26["YoY%"] = ((rank26["Vol_2026"] - rank26["Vol_2025"]) /
                           rank26["Vol_2025"].replace(0, np.nan) * 100)
        r26_rows = [
            [r["Rank"], r["Sub_Brand_Description"],
             _fmt(r["Vol_2026"]), _fmt(r["Vol_2025"]), _pct(r["YoY%"])]
            for _, r in rank26.iterrows()
        ]
        story += dtbl(
            ['Rank', 'Sub-Brand', '2026 YTD (9LC)', f'{ytd_label} (9LC)', 'YoY %'],
            r26_rows,
            [1.0*cm, 6.0*cm, 3.2*cm, 3.7*cm, 1.6*cm],
            center_from=2,
        )

        # 2025 full year ranking
        story.append(Paragraph('Top-10 Sub-Brands by 2025 Full Year Actual Volume', ST['sub']))
        rank25 = rank_grp.nlargest(10, "Vol_2025_Full").reset_index(drop=True)
        rank25["Rank"] = range(1, len(rank25)+1)
        r25_rows = [
            [r["Rank"], r["Sub_Brand_Description"],
             _fmt(r["Vol_2025_Full"]), _fmt(r["Vol_2026"])]
            for _, r in rank25.iterrows()
        ]
        story += dtbl(
            ['Rank', 'Sub-Brand', '2025 Full Yr (9LC)', '2026 YTD (9LC)'],
            r25_rows,
            [1.0*cm, 7.5*cm, 3.8*cm, 3.2*cm],
            center_from=2,
        )

    story.append(PageBreak())

    # ══ APPENDIX D — CATEGORY + BRAND FAMILY BREAKDOWN ════════════════════════
    story += section_hdr('APPENDIX D   CATEGORY + BRAND FAMILY BREAKDOWN')
    story += [
        Paragraph(
            '2026 (actuals + AdjFC) vs 2025 actuals by Category and Brand Family. '
            'Delta shown in 9LC and %.',
            ST['body']),
        sp(4),
    ]

    if not ca.empty and "Brand_Family" in ca.columns:
        bf_grp = ca.groupby(["Category_Grouper_Description_Z", "Brand_Family"]).agg({
            c: "sum" for c in ["Actual_Total_2025", "Total_2026"] if c in ca.columns
        }).reset_index().rename(columns={
            "Category_Grouper_Description_Z": "Category",
        })
        for col in ["Actual_Total_2025", "Total_2026"]:
            if col not in bf_grp.columns:
                bf_grp[col] = 0.0

        bf_grp["Delta"]     = bf_grp["Total_2026"] - bf_grp["Actual_Total_2025"]
        bf_grp["Delta_Pct"] = (bf_grp["Delta"] /
                                bf_grp["Actual_Total_2025"].replace(0, np.nan) * 100)
        bf_grp = bf_grp[
            (bf_grp["Brand_Family"].str.strip() != "") &
            (bf_grp["Brand_Family"] != "Unknown") &
            (bf_grp["Category"].str.strip() != "") &
            (bf_grp["Category"] != "Unknown")
        ]
        bf_grp = bf_grp.sort_values(["Category", "Total_2026"], ascending=[True, False])

        bf_rows = []
        for cat in bf_grp["Category"].unique():
            sub = bf_grp[bf_grp["Category"] == cat]
            for _, r in sub.iterrows():
                bf_rows.append([r["Category"], r["Brand_Family"],
                                 _fmt(r["Actual_Total_2025"]), _fmt(r["Total_2026"]),
                                 _fmt(r["Delta"]), _pct(r["Delta_Pct"])])
            s25 = sub["Actual_Total_2025"].sum()
            s26 = sub["Total_2026"].sum()
            d   = s26 - s25
            dp  = d / s25 * 100 if s25 > 0 else None
            bf_rows.append([f"TOTAL — {cat}", '', _fmt(s25), _fmt(s26), _fmt(d), _pct(dp)])

        story += dtbl(
            ['Category', 'Brand Family', '2025 Actuals', '2026 (A+F)', 'Delta (9LC)', 'Delta %'],
            bf_rows,
            [3.5*cm, 3.5*cm, 2.5*cm, 2.5*cm, 2.2*cm, 2.0*cm],
        )

    # ══ FOOTER ════════════════════════════════════════════════════════════════
    story += [
        sp(12),
        HRFlowable(width=W, thickness=0.5, color=GREY),
        sp(4),
        Paragraph(
            f'Demand Planning Team  |  {country} {sub_segment}  |  '
            f'{cycle} Cycle  |  Confidential',
            ST['footer'],
        ),
    ]

    doc.build(story)
    return buf.getvalue()
