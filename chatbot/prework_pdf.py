"""
Pre-Alignment PDF builder — generates a professional pre-work document
for any Country + Sub-Segment from live BigQuery data.
"""
import io
import os
import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

import anthropic
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image, HRFlowable,
)

from prework_queries import (CLOSED_2026, OPEN_2026, LAST_CLOSED, _ALL_MONTHS,
                             lag_months)

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
    'tblH':  _S('tblH',  fontName='Helvetica-Bold', fontSize=7,
                 textColor=WHITE, alignment=TA_CENTER, leading=9),
    'tblL':  _S('tblL',  fontName='Helvetica-Bold', fontSize=7,
                 textColor=NAVY, alignment=TA_LEFT, leading=9),
    'tblR':  _S('tblR',  fontName='Helvetica', fontSize=7,
                 textColor=BODY_C, alignment=TA_CENTER, leading=9),
    # Hierarchy table: level 0 = sub-brand/size, 1 = UPC, 2 = SKU
    'hL0':   _S('hL0',   fontName='Helvetica-Bold', fontSize=7,
                 textColor=NAVY, alignment=TA_LEFT, leading=9),
    'hR0':   _S('hR0',   fontName='Helvetica-Bold', fontSize=7,
                 textColor=NAVY, alignment=TA_CENTER, leading=9),
    'hL1':   _S('hL1',   fontName='Helvetica-Bold', fontSize=6.8,
                 textColor=BODY_C, alignment=TA_LEFT, leading=8.6, leftIndent=6),
    'hR1':   _S('hR1',   fontName='Helvetica', fontSize=6.8,
                 textColor=BODY_C, alignment=TA_CENTER, leading=8.6),
    'hL2':   _S('hL2',   fontName='Helvetica', fontSize=6.3,
                 textColor=GREY, alignment=TA_LEFT, leading=8, leftIndent=16),
    'hR2':   _S('hR2',   fontName='Helvetica', fontSize=6.3,
                 textColor=GREY, alignment=TA_CENTER, leading=8),
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


def dtbl(headers, rows, col_w, center_from=1, font_size=8.5, pad=5):
    """Styled data table. Columns >= center_from are center-aligned.

    `pad` is the horizontal cell padding; drop it on wide tables, where the
    default costs more width than the numbers do.
    """
    def _st(base_key):
        """Return style, optionally with font_size override."""
        if font_size == 8.5:
            return ST[base_key]
        s = ST[base_key]
        return ParagraphStyle(base_key + '_s', parent=s, fontSize=font_size, leading=font_size + 2.5)

    def pc(text, bold=False, center=False, total=False):
        # Cells carry master data, never markup: sub-brands such as "Kraken
        # Cherry&Vanill" must not be read as an entity (ReportLab silently
        # repaired it to "Cherry&Vanill;" before this).
        text = escape(str(text))
        if total:
            return Paragraph(text, _st('cell_tc') if center else _st('cell_t'))
        if bold:
            return Paragraph(text, _st('cell_h'))
        return Paragraph(text, _st('cell_c') if center else _st('cell'))

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
        ('LEFTPADDING',   (0, 0), (-1, -1), pad),
        ('RIGHTPADDING',  (0, 0), (-1, -1), pad),
    ]
    for ri, row in enumerate(rows):
        if str(row[0]).strip().lower().startswith('total'):
            styles += [
                ('BACKGROUND', (0, ri+1), (-1, ri+1), TOTROW),
                ('LINEABOVE',  (0, ri+1), (-1, ri+1), 0.8, NAVY),
            ]
    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(styles))
    return [t, sp(5)]


