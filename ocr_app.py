#!/usr/bin/env python3
"""
OCR Scanner — Web app for PDF extraction with native text + Tesseract OCR fallback.
Handles native PDFs (text layer) and scanned PDFs (image-only pages) automatically.

Run: streamlit run ocr_app.py --server.port 8503
"""

import streamlit as st
import threading
import time
import json
import multiprocessing
from pathlib import Path
from typing import List, Dict, Any

import fitz
import pandas as pd

from ocr_pipeline import run_scan_threaded_ocr


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="OCR Scanner",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Custom CSS (amber/orange accent — distinct from scan_app.py blue) ─────────

st.markdown("""
<style>
/* Main background */
.stApp { background: #0F1B2D; }

/* Hide default Streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }

/* Upload zone */
[data-testid="stFileUploaderDropzone"] {
    background: #1A2E48 !important;
    border: 2px dashed #5A3A1A !important;
    border-radius: 8px !important;
}

/* Buttons */
.stButton > button {
    background: #D4780A !important;
    color: white !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    padding: 0.6rem 2rem !important;
    font-size: 1rem !important;
    width: 100% !important;
    transition: background 0.2s !important;
}

.stButton > button:hover {
    background: #C06A08 !important;
}

/* Progress bar */
.stProgress > div > div { background: #D4780A !important; }

/* Metric boxes */
[data-testid="stMetric"] {
    background: #1A2E48;
    border: 1px solid #2E4A6A;
    border-radius: 6px;
    padding: 16px;
}

[data-testid="stMetricValue"] { color: #F0A030 !important; }
[data-testid="stMetricLabel"] { color: #7A9AB8 !important; }

/* Tabs */
.stTabs [data-baseweb="tab"] { color: #7A9AB8 !important; }
.stTabs [aria-selected="true"] {
    color: #F0A030 !important;
    border-bottom: 2px solid #D4780A !important;
}

/* Text colors */
h1, h2, h3 { color: #E8F0FA !important; }
p, .stMarkdown { color: #B0C4D8 !important; }

/* Dataframe */
[data-testid="stDataFrame"] { border: 1px solid #2E4A6A; border-radius: 6px; }

/* Sidebar */
[data-testid="stSidebar"] { background: #111E30 !important; }

/* Code blocks */
.stCodeBlock { background: #111E30 !important; }

/* Alert / info boxes */
.stAlert { background: #1A2E48 !important; border-left: 4px solid #D4780A !important; }
</style>
""", unsafe_allow_html=True)


# ── Helper functions ──────────────────────────────────────────────────────────

def _safe_columns(headers: List[str]) -> List[str]:
    """Deduplicate and fill empty column names so pandas doesn't reject them."""
    seen: dict = {}
    out = []
    for i, h in enumerate(headers):
        h = h.strip() if h and h.strip() else f"Col_{i+1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 0
        out.append(h)
    return out


def build_markdown(pages: List[Dict], filename: str) -> str:
    lines = [f"# {filename}", ""]
    for pg in pages:
        path_label = pg.get("path", "native").upper()
        lines.append(f"\n<!-- Page {pg['page']} [{path_label}] -->")
        if "error" in pg:
            lines.append(f"> Error: {pg['error']}")
            continue
        for blk in pg.get("content", []):
            btype = blk["type"]
            if btype == "heading":
                lines.append(f"\n## {blk['text']}")
            elif btype == "paragraph":
                lines.append(f"\n{blk['text']}")
            elif btype == "table":
                rows = blk.get("rows", [])
                if rows:
                    h = rows[0]
                    lines += [
                        "",
                        "| " + " | ".join(h) + " |",
                        "| " + " | ".join(["---"] * len(h)) + " |",
                    ]
                    for row in rows[1:]:
                        pad = row + [""] * max(0, len(h) - len(row))
                        lines.append("| " + " | ".join(pad[: len(h)]) + " |")
                    lines.append("")
    return "\n".join(lines)


def build_text(pages: List[Dict]) -> str:
    SEP = "=" * 60
    lines = []
    for pg in pages:
        path_label = pg.get("path", "native").upper()
        lines += ["", SEP, f"PAGE {pg['page']} [{path_label}]", SEP]
        if "error" in pg:
            lines.append(f"[ERROR: {pg['error']}]")
            continue
        for blk in pg.get("content", []):
            btype = blk["type"]
            if btype in ("heading", "paragraph"):
                lines.append(f"\n{blk['text']}")
            elif btype == "table":
                for row in blk.get("rows", []):
                    lines.append("  | " + " | ".join(row) + " |")
    return "\n".join(lines)


