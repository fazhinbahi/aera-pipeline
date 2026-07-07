#!/usr/bin/env python3
"""
PDF Scanner — Web app for fast parallel PDF text + table extraction.
Run: streamlit run scan_app.py
"""

import streamlit as st
import threading
import tempfile
import time
import json
import statistics
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

import fitz
import pdfplumber

# ── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="PDF Scanner",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* Main background */
.stApp { background: #0F1B2D; }

/* Hide default Streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }

/* Upload zone */
[data-testid="stFileUploaderDropzone"] {
    background: #1A2E48 !important;
    border: 2px dashed #2E5A8A !important;
    border-radius: 8px !important;
}

/* Buttons */
.stButton > button {
    background: #1A6FE0 !important;
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
    background: #2565A8 !important;
}

/* Progress bar */
.stProgress > div > div { background: #1A6FE0 !important; }

/* Metric boxes */
[data-testid="stMetric"] {
    background: #1A2E48;
    border: 1px solid #2E4A6A;
    border-radius: 6px;
    padding: 16px;
}

[data-testid="stMetricValue"] { color: #4DA3FF !important; }
[data-testid="stMetricLabel"] { color: #7A9AB8 !important; }

/* Tabs */
.stTabs [data-baseweb="tab"] { color: #7A9AB8 !important; }
.stTabs [aria-selected="true"] { color: #4DA3FF !important; border-bottom: 2px solid #1A6FE0 !important; }

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
.stAlert { background: #1A2E48 !important; border-left: 4px solid #1A6FE0 !important; }
</style>
""", unsafe_allow_html=True)


# ── Pipeline core (thread-safe, no multiprocessing) ─────────────────────────

HEADING_SCALE    = 1.15
FOOTER_Y_PCT     = 0.91
HEADER_Y_PCT     = 0.07
MIN_TABLE_ROWS   = 2
MIN_TABLE_COLS   = 2


def _process_page(pdf_bytes: bytes, page_num: int) -> Dict[str, Any]:
    """Process one page from bytes (thread-safe)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    page_h = page.rect.height
    page_w = page.rect.width

    tables_out = []
    table_rects = []

    # Table extraction via PyMuPDF 1.23+
    try:
        finder = page.find_tables()
        for tbl in finder:
            if len(tbl.rows) < MIN_TABLE_ROWS:
                continue
            raw = tbl.extract()
            if not raw or len(raw[0]) < MIN_TABLE_COLS:
                continue
            cleaned = [[str(c) if c is not None else "" for c in row] for row in raw]
            tables_out.append({
                "type":      "table",
                "rows":      cleaned,
                "bbox":      list(tbl.bbox),
                "row_count": len(cleaned),
                "col_count": len(cleaned[0]) if cleaned else 0,
            })
            table_rects.append(fitz.Rect(tbl.bbox))
    except (AttributeError, Exception):
        pass

    content = list(tables_out)

    # Text extraction
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    all_sizes = []
    for blk in text_dict.get("blocks", []):
        if blk.get("type") == 0:
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    sz = sp.get("size", 0)
                    if sz > 0:
                        all_sizes.append(sz)

    median_font = statistics.median(all_sizes) if all_sizes else 10.0

    for blk in text_dict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        bbox = blk.get("bbox", [0, 0, 0, 0])
        blk_rect = fitz.Rect(bbox)
        if any(blk_rect.intersects(tr) for tr in table_rects):
            continue

        parts, blk_sizes, is_bold = [], [], False
        for ln in blk.get("lines", []):
            ln_text = ""
            for sp in ln.get("spans", []):
                txt = sp.get("text", "").strip()
                if txt:
                    ln_text += txt + " "
                    sz = sp.get("size", 10.0)
                    if sz > 0:
                        blk_sizes.append(sz)
                    if sp.get("flags", 0) & (1 << 4):
                        is_bold = True
            if ln_text.strip():
                parts.append(ln_text.strip())

        full_text = " ".join(parts).strip()
        if not full_text:
            continue

        blk_font = statistics.median(blk_sizes) if blk_sizes else median_font
        y_top = bbox[1] / page_h
        y_bot = bbox[3] / page_h

        if y_bot < HEADER_Y_PCT:
            btype = "header"
        elif y_top > FOOTER_Y_PCT:
            btype = "footer"
        elif blk_font >= median_font * HEADING_SCALE or (is_bold and blk_font > median_font):
            btype = "heading"
        else:
            btype = "paragraph"

        content.append({
            "type":      btype,
            "text":      full_text,
            "bbox":      bbox,
            "font_size": round(blk_font, 1),
        })

    content.sort(key=lambda b: b.get("bbox", [0, 0, 0, 0])[1])
    doc.close()

    return {
        "page":        page_num + 1,
        "width":       round(page_w, 1),
        "height":      round(page_h, 1),
        "content":     content,
        "table_count": sum(1 for b in content if b["type"] == "table"),
        "word_count":  sum(
            len(b.get("text", "").split())
            for b in content if b["type"] in ("paragraph", "heading")
        ),
    }


def run_scan_threaded(pdf_bytes: bytes, total_pages: int, workers: int, state: dict):
    """Runs the full scan in a background thread, updating `state` with progress."""
    t_start = time.perf_counter()
    results = [None] * total_pages
    state["running"]  = True
    state["done"]     = False
    state["progress"] = 0
    state["error"]    = None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_process_page, pdf_bytes, i): i
                for i in range(total_pages)
            }
            completed = 0
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    results[page_num] = future.result()
                except Exception as e:
                    results[page_num] = {
                        "page":        page_num + 1,
                        "error":       str(e),
                        "content":     [],
                        "table_count": 0,
                        "word_count":  0,
                    }
                completed += 1
                elapsed = time.perf_counter() - t_start
                state["progress"]     = completed
                state["elapsed"]      = round(elapsed, 2)
                state["pages_per_sec"]= round(completed / elapsed, 1) if elapsed > 0 else 0

        elapsed = time.perf_counter() - t_start
        state["results"]     = results
        state["total_tables"]= sum(p.get("table_count", 0) for p in results)
        state["total_words"] = sum(p.get("word_count",  0) for p in results)
        state["elapsed"]     = round(elapsed, 2)
        state["pages_per_sec"]= round(total_pages / elapsed, 1)
        state["done"]        = True

    except Exception as e:
        state["error"] = str(e)
        state["done"]  = True

    state["running"] = False


# ── Output builders ──────────────────────────────────────────────────────────

def build_markdown(pages: List[Dict], filename: str) -> str:
    lines = [f"# {filename}", ""]
    for pg in pages:
        lines.append(f"\n<!-- Page {pg['page']} -->")
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
        lines += ["", SEP, f"PAGE {pg['page']}", SEP]
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


def get_all_tables(pages: List[Dict]) -> List[Dict]:
    tables = []
    for pg in pages:
        for blk in pg.get("content", []):
            if blk["type"] == "table" and blk.get("rows"):
                tables.append({"page": pg["page"], **blk})
    return tables


# ── Session state init ───────────────────────────────────────────────────────

def init_state():
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


# ── Header ───────────────────────────────────────────────────────────────────

st.markdown("""
<div style="padding: 24px 0 8px 0;">
  <h1 style="margin:0; font-size:2rem; color:#E8F0FA; letter-spacing:-0.5px;">
    🔍 PDF Scanner
  </h1>
  <p style="margin:4px 0 0 0; color:#5A7A9A; font-size:0.95rem;">
    Fast parallel extraction · text, headings &amp; tables · 2000+ pages
  </p>
</div>
<hr style="border:none; border-top:1px solid #1E3352; margin: 12px 0 24px 0;">
""", unsafe_allow_html=True)


# ── Upload + config row ──────────────────────────────────────────────────────

col_upload, col_cfg = st.columns([2, 1], gap="large")

with col_upload:
    st.markdown("**Upload PDF**")
    uploaded = st.file_uploader(
        label="upload",
        type=["pdf"],
        label_visibility="collapsed",
        help="Drag and drop or click to browse",
    )

with col_cfg:
    st.markdown("**Scan Settings**")
    import multiprocessing
    max_cpu = multiprocessing.cpu_count()
    workers = st.slider("Parallel workers", 1, max(max_cpu, 8), min(max_cpu, 6))
    show_headers  = st.toggle("Include headers/footers in output", value=False)
    output_format = st.multiselect(
        "Download formats",
        ["JSON", "Markdown", "Plain Text"],
        default=["JSON", "Markdown"],
    )


# ── File info + Start button ─────────────────────────────────────────────────

if uploaded is not None:
    pdf_bytes = uploaded.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count
    doc.close()

    st.session_state.pdf_bytes   = pdf_bytes
    st.session_state.pdf_name    = uploaded.name
    st.session_state.total_pages = total_pages

    size_mb = len(pdf_bytes) / 1_048_576

    # File info strip
    st.markdown(f"""
    <div style="background:#1A2E48; border:1px solid #2E4A6A; border-radius:6px;
                padding:12px 18px; margin:12px 0; display:flex; gap:32px; align-items:center;">
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.08em;">File</div>
        <div style="color:#E8F0FA; font-weight:600;">{uploaded.name}</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.08em;">Pages</div>
        <div style="color:#4DA3FF; font-weight:700; font-size:1.2rem;">{total_pages:,}</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.08em;">Size</div>
        <div style="color:#E8F0FA; font-weight:600;">{size_mb:.1f} MB</div>
      </div>
      <div>
        <div style="color:#7A9AB8; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.08em;">Est. Time</div>
        <div style="color:#E8F0FA; font-weight:600;">~{max(1, total_pages // (workers * 10))}s</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Start Scan button
    scan_running = (
        st.session_state.scan_thread is not None
        and st.session_state.scan_thread.is_alive()
    )

    if not scan_running:
        if st.button("▶  Start Scan", use_container_width=True):
            state = {
                "running":      True,
                "done":         False,
                "progress":     0,
                "elapsed":      0,
                "pages_per_sec":0,
                "total_tables": 0,
                "total_words":  0,
                "results":      None,
                "error":        None,
            }
            st.session_state.scan_state   = state
            st.session_state.scan_started = True

            t = threading.Thread(
                target=run_scan_threaded,
                args=(pdf_bytes, total_pages, workers, state),
                daemon=True,
            )
            t.start()
            st.session_state.scan_thread = t
            st.rerun()
    else:
        st.button("⏳  Scanning…", disabled=True, use_container_width=True)

else:
    # No file yet — show placeholder
    st.markdown("""
    <div style="background:#111E30; border:1px dashed #2E4A6A; border-radius:8px;
                padding:40px; text-align:center; color:#3A5A7A; margin-top:12px;">
      <div style="font-size:2.5rem; margin-bottom:8px;">📄</div>
      <div style="font-size:1rem;">Upload a PDF above to begin</div>
    </div>
    """, unsafe_allow_html=True)


# ── Live progress ────────────────────────────────────────────────────────────

state = st.session_state.scan_state

if state and state.get("running"):
    total  = st.session_state.total_pages
    done   = state.get("progress", 0)
    pct    = done / total if total else 0
    eta    = round((total - done) / state["pages_per_sec"]) if state.get("pages_per_sec", 0) > 0 else "…"

    st.markdown("---")
    st.markdown("**Scanning…**")
    st.progress(pct)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pages Done",   f"{done:,} / {total:,}")
    m2.metric("Speed",        f"{state.get('pages_per_sec', 0)} pg/s")
    m3.metric("Elapsed",      f"{state.get('elapsed', 0):.1f}s")
    m4.metric("ETA",          f"{eta}s" if isinstance(eta, int) else eta)

    time.sleep(0.25)
    st.rerun()


# ── Results ──────────────────────────────────────────────────────────────────

if state and state.get("done") and state.get("results"):
    results  = state["results"]
    filename = st.session_state.pdf_name or "document.pdf"
    total_p  = st.session_state.total_pages

    # ── Summary metrics ──
    st.markdown("---")
    st.markdown("### ✅ Scan Complete")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Pages",       f"{total_p:,}")
    m2.metric("Tables",      f"{state['total_tables']:,}")
    m3.metric("Words",       f"{state['total_words']:,}")
    m4.metric("Time",        f"{state['elapsed']}s")
    m5.metric("Speed",       f"{state['pages_per_sec']} pg/s")

    # ── Output tabs ──
    tab_prev, tab_tables, tab_json, tab_dl = st.tabs([
        "📖 Preview", "📊 Tables", "{ } JSON", "⬇ Download"
    ])

    # ── Preview tab ──
    with tab_prev:
        st.caption("Showing extracted content page by page. Use the slider to navigate.")
        page_num = st.slider("Page", 1, total_p, 1, key="preview_page")
        pg = results[page_num - 1]

        if "error" in pg:
            st.error(f"Page {page_num} error: {pg['error']}")
        else:
            for blk in pg.get("content", []):
                btype = blk["type"]
                if btype == "heading":
                    st.markdown(f"### {blk['text']}")
                elif btype == "paragraph":
                    st.markdown(blk["text"])
                elif btype == "table":
                    rows = blk.get("rows", [])
                    if rows and len(rows) > 1:
                        import pandas as pd
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
                    f"({tbl['row_count']} rows × {tbl['col_count']} cols)",
                    expanded=(i < 3),
                ):
                    if len(rows) > 1:
                        import pandas as pd
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
            "filename":         filename,
            "total_pages":      total_p,
            "tables_found":     state["total_tables"],
            "word_count":       state["total_words"],
            "processing_time_s":state["elapsed"],
            "pages_per_second": state["pages_per_sec"],
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
                file_name=f"{Path(filename).stem}_extracted.json",
                mime="application/json",
                use_container_width=True,
            )

        if "Markdown" in output_format:
            md_text = build_markdown(results, filename)
            dl2.download_button(
                label="⬇ Download Markdown",
                data=md_text.encode("utf-8"),
                file_name=f"{Path(filename).stem}_extracted.md",
                mime="text/markdown",
                use_container_width=True,
            )

        if "Plain Text" in output_format:
            txt = build_text(results)
            dl3.download_button(
                label="⬇ Download Text",
                data=txt.encode("utf-8"),
                file_name=f"{Path(filename).stem}_extracted.txt",
                mime="text/plain",
                use_container_width=True,
            )

        st.markdown("---")
        st.caption("Summary")
        st.json(doc_meta)


elif state and state.get("error"):
    st.error(f"Scan failed: {state['error']}")