def htbl(headers, rows, col_w):
    """Drill-down table. Each row is (level, cells): 0 = group, 1 = UPC, 2 = SKU.

    Levels are distinguished by weight, indent and shading rather than by extra
    columns, so the whole hierarchy keeps one set of measure columns and the
    group rows carry exactly the figures of the summary table above it. The
    header repeats when the table runs onto the next page.
    """
    # Cells hold master data, not markup — material descriptions carry "&"
    # ("KRAKEN RUM&COLA"), which ReportLab would otherwise read as an entity.
    def _p(v, style):
        return Paragraph(escape(str(v)), ST[style])

    data = [[_p(h, 'tblH') for h in headers]]
    styles = [
        ('BACKGROUND',    (0, 0), (-1, 0), NAVY),
        ('LINEBELOW',     (0, 0), (-1, 0), 1, NAVY),
        ('GRID',          (0, 0), (-1, -1), 0.25, colors.HexColor('#DDDDDD')),
        ('TOPPADDING',    (0, 0), (-1, -1), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]
    for ri, (level, cells) in enumerate(rows):
        lvl = max(0, min(2, int(level)))
        data.append([_p(v, f'hL{lvl}' if ci == 0 else f'hR{lvl}')
                     for ci, v in enumerate(cells)])
        if lvl == 0:
            styles += [
                ('BACKGROUND', (0, ri + 1), (-1, ri + 1), LBLUE),
                ('LINEABOVE',  (0, ri + 1), (-1, ri + 1), 0.7, NAVY),
            ]
    t = Table(data, colWidths=col_w, repeatRows=1)
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


# ── Pack-size successions ─────────────────────────────────────────────────────
# Aera cannot chain a retiring SKU to a new one across pack sizes — SKU Updates
# rejects it with SUBBRAND_SIZE_MISMATCH — so a pack changeover leaves the
# forecast stranded on the dead code while sales land on the new one, and
# accuracy reports it twice: once as forecast with no sales, once as sales with
# no forecast. These pairs are the chains the business intended (as filed in
# SKU_Update_upload_Kraken_330_to_375.csv), applied here for reporting only.
SKU_SUCCESSORS = {
    "311808": "311814",   # Kraken Rum & Cola 6x4 330ml -> 375ml
    "311809": "311815",   # Kraken Rum & Dry  6x4 330ml -> 375ml
    "311812": "311814",   # Kraken Rum & Cola 10x3 330ml -> 6x4 375ml
}


def _n_plus(k: int):
    """The month k ahead of the current one, as (abbrev, year).

    n+3 is read off the live calendar, not off the reported accuracy month: in
    October the planning question is January, and in November it becomes
    February without anyone editing this file.
    """
    z = datetime.date.today()
    m = z.month - 1 + k
    return _ALL_MONTHS[m % 12], z.year + m // 12


def _chain(df, cols):
    """Re-key retired pack codes onto their successor, carrying `cols` over.

    Applied to the order-history frame as well as the accuracy frame, so a
    changeover's prior-year sales land on the line that replaced it instead of
    on a code that no longer appears.
    """
    hit = df["Material_Number"].isin(SKU_SUCCESSORS)
    if not hit.any():
        return df
    succ = df.loc[hit, "Material_Number"].map(SKU_SUCCESSORS)
    attr = (df[~hit].drop_duplicates("Material_Number")
                    .set_index("Material_Number")[list(cols)])
    for c in cols:
        df.loc[hit, c] = succ.map(attr[c]).fillna(df.loc[hit, c])
    df.loc[hit, "Material_Number"] = succ
    return df


def _attach_grain(frames):
    """Label every frame with the same Sub-Brand / Size grain.

    The canonical sub-brand map and the modal Material -> (sub-brand, size)
    table are built from all the frames together. Built per frame instead, the
    accuracy rows and the order-history rows can disagree on one product's
    spelling or size format, and it then appears as two half-populated lines.
    """
    allsb = pd.concat([f["Sub_Brand_Description"] for f in frames], ignore_index=True)
    cmap  = _canon_subbrand_map(allsb)
    out = []
    for f in frames:
        f = f.copy()
        f["Sub_Brand_Description"] = (f["Sub_Brand_Description"].map(cmap)
                                      .fillna(f["Sub_Brand_Description"]))
        f["_size"] = f["Volume"].apply(_fmt_size)
        out.append(f)
    modal = (pd.concat([f[["Material_Number", "Sub_Brand_Description", "_size"]]
                        for f in out], ignore_index=True)
               .groupby(["Material_Number", "Sub_Brand_Description", "_size"])
               .size().reset_index(name="_n")
               .sort_values(["Material_Number", "_n"], ascending=[True, False])
               .drop_duplicates("Material_Number")
               [["Material_Number", "Sub_Brand_Description", "_size"]])
    final = []
    for f in out:
        f = (f.drop(columns=["Sub_Brand_Description", "_size"])
               .merge(modal, on="Material_Number", how="left"))
        f["_grain"] = (f["Sub_Brand_Description"].astype(str)
                       + f["_size"].apply(lambda s: f" / {s}" if s else ""))
        final.append(f)
    return final


def _fmt_size(v):
    """Pack size as the BI report shows it: 0.3300 -> 0.33, 1.0000 -> 1."""
    try:
        return "%g" % float(str(v))
    except (TypeError, ValueError):
        s = str(v).strip()
        return "" if s.lower() in ("", "nan", "none") else s


def _canon_subbrand_map(values) -> dict:
    """Map each raw sub-brand spelling to one canonical label.

    The master data carries the same sub-brand two ways — "JOSE CUERVO ESPECIAL
    SILVER" alongside "JC Especial Silver", and names truncated at 20 chars
    ("JC Sparkling Margari" for "...Margarita"). Left alone these split one
    product across several rows of the table. Merge on the normalised form, and
    only treat a prefix as a truncation when it really looks like one, so
    genuinely distinct short sub-brands are never folded together.
    """
    raws = [v for v in dict.fromkeys(values) if str(v).strip() not in ("", "nan", "None")]
    norm = {v: " ".join(str(v).upper().split()).replace("JOSE CUERVO", "JC") for v in raws}

    canon: dict = {}
    for k in sorted(set(norm.values()), key=len):          # shortest first
        # canon only holds keys already seen, i.e. no longer than k
        match = next((c for c in canon
                      if k.startswith(c) and len(c) >= 15 and len(k) - len(c) <= 6), None)
        canon[k] = canon[match] if match else k

    label: dict = {}                                        # longest spelling wins
    for raw, n in norm.items():
        c = canon[n]
        if c not in label or len(str(raw)) > len(str(label[c])):
            label[c] = raw
    return {raw: label[canon[n]] for raw, n in norm.items()}


def _col_sum(df, col):
    return df[col].sum() if col in df.columns else 0


def _upc_key(df: pd.DataFrame) -> pd.Series:
    """Grain key for forecast accuracy: the UPC code, falling back to sub-brand
    where no UPC is on the record.

    Accuracy is measured per UPC (customers netted within it), which is the
    convention of Aera's "Accuracy UPC Code" tab and the Power BI report. A
    UPC is one physical pack, so when a material code is superseded — a new
    vintage-year or repack code for the same product — both codes share a UPC
    and the switch stops counting as forecast error, which is the point.

    Roughly 14% of volume carries no UPC (mostly new/NPD material codes not yet
    in the master). Those fall back to sub-brand rather than to their own SKU,
    because those codes are precisely the ones mid-migration: keying them to
    the SKU would book the migration as error, the opposite of the UPC view.
    """
    upc = df["UPC_Code"].fillna("").astype(str).str.strip()
    return upc.where(upc.ne(""), "SB:" + df["Sub_Brand_Description"].astype(str))


# ── Claude commentary ─────────────────────────────────────────────────────────
def _gpt(prompt: str, client: "anthropic.Anthropic") -> str:
    try:
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=160,
            system=(
                "You are a concise demand planning analyst. Write 2-3 sentences "
                "of professional insight. No bullet points. No headers. Be direct and specific."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        # Escaped: the commentary quotes product names back ("Kraken
        # Cherry&Vanilla"), and it is rendered as a ReportLab paragraph.
        return escape("".join(b.text for b in resp.content
                              if b.type == "text").strip())
    except Exception:
        return ""


# ── Accuracy bar chart ────────────────────────────────────────────────────────
def _accuracy_chart(monthly_stats: list[dict]) -> io.BytesIO:
    months = [d['month']   for d in monthly_stats]
    acts   = [d['Actuals'] for d in monthly_stats]
    lag3s  = [d['IBPFC']   for d in monthly_stats]
    wmapes = [d['wMAPE']   for d in monthly_stats]
    biases = [d['Bias']    for d in monthly_stats]

    x = np.arange(len(months))
    w = 0.35

    fig, ax1 = plt.subplots(figsize=(6.5, 3.2))

    # Left axis — volume bars
    ax1.bar(x - w/2, acts,  w, label='Actuals (9LC)',    color='#1B2B4B', alpha=0.88)
    ax1.bar(x + w/2, lag3s, w, label='IBP n-3 Fcst (9LC)', color='#90B4D4', alpha=0.90)
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

    ax1.set_title('IBP n-3 Forecast Accuracy — 2026 YTD', fontsize=9,
                  fontweight='bold', color='#1B2B4B', pad=8)
    fig.tight_layout(pad=0.8)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf


# ── Anthropic client ──────────────────────────────────────────────────────────
def _openai_client() -> "anthropic.Anthropic":
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        try:
            import streamlit as st
            key = st.secrets.get("ANTHROPIC_API_KEY")
        except Exception:
            pass
    if not key:
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return anthropic.Anthropic(api_key=key) if key else None


# ══════════════════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════════════════
def build_prework_pdf(
    ca: pd.DataFrame,
    acc: pd.DataFrame,
    country: str,
    sub_segment: str,
    customer_name=None,
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
    _cover_title = (
        f'{country.upper()}  ·  {sub_segment}  ·  {customer_name}\nPRE-ALIGNMENT MEETING'
        if customer_name else
        f'{country.upper()}  ·  {sub_segment}\nPRE-ALIGNMENT MEETING'
    )
    cover_top = Table(
        [[Paragraph(_cover_title, ST['cover1'])]],
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
        ('3', 'Forecast Accuracy — IBP n-3'),
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
    _cust_part  = f'  ·  <b>Customer:</b> {customer_name}' if customer_name else ''
    scope_line  = (f'<b>Region:</b> {region}  ·  <b>Market:</b> {country}  ·  '
                   f'<b>Sub-Segment:</b> {sub_segment}{_cust_part}  ·  '
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
        bul('Forecast Accuracy (IBP n-3) — wMAPE &amp; Bias across all closed 2026 months'),
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

    if not ca.empty:
        # ── Market-level monthly summary (Jan–Dec, like Aera view) ───────────
        story.append(Paragraph('Market Monthly View — Full Year 2026', ST['sub']))
        story.append(Paragraph(
            '(A) = closed month actual  ·  (F) = open month Adjusted Forecast  ·  SO = confirmed sales orders',
            ST['source']))

        lbl_w  = 2.4 * cm
        ytd_w  = 1.7 * cm
        m_w    = (W - lbl_w - ytd_w) / 12

        # Header row — mark closed vs open months
        def _mhdr(m):
            tag = '(A)' if m in CLOSED_2026 else '(F)'
            return Paragraph(f'<b>{m}</b><br/><font size="5">{tag}</font>', ST['tblH'])

        mkt_hdrs_row = (
            [Paragraph('<b>Metric</b>', ST['tblH'])] +
            [_mhdr(m) for m in _ALL_MONTHS] +
            [Paragraph('<b>YTD</b>', ST['tblH'])]
        )

        def _mkt_val(m):
            if m in CLOSED_2026:
                return _col_sum(ca, f"Actual_{m}_2026")
            col = f"AdjFC_{m}_2026"
            return _col_sum(ca, col) if col in ca.columns else 0

        def _so_val(m):
            if m in CLOSED_2026:
                return _col_sum(ca, f"Actual_{m}_2026")
            col = f"SO_{m}_2026"
            return _col_sum(ca, col) if col in ca.columns else 0

        def _a25_val(m):
            col = f"Actual_{m}_2025"
            return _col_sum(ca, col) if col in ca.columns else 0

        act_fc_vals = [_mkt_val(m) for m in _ALL_MONTHS]
        so_vals     = [_so_val(m)  for m in _ALL_MONTHS]
        a25_vals    = [_a25_val(m) for m in _ALL_MONTHS]

        ytd_actfc = sum(_mkt_val(m) for m in CLOSED_2026)
        ytd_so    = sum(_so_val(m)  for m in CLOSED_2026)
        ytd_a25   = sum(_a25_val(m) for m in CLOSED_2026)

        def _yoy(v26, v25):
            if v25 == 0:
                return '—'
            pct = (v26 - v25) / v25 * 100
            return f'{pct:+.1f}%'

        yoy_vals = [_yoy(act_fc_vals[i], a25_vals[i]) for i in range(12)]
        ytd_yoy  = _yoy(ytd_actfc, ytd_a25)

        mkt_data = [
            mkt_hdrs_row,
            ([Paragraph('AdjFC / Actuals', ST['tblL'])] +
             [Paragraph(_fmt(v), ST['tblR']) for v in act_fc_vals] +
             [Paragraph(_fmt(ytd_actfc), ST['tblR'])]),
            ([Paragraph('Confirmed SO', ST['tblL'])] +
             [Paragraph(_fmt(v), ST['tblR']) for v in so_vals] +
             [Paragraph(_fmt(ytd_so), ST['tblR'])]),
            ([Paragraph('2025 Actuals', ST['tblL'])] +
             [Paragraph(_fmt(v), ST['tblR']) for v in a25_vals] +
             [Paragraph(_fmt(ytd_a25), ST['tblR'])]),
            ([Paragraph('YoY %', ST['tblL'])] +
             [Paragraph(v, ST['tblR']) for v in yoy_vals] +
             [Paragraph(ytd_yoy, ST['tblR'])]),
        ]

        cw_mkt = [lbl_w] + [m_w] * 12 + [ytd_w]
        mkt_tbl = Table(mkt_data, colWidths=cw_mkt, repeatRows=1)
        _closed_idx = [i + 1 for i, m in enumerate(_ALL_MONTHS) if m in CLOSED_2026]
        _open_idx   = [i + 1 for i, m in enumerate(_ALL_MONTHS) if m not in CLOSED_2026]
        mkt_style = [
            ('BACKGROUND',   (0, 0),  (-1, 0),   NAVY),
            ('TEXTCOLOR',    (0, 0),  (-1, 0),   WHITE),
            ('FONTNAME',     (0, 0),  (-1, -1),  'Helvetica'),
            ('FONTSIZE',     (0, 0),  (-1, -1),  7),
            ('LEADING',      (0, 0),  (-1, -1),  9),
            ('ALIGN',        (0, 0),  (-1, -1),  'CENTER'),
            ('ALIGN',        (0, 0),  (0, -1),   'LEFT'),
            ('VALIGN',       (0, 0),  (-1, -1),  'MIDDLE'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ALTROW]),
            ('GRID',         (0, 0),  (-1, -1),  0.3, colors.HexColor('#CCCCCC')),
            ('TOPPADDING',   (0, 0),  (-1, -1),  3),
            ('BOTTOMPADDING',(0, 0),  (-1, -1),  3),
            ('LEFTPADDING',  (0, 0),  (-1, -1),  2),
            ('RIGHTPADDING', (0, 0),  (-1, -1),  2),
            # YoY row — last row, italic grey
            ('TEXTCOLOR',    (0, 4),  (-1, 4),   GREY),
            # Shade closed months slightly
            ('BACKGROUND',   (0, 1),  (0, -1),   colors.HexColor('#EEF2F8')),
        ]
        # Highlight open month SO cols (may show 0 — flag visually)
        for col_i in _open_idx:
            mkt_style.append(('TEXTCOLOR', (col_i, 2), (col_i, 2), colors.HexColor('#888888')))
        mkt_tbl.setStyle(TableStyle(mkt_style))
        story += [mkt_tbl, sp(10)]

        if gpt_client and fc_cols:
            _h2_fc  = sum(_mkt_val(m) for m in OPEN_2026)
            _h2_so  = sum(_so_val(m)  for m in OPEN_2026)
            _h2_cov = (_h2_so / _h2_fc * 100) if _h2_fc > 0 else 0
            commentary = _gpt(
                f"Market: {country} {sub_segment}. "
                f"YTD 2026 Actuals: {ytd_actfc:,.0f} 9LC vs {ytd_a25:,.0f} 9LC in 2025 ({ytd_yoy} YoY). "
                f"H2 AdjFC: {_h2_fc:,.0f} 9LC. SO confirmed: {_h2_so:,.0f} 9LC ({_h2_cov:.1f}% coverage). "
                f"Write 2-3 sentences of analyst insight on the full-year position and H2 SO coverage risk.",
                gpt_client,
            )
            if commentary:
                story += callout([('■  ANALYST INSIGHT', 'boxB'), (commentary, 'box')], bg=AMBER)

    story.append(PageBreak())

    # ══ SECTION 3 — FORECAST ACCURACY ════════════════════════════════════════
    story += section_hdr('3   FORECAST ACCURACY — IBP n-3')

    monthly_stats = []
    if not acc.empty and CLOSED_2026:
        for m in CLOSED_2026:
            fc_c  = f"IBP_{m}_2026"
            act_c = f"Actual_{m}_2026"
            if fc_c not in acc.columns or act_c not in acc.columns:
                continue
            sub = acc[["UPC_Code", "Sub_Brand_Description", fc_c, act_c]].copy()
            sub[[fc_c, act_c]] = sub[[fc_c, act_c]].fillna(0)
            # Net customers within each UPC, then sum absolute errors across UPCs
            # (Aera "Accuracy UPC Code" tab / Power BI Mape. UPC).
            sub["_k"] = _upc_key(sub)
            sub = sub.groupby("_k", as_index=False)[[fc_c, act_c]].sum()
            # keep UPCs with forecast OR actuals — dropping forecast-only rows
            # hides pure over-forecast error from wMAPE
            sub = sub[(sub[act_c] > 0) | (sub[fc_c] > 0)]
            if sub.empty:
                continue
            tot_act  = sub[act_c].sum()
            tot_fc   = sub[fc_c].sum()
            # Skip months where actuals are <15% of the n-3 forecast — booked SOs make
            # recent months much more complete; 15% catches genuine data-not-yet-available
            if tot_fc > 0 and tot_act / tot_fc < 0.15:
                continue
            tot_err  = (sub[fc_c] - sub[act_c]).abs().sum()
            tot_bias = (sub[fc_c] - sub[act_c]).sum()
            monthly_stats.append({
                'month':   m,
                'Actuals': round(tot_act),
                'IBPFC':   round(tot_fc),
                'wMAPE':   round(tot_err  / tot_act * 100, 1),
                'Bias':    round(tot_bias / tot_act * 100, 1),
            })

    # Description uses actual available months, not CLOSED_2026 (pipeline may lag by 1 month)
    avail_months = [d['month'] for d in monthly_stats] if monthly_stats else []
    months_str   = ", ".join(avail_months) if avail_months else "none yet"
    story.append(Paragraph(
        f'IBP n-3 forecast accuracy across the closed months of 2026 '
        f'({months_str}). For {country} {sub_segment} that is the consensus '
        f'snapshot frozen <b>{lag_months(country, sub_segment)} calendar months</b> '
        f'before the target month, matching this market\'s Power BI accuracy '
        f'report — the IBP lock follows each market\'s review calendar, so the '
        f'snapshot distance is set per market rather than assumed. '
        f'wMAPE and Bias are measured at UPC level — customers netted within each '
        f'UPC, then absolute errors summed across UPCs — matching Aera\'s '
        f'"Accuracy UPC Code" tab and the Power BI Mape. UPC measure. Error ABS UPC '
        f'counts only materials carrying a UPC, as that measure does; roughly 15% '
        f'of rows have none in the Aera master, so their volume shows but their '
        f'error does not. Section 3.2 opens each line down to UPC and material code.',
        ST['body']))
    story.append(sp(4))

    if monthly_stats:
        chart_buf = _accuracy_chart(monthly_stats)
        # Trimmed from 3.2 so the widened 3.1 table below still lands on this page
        chart_h   = W * 2.7 / 6.5
        story.append(Image(chart_buf, width=W, height=chart_h))
        story.append(sp(8))

        # Top-10 table for last available month in acc (may be earlier than LAST_CLOSED
        # if the pipeline hasn't uploaded the most recent closed month yet)
        acc_last = monthly_stats[-1]['month'] if monthly_stats else None
        if acc_last and not acc.empty:
            story.append(Paragraph(
                f'3.1  Last Month Top 10 By Volume — {acc_last} 2026  '
                f'(IBP n-3 vs Actual Sales, by Sub-Brand / Size)',
                ST['sub']))
            _n3m, _n3y = _n_plus(3)
            story.append(Paragraph(
                f'The first eight columns replicate the Power BI view; the last three '
                f'are reference, not accuracy. <b>{acc_last} 25 Act</b> is the same '
                f'month a year earlier. <b>{_n3m} {str(_n3y)[2:]} AdjFC n+3</b> is the '
                f'plan standing three months out from the live cycle — the month this '
                f'review can still change — and <b>{_n3m} {str(_n3y - 1)[2:]} Act</b> '
                f'is what that month delivered last year, to judge it against. n+3 '
                f'advances with the calendar: next cycle it reads '
                f'{_n_plus(4)[0]} {_n_plus(4)[1]}.',
                ST['source']))
            story.append(sp(3))
            fc_c  = f"IBP_{acc_last}_2026"
            act_c = f"Actual_{acc_last}_2026"

            if fc_c in acc.columns and act_c in acc.columns:
                # Laid out to mirror the Power BI "Last Month Top 10 By Volume"
                # KPI view, including its sign conventions:
                #   Forecast Error = Actual - IBP   (negative = sold below plan)
                #   Error ABS UPC  = sum of |error| measured at UPC grain
                #   % VOL          = share of the WHOLE market's actual volume
                #   Mape. UPC      = Error ABS UPC / Actual
                #   BIAS           = Forecast Error / IBP
                _keep = ["Material_Number", "Sub_Brand_Description", "Volume",
                         "UPC_Code", "Material_Long_Description", fc_c, act_c]
                if "Material_Long_Description" not in acc.columns:
                    acc = acc.assign(Material_Long_Description="")
                acc_w = acc[_keep].copy()
                acc_w[[fc_c, act_c]] = acc_w[[fc_c, act_c]].fillna(0)
                # Re-key retired packs onto their successor so the changeover is
                # one line instead of two mirror-image errors. The successor's
                # sub-brand/size carries over but NOT its UPC — the new codes
                # have none, the retiring ones do, and the UPC is what the error
                # measure keys on.
                acc_w = _chain(acc_w, ["Sub_Brand_Description", "Volume",
                                       "Material_Long_Description"])

                # Prior-year sales and the n+3 plan come from order history, not
                # from the lag table, so they are prepared alongside and grained
                # with the same maps further down.
                sply_c   = f"Actual_{acc_last}_2025"
                n3_m, n3_y = _n_plus(3)
                n3_c     = f"AdjFC_{n3_m}_{n3_y}"
                n3_sply  = f"Actual_{n3_m}_{n3_y - 1}"
                extra_cols = [c for c in (sply_c, n3_c, n3_sply) if c in ca.columns]
                hist = None
                if extra_cols:
                    hist = ca[["Material_Number", "Sub_Brand_Description", "Volume"]
                              + extra_cols].copy()
                    hist[extra_cols] = hist[extra_cols].fillna(0)
                    hist = _chain(hist, ["Sub_Brand_Description", "Volume"])

                # UPC is a property of the material, not of the customer row, but
                # it is blank on some rows of a material and populated on others.
                # Fill each material from whichever of its rows carries one — this
                # also lets a chained pack inherit its predecessor's UPC.
                _u = acc_w["UPC_Code"].fillna("").astype(str).str.strip()
                _first = (acc_w.assign(_u=_u)[lambda d: d["_u"] != ""]
                               .drop_duplicates("Material_Number")
                               .set_index("Material_Number")["_u"])
                acc_w["UPC_Code"] = _u.where(_u != "",
                                             acc_w["Material_Number"].map(_first)).fillna("")
                # Canonicalise BEFORE building the UPC key: ~15% of rows carry
                # no UPC (Aera's own master has "Not Set" for them), so they fall
                # back to sub-brand — which must already be the canonical spelling
                # or the same product splits into several UPC groups. One
                # sub-brand/size per MATERIAL, taken as the modal value, because a
                # minority of rows carry an alternate spelling ("JOSE CUERVO
                # SPARKLING MARGARITA" vs "JC Sparkling Margari") or size format
                # (0.3300 vs 0.33); grouping row-by-row would split one product
                # across several table rows and understate each. 22 materials in
                # Australia IMC alone are affected.
                _cmap0 = _canon_subbrand_map(acc_w["Sub_Brand_Description"])
                acc_w["Sub_Brand_Description"] = (acc_w["Sub_Brand_Description"]
                                                  .map(_cmap0)
                                                  .fillna(acc_w["Sub_Brand_Description"]))
                acc_w["_k"] = _upc_key(acc_w)
                if hist is not None:
                    acc_w, hist = _attach_grain([acc_w, hist])
                else:
                    acc_w, = _attach_grain([acc_w])
                # Volume columns cover every row. Error ABS UPC covers only
                # materials that actually carry a UPC — the Power BI measure
                # iterates over UPC, so a material whose UPC is "Not Set" in the
                # Aera master (~15% of rows) contributes no error while its
                # volume still counts. Reproduced here for comparability; it does
                # mean a wholly unforecast no-UPC line can show 0% MAPE.
                vol_df = acc_w.groupby("_grain", as_index=False)[[fc_c, act_c]].sum()
                has_upc = acc_w[acc_w["UPC_Code"].fillna("").astype(str).str.strip() != ""]
                per_upc = (has_upc.groupby(["_grain", "_k"], as_index=False)[[fc_c, act_c]].sum())
                per_upc["_abs_err"] = (per_upc[fc_c] - per_upc[act_c]).abs()
                err_df = (per_upc.groupby("_grain", as_index=False)["_abs_err"].sum()
                                 .rename(columns={"_abs_err": "Error"}))
                rows_df = (vol_df.merge(err_df, on="_grain", how="left")
                                 .rename(columns={fc_c: "IBP", act_c: "Actuals"}))
                rows_df["Error"] = rows_df["Error"].fillna(0)
                market_vol = rows_df["Actuals"].sum()      # whole market, not just top 10
                rows_df = rows_df[rows_df["Actuals"] > 0]
                rows_df["FcstErr"] = rows_df["Actuals"] - rows_df["IBP"]
                rows_df["PctVol"]  = rows_df["Actuals"] / market_vol * 100 if market_vol else 0
                rows_df["MAPE_v"]  = rows_df["Error"]   / rows_df["Actuals"] * 100
                # BIAS is undefined with no plan; PBI shows 100% there
                rows_df["Bias_v"]  = rows_df.apply(
                    lambda r: 100.0 if r["IBP"] == 0 else r["FcstErr"] / r["IBP"] * 100, axis=1)
                # Prior-year sales and the n+3 plan, summed on the same grain.
                # Order history covers materials the lag table does not, so these
                # are joined onto the grain rather than carried through acc_w.
                if hist is not None:
                    rows_df = rows_df.merge(
                        hist.groupby("_grain", as_index=False)[extra_cols].sum(),
                        on="_grain", how="left")
                    rows_df[extra_cols] = rows_df[extra_cols].fillna(0)
                top10 = rows_df.nlargest(10, "Actuals")

                tot_ibp  = top10["IBP"].sum()
                tot_act  = top10["Actuals"].sum()
                tot_err  = top10["Error"].sum()
                tot_fe   = tot_act - tot_ibp
                tot_pct  = tot_act / market_vol * 100 if market_vol else 0
                tot_mape = tot_err / tot_act * 100 if tot_act > 0 else 0
                tot_bias = tot_fe / tot_ibp * 100 if tot_ibp > 0 else 100.0

                # The PBI replica, then the three forward/backward reference
                # columns: last year's same month, the n+3 plan, and the month
                # n+3 will be compared against a year from now.
                acc_hdrs = ['Sub Brand / Size', 'IBP', 'Actual Sales', 'Forecast Error',
                            'Error ABS UPC', '% VOL', 'Mape. UPC', 'BIAS']
                acc_cw   = [4.6*cm, 1.9*cm, 2.1*cm, 2.2*cm, 2.2*cm, 1.6*cm, 1.9*cm, 1.7*cm]
                if extra_cols:
                    _y2 = str(n3_y)[2:]
                    acc_hdrs = (['Sub Brand / Size', 'IBP', 'Actual Sales', 'Fcst Error',
                                 'Error ABS UPC', '% VOL', 'Mape. UPC', 'BIAS']
                                + [{sply_c:  f'{acc_last} 25 Act',
                                    n3_c:    f'{n3_m} {_y2} AdjFC n+3',
                                    n3_sply: f'{n3_m} {int(_y2) - 1} Act'}[c]
                                   for c in extra_cols])
                    acc_cw   = ([4.4*cm, 1.2*cm, 1.3*cm, 1.3*cm, 1.5*cm,
                                 1.1*cm, 1.55*cm, 1.45*cm]
                                + [1.25*cm, 1.25*cm, 1.2*cm][:len(extra_cols)])

                def _acc_row(label, ibp, act, fe, err, pct, mape, bias, extras):
                    return ([label, _fmt(ibp), _fmt(act), _fmt(fe), f"{err:,.2f}",
                             f"{pct:.2f}%", f"{mape:.2f}%", f"{bias:.2f}%"]
                            + [_fmt(v) for v in extras])

                acc_rows = [
                    _acc_row(r["_grain"], r["IBP"], r["Actuals"], r["FcstErr"],
                             r["Error"], r["PctVol"], r["MAPE_v"], r["Bias_v"],
                             [r[c] for c in extra_cols])
                    for _, r in top10.iterrows()
                ]
                acc_rows.append(
                    _acc_row('TOTAL', tot_ibp, tot_act, tot_fe, tot_err,
                             tot_pct, tot_mape, tot_bias,
                             [top10[c].sum() for c in extra_cols]))
                story += dtbl(acc_hdrs, acc_rows, acc_cw,
                              font_size=7 if extra_cols else 8.5,
                              pad=3 if extra_cols else 5)

                if gpt_client:
                    worst = top10.nlargest(1, "MAPE_v")
                    commentary = _gpt(
                        f"Market: {country} {sub_segment}. Month: {acc_last} 2026. "
                        f"Overall wMAPE: {tot_mape:.1f}%, Bias: {_pct(tot_bias)}. "
                        f"Top error driver: {worst.iloc[0]['_grain']} "
                        f"(MAPE {worst.iloc[0]['MAPE_v']:.1f}%, Bias {_pct(worst.iloc[0]['Bias_v'])}). "
                        f"Write 2-3 sentences of demand planning insight on this forecast accuracy.",
                        gpt_client,
                    )
                    if commentary:
                        story += callout([('⚠  KEY TAKEAWAY — ACCURACY', 'boxB'),
                                          (commentary, 'box')], bg=AMBER)

                # ── 3.2 UPC / SKU drill-down ──────────────────────────────────
                # Same ten rows, opened up one level at a time, so it is visible
                # which UPC and which material code drive each line's error. The
                # level-0 rows are byte-identical to 3.1 above.
                story.append(PageBreak())
                story.append(Paragraph(
                    f'3.2  UPC / SKU Breakdown of the Top 10 — {acc_last} 2026',
                    ST['sub']))
                story.append(Paragraph(
                    'Each sub-brand/size from 3.1 opened up to the UPCs it contains, '
                    'and each UPC to its material codes, ordered by actual sales. '
                    'Error ABS on a <b>UPC</b> row is the Power BI measure: the SKUs and '
                    'customers inside that UPC are netted first, then the absolute '
                    'error is taken — which is why two material codes can each show '
                    'a large error while their shared UPC shows almost none (a '
                    'vintage-year or repack changeover). Error ABS on a <b>SKU</b> row is '
                    'that code\'s own absolute error and is shown for diagnosis only; '
                    'SKU rows do not sum to the UPC figure. Values in brackets are '
                    'excluded from the reported measure because the material carries '
                    'no UPC in the Aera master. SKUs with no plan and no sales in the '
                    'month are omitted.',
                    ST['source']))
                story.append(sp(4))

                # Material description, taken from whichever row carries one
                _d = (acc_w["Material_Long_Description"].fillna("")
                      .astype(str).str.strip())
                _dmap = (acc_w.assign(_d=_d)[lambda x: x["_d"] != ""]
                              .drop_duplicates("Material_Number")
                              .set_index("Material_Number")["_d"])
                acc_w["_upc"] = acc_w["UPC_Code"].fillna("").astype(str).str.strip()

                upc_lvl = acc_w.groupby(["_grain", "_upc"], as_index=False)[[fc_c, act_c]].sum()
                sku_lvl = acc_w.groupby(["_grain", "_upc", "Material_Number"],
                                        as_index=False)[[fc_c, act_c]].sum()

                def _m(ibp, act, err, counted=True):
                    """The 7 measure cells for one row, in 3.1's conventions."""
                    fe   = act - ibp
                    pct  = act / market_vol * 100 if market_vol else 0.0
                    bias = 100.0 if ibp == 0 else fe / ibp * 100
                    mape = err / act * 100 if act > 0 else None
                    wrap = (lambda s: s) if counted else (lambda s: f'[{s}]')
                    return [_fmt(ibp), _fmt(act), _fmt(fe),
                            wrap(f'{err:,.2f}'),
                            f'{pct:.2f}%',
                            wrap(f'{mape:.2f}%') if mape is not None else '—',
                            f'{bias:.2f}%']

                brk_rows   = []
                uncounted  = 0.0
                for _, g in top10.iterrows():
                    grain = g["_grain"]
                    brk_rows.append((0, [grain] + _m(g["IBP"], g["Actuals"], g["Error"])))

                    ug = (upc_lvl[upc_lvl["_grain"] == grain]
                          .sort_values(act_c, ascending=False))
                    for _, ur in ug.iterrows():
                        upc      = ur["_upc"]
                        has_code = upc != ""
                        if ur[fc_c] == 0 and ur[act_c] == 0:
                            continue          # dormant code, nothing to explain
                        sg = (sku_lvl[(sku_lvl["_grain"] == grain)
                                      & (sku_lvl["_upc"] == upc)]
                              .assign(_err=lambda d: (d[fc_c] - d[act_c]).abs())
                              .sort_values(act_c, ascending=False))
                        sg = sg[(sg[fc_c] != 0) | (sg[act_c] != 0)]
                        # A UPC nets its SKUs; with no UPC there is nothing to net
                        # against, so sum the codes' own errors to show the size
                        # of what the reported measure leaves out.
                        u_err = (abs(ur[fc_c] - ur[act_c]) if has_code
                                 else sg["_err"].sum())
                        if not has_code:
                            uncounted += u_err

                        def _sku_label(r):
                            d = _dmap.get(r["Material_Number"], "")
                            d = (d[:34] + '…') if len(d) > 35 else d
                            return f'{r["Material_Number"]}  {d}'.strip()

                        if len(sg) <= 1:
                            # Nothing to drill into — fold the single code into
                            # the UPC row rather than repeating the numbers.
                            head = (f'UPC {upc}' if has_code
                                    else 'No UPC in Aera master')
                            if len(sg) == 1:
                                head = f'{head} · {_sku_label(sg.iloc[0])}'
                            brk_rows.append((1, [head]
                                             + _m(ur[fc_c], ur[act_c], u_err, has_code)))
                            continue

                        head = (f'UPC {upc}' if has_code else
                                'No UPC in Aera master — error not reported')
                        brk_rows.append((1, [f'{head}  ({len(sg)} SKUs)']
                                         + _m(ur[fc_c], ur[act_c], u_err, has_code)))
                        for _, sr in sg.iterrows():
                            brk_rows.append((2, [_sku_label(sr)]
                                             + _m(sr[fc_c], sr[act_c], sr["_err"],
                                                  has_code)))

                story += htbl(['Sub Brand / Size  ·  UPC  ·  SKU', 'IBP',
                               'Actual Sales', 'Forecast Error', 'Error ABS',
                               '% VOL', 'Mape. UPC', 'BIAS'],
                              brk_rows,
                              [5.6*cm, 1.75*cm, 1.8*cm, 1.8*cm,
                               1.95*cm, 1.5*cm, 1.7*cm, 1.5*cm])
                if uncounted > 0:
                    story.append(Paragraph(
                        f'Materials with no UPC in the Aera master contribute nothing '
                        f'to the reported measure. Across these ten lines their own '
                        f'absolute error is <b>{uncounted:,.0f}</b> 9LC, against a '
                        f'reported Error ABS UPC of {tot_err:,.0f}. The two do not '
                        f'simply add: once a code is given a UPC it nets against the '
                        f'other codes sharing it, which in several cases here would '
                        f'reduce the error rather than increase it. The figure sizes '
                        f'the master-data gap; it does not restate the KPI.',
                        ST['source']))
    else:
        story.append(Paragraph(
            'No n-3 accuracy data available for this market.', ST['source']))

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
            story.append(Paragraph(f'<b>{escape(str(brand))}</b>', ST['sub']))
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
            story.append(Paragraph(f'<b>{escape(str(brand))}</b>', ST['sub']))
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