def get_all_tables(pages: List[Dict]) -> List[Dict]:
    tables = []
    for pg in pages:
        for blk in pg.get("content", []):
            if blk["type"] == "table" and blk.get("rows"):
                tables.append({"page": pg["page"], **blk})
    return tables


# ── Session state init ────────────────────────────────────────────────────────

def init_state() -> None:
    defaults = {
        "scan_state":   {},
        "scan_thread":  None,
        "scan_started": False,
        "pdf_bytes":    None,
        "pdf_name":     None,
        "total_pages":  0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div style="color:#F0A030; font-size:1rem; font-weight:700; '
        'letter-spacing:0.04em; padding-bottom:4px;">OCR Settings</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<hr style="border:none; border-top:1px solid #2E3A50; margin:4px 0 12px 0;">',
        unsafe_allow_html=True,
    )

    max_cpu = multiprocessing.cpu_count()
    workers = st.slider(
        "Parallel workers",
        1, max(max_cpu, 8), min(max_cpu, 4),
        key="workers_slider",
    )

    dpi = st.select_slider(
        "OCR DPI (scanned pages)",
        options=[150, 200, 300],
        value=300,
        help="Higher DPI = better OCR quality but slower. 300 recommended for most documents.",
    )

    denoise = st.checkbox(
        "Denoise (slower, better quality)",
        value=True,
        help="Apply NL-means denoising before OCR. Improves accuracy on noisy or low-quality scans.",
    )

    st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
    st.markdown(
        '<hr style="border:none; border-top:1px solid #2E3A50; margin:4px 0 12px 0;">',
        unsafe_allow_html=True,
    )

    show_headers = st.toggle("Include headers/footers in Preview", value=False)

    output_format = st.multiselect(
        "Download formats",
        ["JSON", "Markdown", "Plain Text"],
        default=["JSON", "Markdown"],
    )

    st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
    st.markdown("""
    <div style="background:#0F1B2D; border:1px solid #2E3A50; border-radius:6px;
                padding:12px; font-size:0.8rem; line-height:1.7; color:#5A7A9A;">
    <strong style="color:#7A9AB8;">Page routing</strong><br>
    <span style="color:#0A7A5E;">&#9679;</span>
    <strong style="color:#6ABAA0;">NATIVE</strong>
     — PyMuPDF text + table extraction<br>
    <span style="color:#D4780A;">&#9679;</span>
    <strong style="color:#D4780A;">OCR</strong>
     — Render → Otsu → Tesseract<br><br>
    Pages with ≥ 50 alphanumeric chars use the native path.
    </div>
    """, unsafe_allow_html=True)


# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("""
<div style="padding: 24px 0 8px 0;">
  <h1 style="margin:0; font-size:2rem; color:#E8F0FA; letter-spacing:-0.5px;">
    🔎 OCR Scanner
  </h1>
  <p style="margin:4px 0 0 0; color:#5A7A9A; font-size:0.95rem;">
    Native text + Tesseract OCR for scanned pages
  </p>
</div>
<hr style="border:none; border-top:1px solid #1E3352; margin: 12px 0 24px 0;">
""", unsafe_allow_html=True)


# ── Upload row ────────────────────────────────────────────────────────────────

col_upload, col_info = st.columns([2, 1], gap="large")

with col_upload:
    st.markdown("**Upload PDF**")
    uploaded = st.file_uploader(
        label="upload",
        type=["pdf"],
        label_visibility="collapsed",
        help="Drag and drop or click to browse",
    )

with col_info:
    st.markdown("**How it works**")
    st.markdown("""
    <div style="background:#1A2E48; border:1px solid #2E4A6A; border-radius:6px;
                padding:14px; font-size:0.85rem; color:#7A9AB8; line-height:1.7;">
      Each page is auto-classified:<br>
      <span style="color:#0A7A5E;">&#9679;</span>
      <strong>Native</strong>: PyMuPDF text &amp; table extraction<br>
      <span style="color:#D4780A;">&#9679;</span>
      <strong>OCR</strong>: Render image → preprocess → Tesseract
    </div>
    """, unsafe_allow_html=True)


# ── File info + Start button ──────────────────────────────────────────────────

if uploaded is not None:
    pdf_bytes = uploaded.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count
    doc.close()

    st.session_state.pdf_bytes   = pdf_bytes
    st.session_state.pdf_name    = uploaded.name
    st.session_state.total_pages = total_pages

    size_mb = len(pdf_bytes) / 1_048_576

    st.markdown(f"""
    <div style="background:#1A2E48; border:1px solid #2E4A6A; border-radius:6px;
                padding:12px 18px; margin:12px 0; display:flex; gap:32px; align-items:center;
                flex-wrap:wrap;">
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase;
                    letter-spacing:0.08em;">File</div>
        <div style="color:#E8F0FA; font-weight:600;">{uploaded.name}</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase;
                    letter-spacing:0.08em;">Pages</div>
        <div style="color:#F0A030; font-weight:700; font-size:1.2rem;">{total_pages:,}</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase;
                    letter-spacing:0.08em;">Size</div>
        <div style="color:#E8F0FA; font-weight:600;">{size_mb:.1f} MB</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase;
                    letter-spacing:0.08em;">Config</div>
        <div style="color:#E8F0FA; font-weight:600;">{workers} workers · {dpi} DPI</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    scan_running = (
        st.session_state.scan_thread is not None
        and st.session_state.scan_thread.is_alive()
    )

    if not scan_running:
        if st.button("▶  Start OCR Scan", use_container_width=True):
            state: Dict[str, Any] = {
                "running":            True,
                "done":               False,
                "progress":           0,
                "elapsed":            0,
                "pages_per_sec":      0,
                "total_tables":       0,
                "total_words":        0,
                "native_pages":       0,
                "ocr_pages":          0,
                "avg_ocr_confidence": None,
                "results":            None,
                "error":              None,
            }
            st.session_state.scan_state   = state
            st.session_state.scan_started = True

            t = threading.Thread(
                target=run_scan_threaded_ocr,
                args=(pdf_bytes, total_pages, workers, dpi, denoise, state),
                daemon=True,
            )
            t.start()
            st.session_state.scan_thread = t
            st.rerun()
    else:
        st.button("⏳  Scanning…", disabled=True, use_container_width=True)

