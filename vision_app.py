#!/usr/bin/env python3
"""
Vision Scanner — Streamlit app for the 3-path Vision Pipeline.
  NATIVE  (green)  → PyMuPDF direct extraction
  OCR     (amber)  → Tesseract for scanned pages
  VISION  (purple) → Claude Vision for complex/low-confidence pages

Run: streamlit run vision_app.py --server.port 8504
"""

import streamlit as st
import threading
import json
import time
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

import fitz
from vision_pipeline import run_scan_threaded_vision, _write_json, _write_markdown, _write_text

# Auto-load API key: checks st.secrets (Streamlit Cloud), then .env, then env var
def _load_env_key() -> str:
    # Streamlit Community Cloud secrets
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    # Local .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("ANTHROPIC_API_KEY", "")

_ENV_API_KEY = _load_env_key()

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Vision Scanner",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
.stApp { background: #0F1B2D; }
#MainMenu, footer, header { visibility: hidden; }

[data-testid="stFileUploaderDropzone"] {
    background: #1A2240 !important;
    border: 2px dashed #4A3A8A !important;
    border-radius: 8px !important;
}

/* Primary action button — purple */
.stButton > button {
    background: #6B3FD4 !important;
    color: white !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    padding: 0.6rem 2rem !important;
    font-size: 1rem !important;
    width: 100% !important;
}
.stButton > button:hover { background: #5530B0 !important; }

.stProgress > div > div { background: #6B3FD4 !important; }

[data-testid="stMetric"] {
    background: #1A2240;
    border: 1px solid #2E3A6A;
    border-radius: 6px;
    padding: 16px;
}
[data-testid="stMetricValue"] { color: #A78BFA !important; }
[data-testid="stMetricLabel"] { color: #6A7AA8 !important; }

.stTabs [data-baseweb="tab"] { color: #6A7AA8 !important; }
.stTabs [aria-selected="true"] { color: #A78BFA !important; border-bottom: 2px solid #6B3FD4 !important; }

h1, h2, h3 { color: #E8EAFA !important; }
p, .stMarkdown { color: #A0B4D0 !important; }

[data-testid="stSidebar"] { background: #111830 !important; }
[data-testid="stDataFrame"] { border: 1px solid #2E3A6A; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

# ── Badge helper ──────────────────────────────────────────────────────────────

PATH_STYLES = {
    "native": ("NATIVE",  "#0A7A5E", "#0A7A5E22"),
    "ocr":    ("OCR",     "#D4780A", "#D4780A22"),
    "vision": ("VISION",  "#6B3FD4", "#6B3FD422"),
    "error":  ("ERROR",   "#C03030", "#C0303022"),
}

def path_badge(path: str, extra: str = "") -> str:
    label, color, bg = PATH_STYLES.get(path, ("?", "#888", "#88888822"))
    text = f"{label}{('  ·  ' + extra) if extra else ''}"
    return (
        f'<span style="background:{bg};color:{color};padding:2px 10px;'
        f'border-radius:3px;font-size:0.72rem;font-weight:700;'
        f'letter-spacing:0.06em;">{text}</span>'
    )

def _safe_columns(headers: List[str]) -> List[str]:
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

# ── Session state ─────────────────────────────────────────────────────────────

for k, v in {
    "vs_state":   {},
    "vs_thread":  None,
    "vs_started": False,
    "vs_bytes":   None,
    "vs_name":    None,
    "vs_pages":   0,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### ⚙️ Vision Settings")

    api_key = st.text_input(
        "Anthropic API Key",
        value=_ENV_API_KEY,
        type="password",
        placeholder="sk-ant-...",
        help="Auto-loaded from .env. Override here if needed.",
    )

    st.markdown("---")
    st.markdown("**Scan options**")

    import multiprocessing
    max_cpu = multiprocessing.cpu_count()
    workers = st.slider("Workers", 1, max(max_cpu, 8), min(max_cpu, 4),
                        help="Keep ≤ 4 when Vision is active to avoid API rate limits")

    dpi = st.select_slider("OCR DPI", options=[150, 200, 300], value=300)
    denoise = st.toggle("Denoise scanned pages", value=True)
    vision_threshold = st.slider(
        "Vision threshold",
        min_value=0.50, max_value=0.95, value=0.70, step=0.05,
        format="%.0f%%",
        help="Tesseract confidence below this triggers Claude Vision",
    )
    show_headers = st.toggle("Show headers / footers", value=False)
    output_format = st.multiselect(
        "Download formats",
        ["JSON", "Markdown", "Plain Text"],
        default=["JSON", "Markdown"],
    )

    st.markdown("---")
    st.markdown("**Path legend**")
    for p, (lbl, color, bg) in PATH_STYLES.items():
        if p == "error":
            continue
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0;">'
            f'<span style="background:{bg};color:{color};padding:1px 8px;border-radius:3px;'
            f'font-size:0.70rem;font-weight:700;">{lbl}</span>'
            f'<span style="color:#6A7AA8;font-size:0.78rem;">'
            + {"native": "PyMuPDF — text layer", "ocr": "Tesseract — scanned page", "vision": "Claude Vision — complex/low-conf"}[p]
            + "</span></div>",
            unsafe_allow_html=True,
        )

    if api_key:
        source = "from .env" if api_key == _ENV_API_KEY and _ENV_API_KEY else "manually entered"
        st.success(f"Vision active ({source})", icon="🧠")
    else:
        st.warning("No API key — OCR only (Path B)", icon="⚠️")

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("""
<div style="padding:24px 0 8px 0;">
  <h1 style="margin:0;font-size:2rem;color:#E8EAFA;letter-spacing:-0.5px;">
    🧠 Vision Scanner
  </h1>
  <p style="margin:4px 0 0 0;color:#4A5A8A;font-size:0.95rem;">
    3-path extraction &nbsp;·&nbsp; Native text · Tesseract OCR · Claude Vision
  </p>
</div>
<hr style="border:none;border-top:1px solid #1E2A52;margin:12px 0 24px 0;">
""", unsafe_allow_html=True)

# ── Upload ────────────────────────────────────────────────────────────────────

uploaded = st.file_uploader(
    label="Upload PDF",
    type=["pdf"],
    label_visibility="collapsed",
)

if uploaded is not None:
    pdf_bytes   = uploaded.read()
    doc         = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count
    doc.close()

    st.session_state.vs_bytes = pdf_bytes
    st.session_state.vs_name  = uploaded.name
    st.session_state.vs_pages = total_pages

    size_mb = len(pdf_bytes) / 1_048_576
    est_s   = max(1, total_pages // max(workers, 1))

    # File info strip
    st.markdown(f"""
    <div style="background:#1A2240;border:1px solid #2E3A6A;border-radius:6px;
                padding:12px 18px;margin:12px 0;display:flex;gap:32px;align-items:center;">
      <div>
        <div style="color:#6A7AA8;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;">File</div>
        <div style="color:#E8EAFA;font-weight:600;">{uploaded.name}</div>
      </div>
      <div>
        <div style="color:#6A7AA8;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;">Pages</div>
        <div style="color:#A78BFA;font-weight:700;font-size:1.2rem;">{total_pages:,}</div>
      </div>
      <div>
        <div style="color:#6A7AA8;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;">Size</div>
        <div style="color:#E8EAFA;font-weight:600;">{size_mb:.1f} MB</div>
      </div>
      <div>
        <div style="color:#6A7AA8;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;">Mode</div>
        <div style="color:#E8EAFA;font-weight:600;">{'🧠 Vision + OCR' if api_key else '🔡 OCR only'}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    scan_running = (
        st.session_state.vs_thread is not None
        and st.session_state.vs_thread.is_alive()
    )

    if not scan_running:
        if st.button("▶  Start Scan", use_container_width=True):
            state = {
                "running": True, "done": False, "progress": 0,
                "elapsed": 0, "pages_per_sec": 0,
                "total_tables": 0, "total_words": 0,
                "native_pages": 0, "ocr_pages": 0, "vision_pages": 0,
                "avg_ocr_confidence": None, "avg_vision_confidence": None,
                "results": None, "error": None,
            }
            st.session_state.vs_state   = state
            st.session_state.vs_started = True

            t = threading.Thread(
                target=run_scan_threaded_vision,
                args=(pdf_bytes, total_pages, workers,
                      api_key or None, dpi, denoise, vision_threshold, state),
                daemon=True,
            )
            t.start()
            st.session_state.vs_thread = t
            st.rerun()
    else:
        st.button("⏳  Scanning…", disabled=True, use_container_width=True)

else:
    st.markdown("""
    <div style="background:#111830;border:1px dashed #2E3A6A;border-radius:8px;
                padding:40px;text-align:center;color:#2E3A6A;margin-top:12px;">
      <div style="font-size:2.5rem;margin-bottom:8px;">📄</div>
      <div style="font-size:1rem;">Upload a PDF above to begin</div>
    </div>
    """, unsafe_allow_html=True)

# ── Live progress ─────────────────────────────────────────────────────────────

state = st.session_state.vs_state

if state and state.get("running"):
    total = st.session_state.vs_pages
    done  = state.get("progress", 0)
    pct   = done / total if total else 0
    speed = state.get("pages_per_sec", 0)
    eta   = round((total - done) / speed) if speed > 0 else "…"

    st.markdown("---")
    st.markdown("**Scanning…**")
    st.progress(pct)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pages",   f"{done:,} / {total:,}")
    c2.metric("Native",  f"{state.get('native_pages', 0):,}")
    c3.metric("OCR",     f"{state.get('ocr_pages', 0):,}")
    c4.metric("Vision",  f"{state.get('vision_pages', 0):,}")
    c5.metric("ETA",     f"{eta}s" if isinstance(eta, int) else eta)

    time.sleep(0.25)
    st.rerun()

# ── Results ───────────────────────────────────────────────────────────────────

if state and state.get("done") and state.get("results"):
    results  = state["results"]
    filename = st.session_state.vs_name or "document.pdf"
    total_p  = st.session_state.vs_pages

    st.markdown("---")
    st.markdown("### ✅ Scan Complete")

    # ── Path breakdown visual ──
    native_p = state.get("native_pages", 0)
    ocr_p    = state.get("ocr_pages",    0)
    vision_p = state.get("vision_pages", 0)

    pct_n = native_p / total_p * 100 if total_p else 0
    pct_o = ocr_p    / total_p * 100 if total_p else 0
    pct_v = vision_p / total_p * 100 if total_p else 0

    st.markdown(f"""
    <div style="background:#1A2240;border:1px solid #2E3A6A;border-radius:6px;padding:14px 18px;margin:12px 0;">
      <div style="font-size:0.75rem;color:#6A7AA8;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px;">
        Path breakdown
      </div>
      <div style="display:flex;height:14px;border-radius:3px;overflow:hidden;gap:2px;">
        <div style="width:{pct_n:.1f}%;background:#0A7A5E;" title="Native: {native_p}"></div>
        <div style="width:{pct_o:.1f}%;background:#D4780A;" title="OCR: {ocr_p}"></div>
        <div style="width:{pct_v:.1f}%;background:#6B3FD4;" title="Vision: {vision_p}"></div>
      </div>
      <div style="display:flex;gap:24px;margin-top:8px;font-size:0.80rem;">
        <span style="color:#0A7A5E;">● Native  {native_p:,} ({pct_n:.0f}%)</span>
        <span style="color:#D4780A;">● OCR     {ocr_p:,} ({pct_o:.0f}%)</span>
        <span style="color:#6B3FD4;">● Vision  {vision_p:,} ({pct_v:.0f}%)</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Summary metrics ──
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Pages",       f"{total_p:,}")
    m2.metric("Tables",      f"{state['total_tables']:,}")
    m3.metric("Words",       f"{state['total_words']:,}")
    m4.metric("Time",        f"{state['elapsed']}s")
    m5.metric("OCR conf",    f"{state['avg_ocr_confidence']:.0%}"    if state.get('avg_ocr_confidence')    is not None else "—")
    m6.metric("Vision conf", f"{state['avg_vision_confidence']:.0%}" if state.get('avg_vision_confidence') is not None else "—")

    # ── Tabs ──
    tab_prev, tab_tables, tab_json, tab_dl = st.tabs([
        "📖 Preview", "📊 Tables", "{ } JSON", "⬇ Download"
    ])

    # ── Preview ──
    with tab_prev:
        st.caption("Navigate page by page. Badge shows which path processed each page.")
        page_num = st.slider("Page", 1, total_p, 1, key="vp_preview")
        pg = results[page_num - 1]

        if not pg or "error" in pg:
            st.error(f"Page {page_num} error: {pg.get('error','unknown') if pg else 'null'}")
        else:
            path = pg.get("path", "native")

            # Badge + confidence
            badge_extra = ""
            if path == "ocr" and pg.get("ocr_confidence") is not None:
                badge_extra = f"{pg['ocr_confidence']:.0%} conf"
            elif path == "vision" and pg.get("vision_confidence") is not None:
                badge_extra = f"{pg['vision_confidence']:.0%} conf"
            if pg.get("has_handwriting"):
                badge_extra += " · handwritten"

            st.markdown(path_badge(path, badge_extra), unsafe_allow_html=True)

            if path == "ocr" and pg.get("ocr_confidence") is not None:
                st.progress(pg["ocr_confidence"])
            elif path == "vision" and pg.get("vision_confidence") is not None:
                st.progress(pg["vision_confidence"])

            st.markdown("")

            for blk in pg.get("content", []):
                btype = blk.get("type")
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
                    st.caption(f"[{btype}] {blk.get('text','')}")

    # ── Tables ──
    with tab_tables:
        import pandas as pd
        all_tables = [
            {"page": pg["page"], "path": pg.get("path","?"), **blk}
            for pg in results if pg and "error" not in pg
            for blk in pg.get("content", [])
            if blk.get("type") == "table" and blk.get("rows")
        ]
        if not all_tables:
            st.info("No tables detected.")
        else:
            st.caption(f"{len(all_tables)} tables found")
            for i, tbl in enumerate(all_tables):
                rows = tbl.get("rows", [])
                path_lbl, color, _ = PATH_STYLES.get(tbl.get("path","native"), ("?","#888","#88888822"))
                with st.expander(
                    f"Table {i+1} — Page {tbl['page']}  [{path_lbl}]  "
                    f"({tbl.get('row_count', len(rows))} rows × {tbl.get('col_count', len(rows[0]) if rows else 0)} cols)",
                    expanded=(i < 3),
                ):
                    if len(rows) > 1:
                        df = pd.DataFrame(rows[1:], columns=_safe_columns(rows[0]))
                        st.dataframe(df, use_container_width=True)
                    else:
                        st.table(rows)

    # ── JSON ──
    with tab_json:
        st.caption("Full structured output per page including path, confidence, and content blocks.")
        page_j = st.slider("Page", 1, total_p, 1, key="vp_json")
        st.json(results[page_j - 1], expanded=2)

    # ── Download ──
    with tab_dl:
        st.markdown("**Download extracted data**")
        doc_meta = {
            "filename":             filename,
            "total_pages":          total_p,
            "native_pages":         native_p,
            "ocr_pages":            ocr_p,
            "vision_pages":         vision_p,
            "tables_found":         state["total_tables"],
            "word_count":           state["total_words"],
            "processing_time_s":    state["elapsed"],
            "pages_per_second":     state["pages_per_sec"],
            "avg_ocr_confidence":   state.get("avg_ocr_confidence"),
            "avg_vision_confidence":state.get("avg_vision_confidence"),
        }

        d1, d2, d3 = st.columns(3)

        if "JSON" in output_format:
            d1.download_button(
                "⬇ Download JSON",
                data=json.dumps({"document": doc_meta, "pages": results},
                                ensure_ascii=False, indent=2).encode(),
                file_name=f"{Path(filename).stem}_vision.json",
                mime="application/json",
                use_container_width=True,
            )

        if "Markdown" in output_format:
            lines = [f"# {filename}", ""]
            for pg in results:
                if not pg:
                    continue
                lines.append(f"\n<!-- Page {pg['page']} [{pg.get('path','?').upper()}] -->")
                if "error" in pg:
                    lines.append(f"> Error: {pg['error']}")
                    continue
                for blk in pg.get("content", []):
                    bt = blk.get("type")
                    if bt == "heading":
                        lines.append(f"\n## {blk['text']}")
                    elif bt == "paragraph":
                        lines.append(f"\n{blk['text']}")
                    elif bt == "table":
                        rows = blk.get("rows", [])
                        if rows:
                            h = rows[0]
                            lines += ["", "| " + " | ".join(h) + " |",
                                      "| " + " | ".join(["---"]*len(h)) + " |"]
                            for row in rows[1:]:
                                pad = row + [""] * max(0, len(h)-len(row))
                                lines.append("| " + " | ".join(pad[:len(h)]) + " |")
                            lines.append("")
            d2.download_button(
                "⬇ Download Markdown",
                data="\n".join(lines).encode(),
                file_name=f"{Path(filename).stem}_vision.md",
                mime="text/markdown",
                use_container_width=True,
            )

        if "Plain Text" in output_format:
            txt_lines = []
            for pg in results:
                if not pg:
                    continue
                txt_lines += ["", "="*60,
                              f"PAGE {pg['page']}  [{pg.get('path','?').upper()}]",
                              "="*60]
                for blk in pg.get("content", []):
                    bt = blk.get("type")
                    if bt in ("heading","paragraph"):
                        txt_lines.append(f"\n{blk['text']}")
                    elif bt == "table":
                        for row in blk.get("rows",[]):
                            txt_lines.append("  | " + " | ".join(row) + " |")
            d3.download_button(
                "⬇ Download Text",
                data="\n".join(txt_lines).encode(),
                file_name=f"{Path(filename).stem}_vision.txt",
                mime="text/plain",
                use_container_width=True,
            )

        st.markdown("---")
        st.caption("Scan summary")
        st.json(doc_meta)

elif state and state.get("error"):
    st.error(f"Scan failed: {state['error']}")
