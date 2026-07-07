#!/usr/bin/env python3
"""
Vision Pipeline — 3-path PDF extraction:
  Path A: Native text layer  → PyMuPDF  (fast, free)
  Path B: Scanned, high-conf → Tesseract OCR  (free, local)
  Path C: Low-conf / complex → Claude Vision API  (accurate, costs ~$0.003/page)

Usage:
    python vision_pipeline.py input.pdf --api-key sk-ant-...
    python vision_pipeline.py input.pdf --api-key sk-ant-... --vision-threshold 0.75

Requirements:
    pip install pymupdf pdfplumber pytesseract opencv-python-headless anthropic pillow
    brew install tesseract
"""

import fitz
import pdfplumber
import pytesseract
import anthropic
import base64
import cv2
import numpy as np
import json
import time
import argparse
import sys
import statistics
from pathlib import Path
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Tuple, Optional

# ── Constants ────────────────────────────────────────────────────────────────

HEADING_SCALE       = 1.15
FOOTER_Y_PCT        = 0.91
HEADER_Y_PCT        = 0.07
MIN_TABLE_ROWS      = 2
MIN_TABLE_COLS      = 2
NATIVE_MIN_CHARS    = 50    # fewer alnum chars → treat as scanned
DEFAULT_OCR_DPI     = 300
DEFAULT_VIS_THRESH  = 0.70  # Tesseract avg confidence below this → Claude Vision
MAX_IMAGE_BYTES     = 4_000_000  # 4 MB — resize if larger (Claude limit is 5 MB)

VISION_PROMPT = """You are a precise document extraction engine. Extract ALL text and structured data from this document page image.

Return ONLY a valid JSON object with this exact structure — no markdown, no explanation:
{
  "content": [
    {"type": "heading",   "text": "..."},
    {"type": "paragraph", "text": "..."},
    {"type": "table",     "rows": [["col1","col2"], ["val1","val2"]]},
    {"type": "footer",    "text": "..."}
  ],
  "confidence": 0.95,
  "has_handwriting": false,
  "notes": "brief observation if quality is poor or content is unusual"
}

Rules:
- Preserve reading order top-to-bottom, left-to-right
- For tables: first row must be column headers; capture ALL rows
- For handwritten text: transcribe as accurately as possible; mark has_handwriting true
- confidence: your 0.0–1.0 estimate of extraction completeness and accuracy
- If a region is illegible, include it as paragraph with text "[ILLEGIBLE]"
- Never omit content — partial extraction is better than skipping"""


# ── Path A: Native text (PyMuPDF) ───────────────────────────────────────────

def _detect_page_type(page: fitz.Page, min_chars: int = NATIVE_MIN_CHARS) -> str:
    alnum = sum(1 for c in page.get_text() if c.isalnum())
    return "native" if alnum >= min_chars else "scanned"


def _extract_native(page: fitz.Page) -> Tuple[List[Dict], int, int]:
    """Returns (content_blocks, table_count, word_count)."""
    page_h = page.rect.height

    # Tables
    tables_out, table_rects = [], []
    try:
        for tbl in page.find_tables():
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

    # Text blocks
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    all_sizes = [
        sp["size"]
        for b in text_dict.get("blocks", []) if b.get("type") == 0
        for ln in b.get("lines", [])
        for sp in ln.get("spans", []) if sp.get("size", 0) > 0
    ]
    median_font = statistics.median(all_sizes) if all_sizes else 10.0

    for blk in text_dict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        bbox = blk.get("bbox", [0, 0, 0, 0])
        if any(fitz.Rect(bbox).intersects(tr) for tr in table_rects):
            continue

        parts, blk_sizes, is_bold = [], [], False
        for ln in blk.get("lines", []):
            ln_text = ""
            for sp in ln.get("spans", []):
                t = sp.get("text", "").strip()
                if t:
                    ln_text += t + " "
                    if sp.get("size", 0) > 0:
                        blk_sizes.append(sp["size"])
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

        content.append({"type": btype, "text": full_text, "bbox": bbox, "font_size": round(blk_font, 1)})

    content.sort(key=lambda b: b.get("bbox", [0, 0, 0, 0])[1])
    table_count = sum(1 for b in content if b["type"] == "table")
    word_count  = sum(len(b.get("text", "").split()) for b in content if b["type"] in ("paragraph", "heading"))
    return content, table_count, word_count


# ── Path B: Tesseract OCR ────────────────────────────────────────────────────

