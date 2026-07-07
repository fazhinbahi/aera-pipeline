#!/usr/bin/env python3
"""
OCR Pipeline — Hybrid PDF processing with native text extraction + Tesseract OCR.

Handles both native PDFs (with text layers) and scanned PDFs (image-only pages).
Uses PyMuPDF for native text and Tesseract for OCR on scanned pages.

Usage:
    python ocr_pipeline.py input.pdf
    python ocr_pipeline.py input.pdf --output-dir ./results --workers 4 --dpi 300
    python ocr_pipeline.py input.pdf --dpi 150 --no-denoise

Requirements:
    pip install pymupdf pytesseract pillow opencv-python
    # Also requires Tesseract OCR installed on the system (brew install tesseract)
"""

import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output
import cv2
import numpy as np
from PIL import Image
import json
import time
import argparse
import sys
import statistics
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Tuple, Optional


# ── Constants ────────────────────────────────────────────────────────────────

HEADING_SCALE    = 1.15
FOOTER_Y_PCT     = 0.91
HEADER_Y_PCT     = 0.07
MIN_TABLE_ROWS   = 2
MIN_TABLE_COLS   = 2
MIN_CHARS_NATIVE = 50   # alphanumeric chars needed to classify page as native


# ── Page type detection ──────────────────────────────────────────────────────

def detect_page_type(page: fitz.Page, min_chars: int = MIN_CHARS_NATIVE) -> str:
    """
    Detect whether a page has a usable text layer or is image-only.

    Counts alphanumeric characters extracted by PyMuPDF. Pages with a real
    text layer typically have hundreds; scanned image-only pages have < 5.

    Returns:
        "native"  if alpha_count >= min_chars
        "scanned" otherwise
    """
    text = page.get_text("text")
    alpha_count = sum(1 for c in text if c.isalnum())
    return "native" if alpha_count >= min_chars else "scanned"


# ── OCR preprocessing ────────────────────────────────────────────────────────

def preprocess_for_ocr(pil_img: Image.Image, denoise: bool = True) -> Image.Image:
    """
    Preprocess a PIL Image for Tesseract OCR.

    Pipeline: grayscale → Otsu binarize → optional NL-means denoise.

    Args:
        pil_img: Input PIL Image (any mode)
        denoise: Apply cv2.fastNlMeansDenoising (slower, better quality)

    Returns:
        Preprocessed grayscale PIL Image ready for Tesseract.
    """
    # 1. Grayscale
    gray = np.array(pil_img.convert("L"))

    # 2. Otsu binarize
    _, binarized = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 3. Optional denoise
    if denoise:
        processed = cv2.fastNlMeansDenoising(
            binarized, h=10, templateWindowSize=7, searchWindowSize=21
        )
    else:
        processed = binarized

    return Image.fromarray(processed)


# ── Tesseract OCR ─────────────────────────────────────────────────────────────

def ocr_page_tesseract(pil_img: Image.Image) -> Tuple[str, float]:
    """
    Run Tesseract OCR on a (pre-processed) PIL Image.

    Args:
        pil_img: Grayscale/binary PIL Image

    Returns:
        (full_text, confidence) where confidence is 0.0–1.0
    """
    data = pytesseract.image_to_data(
        pil_img,
        output_type=Output.DICT,
        config="--psm 3",
    )

    words: List[str] = []
    confs: List[int] = []
    n = len(data["text"])
    for i in range(n):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if word and conf > 0:
            words.append(word)
            confs.append(conf)

    text = " ".join(words)
    confidence = (sum(confs) / (len(confs) * 100)) if confs else 0.0
    return text, round(confidence, 3)


# ── Native path: text + table extraction (PyMuPDF) ───────────────────────────