else:
    st.markdown("""
    <div style="background:#111E30; border:1px dashed #3A2A1A; border-radius:8px;
                padding:40px; text-align:center; color:#5A4A3A; margin-top:12px;">
      <div style="font-size:2.5rem; margin-bottom:8px;">📄</div>
      <div style="font-size:1rem;">Upload a PDF above to begin</div>
    </div>
    """, unsafe_allow_html=True)


# ── Live progress ──────────────────────────────────────────────────────────────

state = st.session_state.scan_state

if state and state.get("running"):
    total = st.session_state.total_pages
    done  = state.get("progress", 0)
    pct   = done / total if total else 0
    pps   = state.get("pages_per_sec", 0)
    eta   = round((total - done) / pps) if pps > 0 else "…"

    st.markdown("---")
    st.markdown("**Scanning…**")
    st.progress(pct)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pages Done",  f"{done:,} / {total:,}")
    m2.metric("Speed",       f"{pps} pg/s")
    m3.metric("Elapsed",     f"{state.get('elapsed', 0):.1f}s")
    m4.metric("ETA",         f"{eta}s" if isinstance(eta, int) else eta)

    time.sleep(0.25)
    st.rerun()


# ── Results ────────────────────────────────────────────────────────────────────

if state and state.get("done") and state.get("results"):
    results  = state["results"]
    filename = st.session_state.pdf_name or "document.pdf"
    total_p  = st.session_state.total_pages
    native_p = state.get("native_pages", 0)
    ocr_p    = state.get("ocr_pages",    0)
    avg_conf = state.get("avg_ocr_confidence")

    # ── Summary metrics ──
    st.markdown("---")
    st.markdown("### Scan Complete")

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Pages",   f"{total_p:,}")
    m2.metric("Native",  f"{native_p:,}")
    m3.metric("OCR",     f"{ocr_p:,}")
    m4.metric("Tables",  f"{state['total_tables']:,}")
    m5.metric("Words",   f"{state['total_words']:,}")
    m6.metric("Time",    f"{state['elapsed']}s")

    # OCR confidence bar (only shown when OCR pages exist)
    if avg_conf is not None and ocr_p > 0:
        conf_pct   = avg_conf * 100
        conf_color = (
            "#0A7A5E" if conf_pct >= 80
            else "#D4780A" if conf_pct >= 60
            else "#C04040"
        )
        st.markdown(f"""
        <div style="background:#1A2E48; border:1px solid #2E4A6A; border-radius:6px;
                    padding:10px 18px; margin:10px 0; display:flex; align-items:center; gap:16px;">
          <div style="color:#7A9AB8; font-size:0.85rem; white-space:nowrap;">
            Avg OCR Confidence
          </div>
          <div style="color:{conf_color}; font-weight:700; font-size:1.1rem;
                      white-space:nowrap;">{conf_pct:.1f}%</div>
          <div style="flex:1; background:#111E30; border-radius:4px; height:8px; overflow:hidden;">
            <div style="width:{min(conf_pct, 100):.1f}%; height:100%;
                        background:{conf_color}; border-radius:4px;"></div>
          </div>
        </div>
        """, unsafe_allow_html=True)

    # ── Output tabs ──
    tab_prev, tab_tables, tab_json, tab_dl = st.tabs([
        "📖 Preview", "📊 Tables", "{ } JSON", "⬇ Download"
    ])

    # ── Preview tab ──
    with tab_prev:
        st.caption(
            "Extracted content page by page. "
            "Green badge = native text path · Amber badge = Tesseract OCR path."
        )
        page_num = st.slider("Page", 1, total_p, 1, key="preview_page")
        pg = results[page_num - 1]

        if "error" in pg:
            st.error(f"Page {page_num} error: {pg['error']}")
        else:
            # Path badge + confidence meter
            path = pg.get("path", "native")
            if path == "ocr":
                conf = pg.get("ocr_confidence") or 0.0
                st.markdown(
                    f'<span style="background:#D4780A22;color:#D4780A;padding:2px 8px;'
                    f'border-radius:3px;font-size:0.75rem;font-weight:700;">'
                    f'OCR · {conf:.0%} confidence</span>',
                    unsafe_allow_html=True,
                )
                st.progress(float(conf))
            else:
                st.markdown(
                    '<span style="background:#0A7A5E22;color:#0A7A5E;padding:2px 8px;'
                    'border-radius:3px;font-size:0.75rem;font-weight:700;">'
                    'NATIVE TEXT</span>',
                    unsafe_allow_html=True,
                )

            st.markdown("")

            for blk in pg.get("content", []):
                btype = blk["type"]
                if btype == "heading":
                    st.markdown(f"### {blk['text']}")
                elif btype == "paragraph":
                    st.markdown(blk["text"])
                elif btype == "table":
                    rows = blk.get("rows", [])
                    if rows and len(rows) > 1:
                        df = pd.DataFrame(rows[1:], columns=_safe_columns(rows[0]))
                        st.dataframe(df, use_container_width=True)
                    elif rows:
                        st.table(rows)
                elif show_headers and btype in ("header", "footer"):
                    st.caption(f"[{btype}] {blk['text']}")

    # ── Tables tab ──
    with tab_tables:
        all_tables = get_all_tables(results)
        if not all_tables:
            st.info("No tables detected in this document.")
        else:
            st.caption(f"{len(all_tables)} tables found across {total_p} pages")
            for i, tbl in enumerate(all_tables):
                rows = tbl.get("rows", [])
                with st.expander(
                    f"Table {i+1} — Page {tbl['page']} "
                    f"({tbl.get('row_count', len(rows))} rows × "
                    f"{tbl.get('col_count', len(rows[0]) if rows else 0)} cols)",
                    expanded=(i < 3),
                ):
                    if len(rows) > 1:
                        df = pd.DataFrame(rows[1:], columns=_safe_columns(rows[0]))
                        st.dataframe(df, use_container_width=True)
                    else:
                        st.table(rows)

    # ── JSON tab ──
    with tab_json:
        st.caption("Full structured output — one object per page")
        page_j = st.slider("Page", 1, total_p, 1, key="json_page")
        st.json(results[page_j - 1], expanded=2)

    # ── Download tab ──
    with tab_dl:
        st.markdown("**Download extracted data**")

        doc_meta = {
            "filename":           filename,
            "total_pages":        total_p,
            "native_pages":       native_p,
            "ocr_pages":          ocr_p,
            "avg_ocr_confidence": avg_conf,
            "tables_found":       state["total_tables"],
            "word_count":         state["total_words"],
            "processing_time_s":  state["elapsed"],
            "pages_per_second":   state.get("pages_per_sec", 0),
        }

        dl1, dl2, dl3 = st.columns(3)

        if "JSON" in output_format:
            json_bytes = json.dumps(
                {"document": doc_meta, "pages": results},
                ensure_ascii=False, indent=2
            ).encode("utf-8")
            dl1.download_button(
                label="⬇ Download JSON",
                data=json_bytes,
                file_name=f"{Path(filename).stem}_ocr.json",
                mime="application/json",
                use_container_width=True,
            )

        if "Markdown" in output_format:
            md_text = build_markdown(results, filename)
            dl2.download_button(
                label="⬇ Download Markdown",
                data=md_text.encode("utf-8"),
                file_name=f"{Path(filename).stem}_ocr.md",
                mime="text/markdown",
                use_container_width=True,
            )

        if "Plain Text" in output_format:
            txt = build_text(results)
            dl3.download_button(
                label="⬇ Download Text",
                data=txt.encode("utf-8"),
                file_name=f"{Path(filename).stem}_ocr.txt",
                mime="text/plain",
                use_container_width=True,
            )

        st.markdown("---")
        st.caption("Summary")
        st.json(doc_meta)


elif state and state.get("error"):
    st.error(f"Scan failed: {state['error']}")