def _page_to_pil(page: fitz.Page, dpi: int = DEFAULT_OCR_DPI) -> Image.Image:
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _preprocess(pil_img: Image.Image, denoise: bool = True) -> Image.Image:
    arr = np.array(pil_img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if denoise:
        binary = cv2.fastNlMeansDenoising(binary, h=10)
    return Image.fromarray(binary)


def _run_tesseract(pil_img: Image.Image) -> Tuple[List[Dict], float]:
    """Returns (content_blocks, avg_confidence 0-1)."""
    data = pytesseract.image_to_data(
        pil_img,
        output_type=pytesseract.Output.DICT,
        config="--psm 3 --oem 3",
    )

    # Group words into paragraph blocks by (block_num, par_num)
    blocks: Dict[Tuple, Dict] = {}
    confs = []
    n = len(data["text"])

    for i in range(n):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if not word or conf < 0:
            continue
        confs.append(conf)
        key = (data["block_num"][i], data["par_num"][i])
        if key not in blocks:
            blocks[key] = {
                "words": [],
                "x": data["left"][i],
                "y": data["top"][i],
                "x2": data["left"][i] + data["width"][i],
                "y2": data["top"][i] + data["height"][i],
            }
        b = blocks[key]
        b["words"].append(word)
        b["x"]  = min(b["x"],  data["left"][i])
        b["y"]  = min(b["y"],  data["top"][i])
        b["x2"] = max(b["x2"], data["left"][i] + data["width"][i])
        b["y2"] = max(b["y2"], data["top"][i] + data["height"][i])

    content = []
    for b in sorted(blocks.values(), key=lambda x: (x["y"], x["x"])):
        text = " ".join(b["words"]).strip()
        if text:
            content.append({
                "type": "paragraph",
                "text": text,
                "bbox": [b["x"], b["y"], b["x2"], b["y2"]],
            })

    avg_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return content, avg_conf


# ── Path C: Claude Vision ────────────────────────────────────────────────────

def _pil_to_base64(pil_img: Image.Image, max_bytes: int = MAX_IMAGE_BYTES) -> Tuple[str, str]:
    """Convert PIL image to base64 string. Resize if over size limit."""
    buf = BytesIO()
    pil_img.save(buf, format="JPEG", quality=85)
    if buf.tell() > max_bytes:
        scale = (max_bytes / buf.tell()) ** 0.5
        new_w = int(pil_img.width  * scale)
        new_h = int(pil_img.height * scale)
        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
        buf = BytesIO()
        pil_img.save(buf, format="JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"


def _run_vision(client: anthropic.Anthropic, pil_img: Image.Image) -> Tuple[List[Dict], float, bool]:
    """
    Call Claude Vision. Returns (content_blocks, confidence, has_handwriting).
    Falls back to empty result on any API error.
    """
    img_b64, media_type = _pil_to_base64(pil_img)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       img_b64,
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw)
        content = parsed.get("content", [])
        confidence = float(parsed.get("confidence", 0.9))
        has_hw = bool(parsed.get("has_handwriting", False))

        # Normalise: ensure each block has bbox = [] (Vision doesn't give coordinates)
        for blk in content:
            blk.setdefault("bbox", [])
            if "rows" not in blk and blk.get("type") == "table":
                blk["rows"] = []
            if blk.get("type") == "table":
                blk.setdefault("row_count", len(blk.get("rows", [])))
                blk.setdefault("col_count", len(blk["rows"][0]) if blk.get("rows") else 0)

        return content, confidence, has_hw

    except (json.JSONDecodeError, KeyError, anthropic.APIError) as e:
        return [{"type": "paragraph", "text": f"[Vision extraction failed: {e}]", "bbox": []}], 0.0, False


# ── Routing logic ────────────────────────────────────────────────────────────

def _should_use_vision(
    page: fitz.Page,
    page_type: str,
    ocr_confidence: float,
    vision_threshold: float,
) -> bool:
    """Decide whether to escalate a scanned page to Claude Vision."""
    if page_type == "native":
        return False
    if ocr_confidence < vision_threshold:
        return True
    # Also escalate if page has images but very few OCR words (likely form/diagram heavy)
    images = page.get_images()
    if len(images) >= 2 and ocr_confidence < 0.85:
        return True
    return False


# ── Main page processor ──────────────────────────────────────────────────────

def process_page(
    pdf_bytes: bytes,
    page_num: int,
    api_key: Optional[str] = None,
    dpi: int = DEFAULT_OCR_DPI,
    denoise: bool = True,
    vision_threshold: float = DEFAULT_VIS_THRESH,
) -> Dict[str, Any]:
    """
    Full 3-path processor. Opens its own fitz doc (thread-safe).
    Returns page result dict with `path` = 'native' | 'ocr' | 'vision'.
    """
    doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    page_h = page.rect.height
    page_w = page.rect.width

    page_type = _detect_page_type(page)
    path      = page_type   # default: 'native' or will become 'ocr'/'vision'
    ocr_conf  = None
    vision_conf = None
    has_hw    = False
    content   = []
    table_count = 0
    word_count  = 0

    try:
        if page_type == "native":
            # ── Path A ──────────────────────────────────────────────────────
            path = "native"
            content, table_count, word_count = _extract_native(page)

        else:
            # ── Path B: Try Tesseract first ─────────────────────────────────
            pil_img = _page_to_pil(page, dpi)
            processed = _preprocess(pil_img, denoise)
            ocr_content, ocr_conf = _run_tesseract(processed)

            client = anthropic.Anthropic(api_key=api_key) if api_key else None

            if client and _should_use_vision(page, page_type, ocr_conf, vision_threshold):
                # ── Path C: Claude Vision ────────────────────────────────────
                path = "vision"
                vis_content, vision_conf, has_hw = _run_vision(client, pil_img)
                content     = vis_content
                table_count = sum(1 for b in content if b.get("type") == "table")
                word_count  = sum(
                    len(b.get("text", "").split())
                    for b in content if b.get("type") in ("paragraph", "heading")
                )
            else:
                # ── Path B: Use Tesseract result ─────────────────────────────
                path        = "ocr"
                content     = ocr_content
                word_count  = sum(len(b.get("text", "").split()) for b in content)

    except Exception as e:
        content = [{"type": "paragraph", "text": f"[Error: {e}]", "bbox": []}]

    doc.close()

    return {
        "page":            page_num + 1,
        "width":           round(page_w, 1),
        "height":          round(page_h, 1),
        "path":            path,
        "ocr_confidence":  round(ocr_conf, 3)     if ocr_conf     is not None else None,
        "vision_confidence": round(vision_conf, 3) if vision_conf  is not None else None,
        "has_handwriting": has_hw,
        "content":         content,
        "table_count":     table_count,
        "word_count":      word_count,
    }


# ── Threaded runner (called by vision_app.py) ────────────────────────────────

def run_scan_threaded_vision(
    pdf_bytes: bytes,
    total_pages: int,
    workers: int,
    api_key: Optional[str],
    dpi: int,
    denoise: bool,
    vision_threshold: float,
    state: dict,
) -> None:
    """Background thread entry point. Updates `state` dict live."""
    t_start = time.perf_counter()
    results = [None] * total_pages
    state.update({"running": True, "done": False, "progress": 0,
                  "elapsed": 0, "pages_per_sec": 0, "error": None})

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    process_page, pdf_bytes, i,
                    api_key, dpi, denoise, vision_threshold
                ): i
                for i in range(total_pages)
            }
            for future in as_completed(futures):
                pn = futures[future]
                try:
                    results[pn] = future.result()
                except Exception as e:
                    results[pn] = {
                        "page": pn + 1, "path": "error", "error": str(e),
                        "content": [], "table_count": 0, "word_count": 0,
                    }
                done = sum(1 for r in results if r is not None)
                elapsed = time.perf_counter() - t_start
                state["progress"]      = done
                state["elapsed"]       = round(elapsed, 2)
                state["pages_per_sec"] = round(done / elapsed, 1) if elapsed > 0 else 0

        elapsed = time.perf_counter() - t_start

        native_pages = sum(1 for r in results if r and r.get("path") == "native")
        ocr_pages    = sum(1 for r in results if r and r.get("path") == "ocr")
        vision_pages = sum(1 for r in results if r and r.get("path") == "vision")

        ocr_confs = [r["ocr_confidence"] for r in results
                     if r and r.get("ocr_confidence") is not None]
        vis_confs = [r["vision_confidence"] for r in results
                     if r and r.get("vision_confidence") is not None]

        state.update({
            "results":           results,
            "total_tables":      sum(r.get("table_count", 0) for r in results if r),
            "total_words":       sum(r.get("word_count",  0) for r in results if r),
            "elapsed":           round(elapsed, 2),
            "pages_per_sec":     round(total_pages / elapsed, 1),
            "native_pages":      native_pages,
            "ocr_pages":         ocr_pages,
            "vision_pages":      vision_pages,
            "avg_ocr_confidence":   round(sum(ocr_confs) / len(ocr_confs), 3) if ocr_confs else None,
            "avg_vision_confidence":round(sum(vis_confs) / len(vis_confs), 3) if vis_confs else None,
            "done":  True,
        })

    except Exception as e:
        state["error"] = str(e)
        state["done"]  = True

    state["running"] = False