def _process_native_page(page: fitz.Page) -> Tuple[List[Dict], int, int]:
    """
    Extract text blocks and tables from a native-text PDF page.

    Returns:
        (content_blocks, table_count, word_count)
    """
    page_h = page.rect.height
    tables_out: List[Dict] = []
    table_rects: List[fitz.Rect] = []

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

    content: List[Dict] = list(tables_out)

    # Text block extraction
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    all_sizes: List[float] = []
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
        # Skip text that overlaps a table region
        if any(blk_rect.intersects(tr) for tr in table_rects):
            continue

        parts: List[str] = []
        blk_sizes: List[float] = []
        is_bold = False

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
            "bbox":      list(bbox),
            "font_size": round(blk_font, 1),
        })

    content.sort(key=lambda b: b.get("bbox", [0, 0, 0, 0])[1])

    table_count = sum(1 for b in content if b["type"] == "table")
    word_count  = sum(
        len(b.get("text", "").split())
        for b in content if b["type"] in ("paragraph", "heading")
    )
    return content, table_count, word_count


# ── Scanned path: render → preprocess → Tesseract ────────────────────────────

def _process_ocr_page(
    page: fitz.Page,
    dpi: int = 300,
    denoise: bool = True,
) -> Tuple[List[Dict], float, int]:
    """
    Process an image-only page using Tesseract OCR.

    Renders the page at `dpi`, preprocesses, runs Tesseract, and groups
    Tesseract word data into paragraph-level content blocks.

    Returns:
        (content_blocks, ocr_confidence 0–1, word_count)
    """
    page_w = page.rect.width
    page_h = page.rect.height

    # Render at DPI
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img_w, img_h = pixmap.width, pixmap.height

    pil_img = Image.frombytes("RGB", [img_w, img_h], pixmap.samples)

    # Preprocess
    preprocessed = preprocess_for_ocr(pil_img, denoise=denoise)

    # Run Tesseract word-level extraction
    data = pytesseract.image_to_data(
        preprocessed,
        output_type=Output.DICT,
        config="--psm 3",
    )

    # Group words by (block_num, par_num) → paragraph blocks
    para_blocks: Dict[Tuple[int, int], Dict] = {}
    n = len(data["text"])

    for i in range(n):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if not word or conf < 0:
            continue

        key = (data["block_num"][i], data["par_num"][i])
        x = data["left"][i]
        y = data["top"][i]
        w = data["width"][i]
        h = data["height"][i]

        if key not in para_blocks:
            para_blocks[key] = {
                "words": [],
                "confs": [],
                "x1": x,
                "y1": y,
                "x2": x + w,
                "y2": y + h,
            }

        blk = para_blocks[key]
        blk["words"].append(word)
        if conf > 0:
            blk["confs"].append(conf)
        blk["x1"] = min(blk["x1"], x)
        blk["y1"] = min(blk["y1"], y)
        blk["x2"] = max(blk["x2"], x + w)
        blk["y2"] = max(blk["y2"], y + h)

    # Scale pixel coords → PDF point coords
    scale_x = page_w / img_w if img_w > 0 else 1.0
    scale_y = page_h / img_h if img_h > 0 else 1.0

    content: List[Dict] = []
    all_confs: List[int] = []

    for key in sorted(para_blocks.keys()):
        blk = para_blocks[key]
        block_text = " ".join(blk["words"]).strip()
        if not block_text:
            continue
        raw_confs = blk["confs"]
        block_conf = (sum(raw_confs) / (len(raw_confs) * 100)) if raw_confs else 0.0
        all_confs.extend(raw_confs)

        bbox = [
            blk["x1"] * scale_x,
            blk["y1"] * scale_y,
            blk["x2"] * scale_x,
            blk["y2"] * scale_y,
        ]
        content.append({
            "type":           "paragraph",
            "text":           block_text,
            "bbox":           bbox,
            "ocr_confidence": round(block_conf, 3),
        })

    overall_conf = (sum(all_confs) / (len(all_confs) * 100)) if all_confs else 0.0
    word_count   = sum(len(b["text"].split()) for b in content)

    return content, round(overall_conf, 3), word_count


# ── Main page processor ───────────────────────────────────────────────────────