# ── Output writers ────────────────────────────────────────────────────────────

def _write_json(pages, meta, out_dir):
    p = Path(out_dir) / "output.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"document": meta, "pages": pages}, f, ensure_ascii=False, indent=2)
    return p

def _write_markdown(pages, meta, out_dir):
    lines = [f"# {meta.get('filename', 'Document')}", "",
             f"*{meta['total_pages']:,} pages · {meta['word_count']:,} words · "
             f"{meta['tables_found']:,} tables*", "", "---"]
    for pg in pages:
        lines.append(f"\n<!-- Page {pg['page']} ({pg.get('path','?')}) -->")
        if "error" in pg:
            lines.append(f"> Error: {pg['error']}")
            continue
        for blk in pg.get("content", []):
            btype = blk.get("type")
            if btype == "heading":
                lines.append(f"\n## {blk['text']}")
            elif btype == "paragraph":
                lines.append(f"\n{blk['text']}")
            elif btype == "table":
                rows = blk.get("rows", [])
                if rows:
                    h = rows[0]
                    lines += ["", "| " + " | ".join(h) + " |",
                              "| " + " | ".join(["---"] * len(h)) + " |"]
                    for row in rows[1:]:
                        pad = row + [""] * max(0, len(h) - len(row))
                        lines.append("| " + " | ".join(pad[:len(h)]) + " |")
                    lines.append("")
        lines.append(f"\n---")
    p = Path(out_dir) / "output.md"
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return p

def _write_text(pages, out_dir):
    lines = []
    for pg in pages:
        lines += ["", "=" * 60, f"PAGE {pg['page']}  [{pg.get('path','?').upper()}]", "=" * 60]
        if "error" in pg:
            lines.append(f"[ERROR: {pg['error']}]")
            continue
        for blk in pg.get("content", []):
            btype = blk.get("type")
            if btype in ("heading", "paragraph"):
                lines.append(f"\n{blk['text']}")
            elif btype == "table":
                for row in blk.get("rows", []):
                    lines.append("  | " + " | ".join(row) + " |")
    p = Path(out_dir) / "output.txt"
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return p


# ── CLI pipeline runner ──────────────────────────────────────────────────────

def run_vision_pipeline(
    pdf_path: str,
    api_key: Optional[str] = None,
    output_dir: str = "./vision_output",
    workers: int = 4,
    dpi: int = DEFAULT_OCR_DPI,
    denoise: bool = True,
    vision_threshold: float = DEFAULT_VIS_THRESH,
) -> Dict[str, Any]:

    pdf_path  = Path(pdf_path).resolve()
    out_dir   = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc         = fitz.open(str(pdf_path))
    total_pages = doc.page_count
    pdf_meta    = doc.metadata or {}
    doc.close()

    pdf_bytes = pdf_path.read_bytes()

    print(f"\n{'─'*62}")
    print("  Vision Pipeline — 3-Path PDF Extraction")
    print(f"{'─'*62}")
    print(f"  File             : {pdf_path.name}")
    print(f"  Pages            : {total_pages:,}")
    print(f"  Workers          : {workers}")
    print(f"  OCR DPI          : {dpi}")
    print(f"  Vision threshold : {vision_threshold:.0%}  {'(Claude API active)' if api_key else '(no API key — OCR only)'}")
    print(f"{'─'*62}\n")

    t_start  = time.perf_counter()
    results  = [None] * total_pages

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_page, pdf_bytes, i, api_key, dpi, denoise, vision_threshold): i
            for i in range(total_pages)
        }
        for future in as_completed(futures):
            pn = futures[future]
            try:
                results[pn] = future.result()
            except Exception as e:
                results[pn] = {"page": pn+1, "path": "error", "error": str(e),
                               "content": [], "table_count": 0, "word_count": 0}
            done    = sum(1 for r in results if r is not None)
            elapsed = time.perf_counter() - t_start
            pct     = done / total_pages * 100
            bar     = "█" * int(pct/4) + "░" * (25 - int(pct/4))
            speed   = round(done / elapsed, 1) if elapsed > 0 else 0
            print(f"\r  [{bar}] {pct:5.1f}%  {done:,}/{total_pages:,}  {speed} pg/s", end="", flush=True)

    elapsed = time.perf_counter() - t_start
    results.sort(key=lambda r: r.get("page", 0) if r else 0)

    native_pgs = sum(1 for r in results if r and r.get("path") == "native")
    ocr_pgs    = sum(1 for r in results if r and r.get("path") == "ocr")
    vis_pgs    = sum(1 for r in results if r and r.get("path") == "vision")
    ocr_confs  = [r["ocr_confidence"] for r in results if r and r.get("ocr_confidence") is not None]
    vis_confs  = [r["vision_confidence"] for r in results if r and r.get("vision_confidence") is not None]

    doc_meta = {
        "filename":             pdf_path.name,
        "total_pages":          total_pages,
        "native_pages":         native_pgs,
        "ocr_pages":            ocr_pgs,
        "vision_pages":         vis_pgs,
        "tables_found":         sum(r.get("table_count", 0) for r in results if r),
        "word_count":           sum(r.get("word_count",  0) for r in results if r),
        "processing_time_s":    round(elapsed, 2),
        "pages_per_second":     round(total_pages / elapsed, 1),
        "avg_ocr_confidence":   round(sum(ocr_confs)/len(ocr_confs), 3) if ocr_confs else None,
        "avg_vision_confidence":round(sum(vis_confs)/len(vis_confs), 3) if vis_confs else None,
        "pdf_title":            pdf_meta.get("title", ""),
        "pdf_author":           pdf_meta.get("author", ""),
    }

    print(f"\r  [{'█'*25}] 100.0%  {total_pages:,}/{total_pages:,}  done          \n")
    print("  Writing outputs...")

    p1 = _write_json(results, doc_meta, out_dir)
    p2 = _write_markdown(results, doc_meta, out_dir)
    p3 = _write_text(results, out_dir)

    print(f"\n{'─'*62}")
    print(f"  Done in {elapsed:.2f}s")
    print(f"{'─'*62}")
    print(f"  Native  : {native_pgs:,} pages (PyMuPDF)")
    print(f"  OCR     : {ocr_pgs:,} pages (Tesseract)")
    print(f"  Vision  : {vis_pgs:,} pages (Claude Vision)")
    if ocr_confs:
        print(f"  Avg OCR conf   : {sum(ocr_confs)/len(ocr_confs):.1%}")
    if vis_confs:
        print(f"  Avg Vision conf: {sum(vis_confs)/len(vis_confs):.1%}")
    print(f"\n  Outputs: {out_dir}/")
    for label, p in [("JSON", p1), ("Markdown", p2), ("Text", p3)]:
        print(f"    {label:<10} → {p.name}  ({p.stat().st_size//1024:,} KB)")
    print(f"{'─'*62}\n")

    return doc_meta


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Vision Pipeline — 3-path PDF extraction (Native / OCR / Claude Vision)"
    )
    parser.add_argument("pdf",                  help="Input PDF path")
    parser.add_argument("--api-key",            default=None,               help="Anthropic API key (sk-ant-...)")
    parser.add_argument("--output-dir",         default="./vision_output",  help="Output directory")
    parser.add_argument("--workers",            type=int, default=4,        help="Parallel threads (default 4; keep low if using Vision)")
    parser.add_argument("--dpi",                type=int, default=DEFAULT_OCR_DPI, help="OCR render DPI (default 300)")
    parser.add_argument("--vision-threshold",   type=float, default=DEFAULT_VIS_THRESH, help="OCR confidence below this → Claude Vision (default 0.70)")
    parser.add_argument("--no-denoise",         action="store_true",        help="Skip denoising (faster)")

    args = parser.parse_args()
    if not Path(args.pdf).exists():
        print(f"Error: {args.pdf} not found", file=sys.stderr)
        sys.exit(1)

    run_vision_pipeline(
        pdf_path=args.pdf,
        api_key=args.api_key,
        output_dir=args.output_dir,
        workers=args.workers,
        dpi=args.dpi,
        denoise=not args.no_denoise,
        vision_threshold=args.vision_threshold,
    )


if __name__ == "__main__":
    main()