def process_page(
    pdf_bytes: bytes,
    page_num: int,
    dpi: int = 300,
    denoise: bool = True,
) -> Dict[str, Any]:
    """
    Full processing pipeline for a single PDF page.

    Auto-detects whether the page has a native text layer or is image-only,
    then routes to the appropriate extraction path.

    Args:
        pdf_bytes: Raw PDF file bytes (thread-safe — each call opens its own doc)
        page_num:  0-indexed page number
        dpi:       Render DPI for OCR path (higher = better quality, slower)
        denoise:   Whether to denoise before OCR

    Returns:
        {
            "page":           int (1-indexed),
            "path":           "native" | "ocr",
            "ocr_confidence": float 0–1 | None,
            "content":        list of content blocks,
            "table_count":    int,
            "word_count":     int,
            "width":          float,
            "height":         float,
        }
    """
    doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    page_w = page.rect.width
    page_h = page.rect.height

    page_type = detect_page_type(page)

    if page_type == "native":
        content, table_count, word_count = _process_native_page(page)
        doc.close()
        return {
            "page":           page_num + 1,
            "path":           "native",
            "ocr_confidence": None,
            "content":        content,
            "table_count":    table_count,
            "word_count":     word_count,
            "width":          round(page_w, 1),
            "height":         round(page_h, 1),
        }
    else:
        content, ocr_confidence, word_count = _process_ocr_page(page, dpi=dpi, denoise=denoise)
        doc.close()
        return {
            "page":           page_num + 1,
            "path":           "ocr",
            "ocr_confidence": ocr_confidence,
            "content":        content,
            "table_count":    0,
            "word_count":     word_count,
            "width":          round(page_w, 1),
            "height":         round(page_h, 1),
        }


# ── Threaded runner for Streamlit ─────────────────────────────────────────────

def run_scan_threaded_ocr(
    pdf_bytes: bytes,
    total_pages: int,
    workers: int,
    dpi: int,
    denoise: bool,
    state: dict,
) -> None:
    """
    Run the full OCR pipeline in a background thread, updating `state` live.

    Uses ThreadPoolExecutor (NOT multiprocessing) for Streamlit compatibility.
    The caller should start this in a daemon threading.Thread and poll `state`.

    Args:
        pdf_bytes:   Raw PDF bytes
        total_pages: Total page count
        workers:     Thread pool size
        dpi:         OCR rendering DPI (scanned pages only)
        denoise:     Whether to denoise before OCR
        state:       Shared dict updated live; keys:
                       running, done, progress, elapsed, pages_per_sec,
                       results, total_tables, total_words,
                       native_pages, ocr_pages, avg_ocr_confidence, error
    """
    t_start = time.perf_counter()
    results: List[Optional[Dict]] = [None] * total_pages
    state["running"]  = True
    state["done"]     = False
    state["progress"] = 0
    state["error"]    = None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(process_page, pdf_bytes, i, dpi, denoise): i
                for i in range(total_pages)
            }
            completed = 0
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    results[page_num] = future.result()
                except Exception as exc:
                    results[page_num] = {
                        "page":           page_num + 1,
                        "path":           "native",
                        "ocr_confidence": None,
                        "error":          str(exc),
                        "content":        [],
                        "table_count":    0,
                        "word_count":     0,
                    }
                completed += 1
                elapsed = time.perf_counter() - t_start
                state["progress"]      = completed
                state["elapsed"]       = round(elapsed, 2)
                state["pages_per_sec"] = round(completed / elapsed, 1) if elapsed > 0 else 0

        elapsed = time.perf_counter() - t_start

        native_pages = sum(1 for p in results if p and p.get("path") == "native")
        ocr_pages    = sum(1 for p in results if p and p.get("path") == "ocr")
        ocr_confs    = [
            p["ocr_confidence"]
            for p in results
            if p and p.get("ocr_confidence") is not None
        ]

        state["results"]            = results
        state["total_tables"]       = sum(p.get("table_count", 0) for p in results if p)
        state["total_words"]        = sum(p.get("word_count",  0) for p in results if p)
        state["native_pages"]       = native_pages
        state["ocr_pages"]          = ocr_pages
        state["avg_ocr_confidence"] = round(sum(ocr_confs) / len(ocr_confs), 3) if ocr_confs else None
        state["elapsed"]            = round(elapsed, 2)
        state["pages_per_sec"]      = round(total_pages / elapsed, 1) if elapsed > 0 else 0
        state["done"]               = True

    except Exception as exc:
        state["error"] = str(exc)
        state["done"]  = True

    state["running"] = False


# ── Output writers ────────────────────────────────────────────────────────────

def _bar(done: int, total: int, width: int = 28) -> str:
    pct    = done / total if total else 0
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


def _write_json(pages: List[Dict], meta: Dict, out_dir: Path) -> Path:
    path = out_dir / "output.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"document": meta, "pages": pages}, fh, ensure_ascii=False, indent=2)
    return path


def _write_markdown(pages: List[Dict], meta: Dict, out_dir: Path) -> Path:
    native_p = meta.get("native_pages", 0)
    ocr_p    = meta.get("ocr_pages", 0)
    lines = [
        f"# {meta.get('filename', 'Document')}",
        "",
        f"*{meta['total_pages']:,} pages · {meta['word_count']:,} words · "
        f"{meta['tables_found']:,} tables · "
        f"{native_p} native · {ocr_p} OCR · "
        f"processed in {meta['processing_time_s']}s*",
        "",
        "---",
    ]

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

    path = out_dir / "output.md"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _write_text(pages: List[Dict], out_dir: Path) -> Path:
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

    path = out_dir / "output.txt"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _write_summary(meta: Dict, out_dir: Path) -> Path:
    path = out_dir / "summary.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return path


# ── Main pipeline (CLI) ───────────────────────────────────────────────────────

def run_ocr_pipeline(
    pdf_path: str,
    output_dir: str = "./ocr_output",
    workers: Optional[int] = None,
    dpi: int = 300,
    denoise: bool = True,
    write_json_out: bool = True,
    write_md_out: bool = True,
    write_txt_out: bool = True,
) -> Dict[str, Any]:
    """
    Main OCR pipeline entry point for CLI and programmatic use.

    Processes every page via ThreadPoolExecutor, auto-routing each page
    to native (PyMuPDF) or OCR (Tesseract) path as appropriate.

    Returns the summary metadata dict.
    """
    import multiprocessing as _mp

    pdf_path = Path(pdf_path).resolve()
    out_dir  = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if workers is None:
        workers = min(_mp.cpu_count(), 4)

    with open(str(pdf_path), "rb") as fh:
        pdf_bytes = fh.read()

    doc       = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pgs = doc.page_count
    pdf_meta  = doc.metadata or {}
    doc.close()

    print(f"\n{'─' * 62}")
    print("  OCR Pipeline — Native + Tesseract Hybrid Extraction")
    print(f"{'─' * 62}")
    print(f"  File    : {pdf_path.name}")
    print(f"  Pages   : {total_pgs:,}")
    print(f"  Workers : {workers}  ·  DPI : {dpi}  ·  Denoise : {denoise}")
    print(f"  Output  : {out_dir}")
    print(f"{'─' * 62}\n")

    t_start: float = time.perf_counter()
    page_results: List[Optional[Dict]] = [None] * total_pgs

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_page, pdf_bytes, i, dpi, denoise): i
            for i in range(total_pgs)
        }
        completed = 0
        for future in as_completed(futures):
            page_num = futures[future]
            try:
                page_results[page_num] = future.result()
            except Exception as exc:
                page_results[page_num] = {
                    "page":           page_num + 1,
                    "path":           "native",
                    "ocr_confidence": None,
                    "error":          str(exc),
                    "content":        [],
                    "table_count":    0,
                    "word_count":     0,
                }
            completed += 1
            print(
                f"\r  [{_bar(completed, total_pgs)}] "
                f"{completed / total_pgs * 100:5.1f}%  "
                f"{completed:,}/{total_pgs:,}",
                end="",
                flush=True,
            )

    elapsed = time.perf_counter() - t_start
    print(f"\r  [{'█' * 28}] 100.0%  {total_pgs:,}/{total_pgs:,}  ({elapsed:.1f}s)  \n")

    # Sort by page number (futures complete out of order)
    page_results.sort(key=lambda p: p.get("page", 0) if p else 0)

    native_pages = sum(1 for p in page_results if p and p.get("path") == "native")
    ocr_pages    = sum(1 for p in page_results if p and p.get("path") == "ocr")
    ocr_confs    = [
        p["ocr_confidence"]
        for p in page_results
        if p and p.get("ocr_confidence") is not None
    ]
    total_tables = sum(p.get("table_count", 0) for p in page_results if p)
    total_words  = sum(p.get("word_count",  0) for p in page_results if p)
    error_pages  = sum(1 for p in page_results if p and "error" in p)

    doc_meta: Dict[str, Any] = {
        "filename":           pdf_path.name,
        "total_pages":        total_pgs,
        "native_pages":       native_pages,
        "ocr_pages":          ocr_pages,
        "avg_ocr_confidence": round(sum(ocr_confs) / len(ocr_confs), 3) if ocr_confs else None,
        "processed_pages":    total_pgs - error_pages,
        "error_pages":        error_pages,
        "tables_found":       total_tables,
        "word_count":         total_words,
        "processing_time_s":  round(elapsed, 2),
        "pages_per_second":   round(total_pgs / elapsed, 1) if elapsed > 0 else 0,
        "dpi":                dpi,
        "denoise":            denoise,
        "pdf_title":          pdf_meta.get("title", ""),
        "pdf_author":         pdf_meta.get("author", ""),
    }

    print("  Writing outputs...")
    paths: Dict[str, Path] = {}
    if write_json_out:
        paths["json"]    = _write_json(page_results, doc_meta, out_dir)
    if write_md_out:
        paths["markdown"]= _write_markdown(page_results, doc_meta, out_dir)
    if write_txt_out:
        paths["text"]    = _write_text(page_results, out_dir)
    paths["summary"]     = _write_summary(doc_meta, out_dir)

    print(f"\n{'─' * 62}")
    print(f"  Done in {elapsed:.2f}s  →  {doc_meta['pages_per_second']} pages/sec")
    print(f"{'─' * 62}")
    print(f"  Pages processed : {doc_meta['processed_pages']:,}")
    print(f"  Native pages    : {native_pages:,}")
    print(f"  OCR pages       : {ocr_pages:,}")
    if ocr_confs:
        print(f"  Avg OCR conf.   : {doc_meta['avg_ocr_confidence']:.1%}")
    print(f"  Tables found    : {total_tables:,}")
    print(f"  Words extracted : {total_words:,}")
    if error_pages:
        print(f"  Pages w/ errors : {error_pages}")
    print(f"\n  Outputs saved to: {out_dir}/")
    for label, p in paths.items():
        size_kb = p.stat().st_size // 1024
        print(f"    {label:<10} → {p.name}  ({size_kb:,} KB)")
    print(f"{'─' * 62}\n")

    return doc_meta


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ocr_pipeline",
        description="Hybrid PDF extractor: native text + Tesseract OCR for scanned pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ocr_pipeline.py report.pdf
  python ocr_pipeline.py report.pdf --output-dir ./out --workers 4 --dpi 300
  python ocr_pipeline.py report.pdf --dpi 150 --no-denoise
        """,
    )
    parser.add_argument("pdf",           help="Path to input PDF file")
    parser.add_argument("--output-dir",  default="./ocr_output", help="Output directory (default: ./ocr_output)")
    parser.add_argument("--workers",     type=int, default=None,  help="Parallel workers (default: CPU count, max 4)")
    parser.add_argument("--dpi",         type=int, default=300,   help="OCR rendering DPI (default: 300)")
    parser.add_argument("--no-denoise",  action="store_true",     help="Skip NL-means denoising (faster)")
    parser.add_argument("--no-json",     action="store_true",     help="Skip JSON output")
    parser.add_argument("--no-md",       action="store_true",     help="Skip Markdown output")
    parser.add_argument("--no-text",     action="store_true",     help="Skip plain text output")

    args = parser.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"Error: file not found — {args.pdf}", file=sys.stderr)
        sys.exit(1)
    if pdf.suffix.lower() != ".pdf":
        print(f"Warning: file does not have .pdf extension — {args.pdf}", file=sys.stderr)

    run_ocr_pipeline(
        pdf_path       = str(pdf),
        output_dir     = args.output_dir,
        workers        = args.workers,
        dpi            = args.dpi,
        denoise        = not args.no_denoise,
        write_json_out = not args.no_json,
        write_md_out   = not args.no_md,
        write_txt_out  = not args.no_text,
    )


if __name__ == "__main__":
    main()
