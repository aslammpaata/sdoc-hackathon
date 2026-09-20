"""Stage 3 — validate & route: real format from the bytes, a reader per format,
business document type from the content.

Runs only for BL_COMPARISON cases. Nothing here trusts a filename: the format
comes from magic numbers and the document type from what the text says, in any
language. A case that cannot be read exits here, before extraction cost is spent.

Scanned pages have no OCR engine behind them — the multimodal model reads the
bytes directly, so there is no tesseract/poppler dependency to ship.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from app import llm_client, store
from app.schema import Case, DocType, ExtractionMethod, ReviewReason

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Format — from the first bytes, never the extension
# ---------------------------------------------------------------------------


def sniff(data: bytes) -> str:
    """"pdf" | "docx" | "xlsx" | "image" | "text" | "unknown".

    A file named .pdf that is really a DOCX reads as docx: both OOXML formats are
    zip containers, told apart by the part they carry.
    """
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG")):
        return "image"
    try:
        data.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unknown"


# ---------------------------------------------------------------------------
# Readers — each emits plain text with [location] markers stage 4 cites as `source`
# ---------------------------------------------------------------------------


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    pages = PdfReader(io.BytesIO(data)).pages
    return "\n".join(f"[p{i}]\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))


def _docx(data: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs]
    # SI/BL data is usually tabular — a label cell beside its value cell.
    lines += [
        " | ".join(c.text.replace("\n", " / ") for c in row.cells) for t in doc.tables for row in t.rows
    ]
    return "\n".join(line for line in lines if line.strip())


def _xlsx(data: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    rows = []
    for ws in wb:
        for row in ws.iter_rows():
            cells = [f"{c.value} [{ws.title}!{c.coordinate}]" for c in row if c.value is not None]
            if cells:
                rows.append(" | ".join(cells))
    return "\n".join(rows)


VISION_PROMPT = (
    "Transcribe this shipping document. Output every line of text exactly as it appears, "
    "in reading order, keeping each label together with its value on one line. "
    "Do not translate, summarise or add anything."
)


def _vision(data: bytes, mime: str) -> str:
    """A scanned page read by the multimodal model — our OCR, without an OCR engine."""
    return llm_client.generate(VISION_PROMPT, media=(data, mime), max_output_tokens=4096)


def _has_text(text: str) -> bool:
    """Enough real characters to extract from — page markers and whitespace don't count."""
    return len(re.sub(r"\[[^\]]*\]|\s", "", text)) >= 40


def read_text(data: bytes, filename: str = "") -> tuple[str, ExtractionMethod]:
    """(text, how it was read). Never raises: an unreadable file returns empty text."""
    fmt = sniff(data)
    try:
        if fmt == "text":
            return data.decode("utf-8", errors="replace"), ExtractionMethod.TXT
        if fmt == "docx":
            return _docx(data), ExtractionMethod.DOCX
        if fmt == "xlsx":
            return _xlsx(data), ExtractionMethod.XLSX
        if fmt == "image":
            mime = "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "image/png"
            return _vision(data, mime), ExtractionMethod.OCR
        if fmt == "pdf":
            text = _pdf(data)
            if _has_text(text):
                return text, ExtractionMethod.PDF_TEXT
            # No text layer: a scan. Hand the PDF itself to the model.
            return _vision(data, "application/pdf"), ExtractionMethod.OCR
    except Exception as e:  # noqa: BLE001 — a broken file is a review reason, not a crash
        log.warning("read_text failed for %s (%s): %s", filename, fmt, e)
        return "", ExtractionMethod.OCR if fmt == "image" else ExtractionMethod.PDF_TEXT
    return "", ExtractionMethod.TXT


# ---------------------------------------------------------------------------
# Business document type — from the content, in any language
# ---------------------------------------------------------------------------

# Order matters: an SI is frequently *titled* "BILL OF LADING INSTRUCTION" (32 files
# in this dataset), so the word INSTRUCTION has to win over "BILL OF LADING", and a
# document that is neither has to be recognised before either.
OTHER_MARKERS = ("COMMERCIAL INVOICE", "PACKING LIST", "CERTIFICATE OF ORIGIN", "发票", "装箱单", "原产地证", "فاتورة")
SI_MARKERS = ("SHIPPING INSTRUCTION", "SHIPPER'S INSTRUCTION", "BL INSTRUCTION", "B/L INSTRUCTION",
              "BILL OF LADING INSTRUCTION", "装货单", "托运单", "订舱单", "تعليمات الشحن", "ARAHAN PENGHANTARAN")
BL_MARKERS = ("BILL OF LADING", "B/L NO", "B/L NUMBER", "WAYBILL", "提单", "بوليصة الشحن")

DOC_TYPE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"doc_type": {"type": "STRING", "enum": [d.value for d in DocType]}},
    "required": ["doc_type"],
}


def doc_type(text: str) -> DocType:
    """SI | BL | OTHER from the document's own words. Filenames lie; this doesn't."""
    head = text[:400].upper()
    if any(m in head for m in OTHER_MARKERS):
        return DocType.OTHER
    if any(m in head for m in SI_MARKERS):
        return DocType.SI
    if any(m in head for m in BL_MARKERS):
        return DocType.BL
    if not text.strip():
        return DocType.OTHER
    try:  # unrecognised wording or an unseen language — ask the model
        result = llm_client.generate_json(
            "Classify this shipping document, which may be in any language.\n"
            "SI = an instruction to the carrier saying how to issue the bill of lading "
            "(titled SHIPPING INSTRUCTION, BL INSTRUCTION, BILL OF LADING INSTRUCTION, 装货单...).\n"
            "BL = the bill of lading itself, usually a draft.\n"
            "OTHER = anything else: invoice, packing list, certificate.\n\n"
            f'"""\n{text[:3000]}\n"""',
            DOC_TYPE_SCHEMA,
        )
        return DocType(result["doc_type"])
    except Exception as e:  # noqa: BLE001
        log.warning("doc_type fallback failed: %s", e)
        return DocType.OTHER


# ---------------------------------------------------------------------------
# Stage entrypoint
# ---------------------------------------------------------------------------


def validate_case(case: Case) -> dict:
    """Read every attachment and pick the SI and the BL.

    Returns {"si": doc, "bl": doc, "readers": {filename: method}} when the case can
    go on to extraction, otherwise {"review_reason": ..., "readers": ...}. Each doc
    is {"text", "filename", "doc_type", "method"} — exactly what stage 4 needs.
    """
    attachments = case.get("attachments") or []
    readers: dict[str, str] = {}
    docs = []
    for a in attachments:
        data = store.get_attachment(case["email_id"], a["filename"])
        text, method = read_text(data, a["filename"])
        readers[a["filename"]] = str(method) if _has_text(text) else "unreadable"
        if _has_text(text):
            docs.append(
                {"filename": a["filename"], "text": text, "method": str(method), "doc_type": doc_type(text)}
            )

    if len(attachments) < 2:
        return {"review_reason": ReviewReason.MISSING_ATTACHMENT, "readers": readers}
    if len(docs) < 2:
        return {"review_reason": ReviewReason.UNREADABLE, "readers": readers}
    si = next((d for d in docs if d["doc_type"] == DocType.SI), None)
    bl = next((d for d in docs if d["doc_type"] == DocType.BL), None)
    if si is None or bl is None:
        # Both files arrived and both were readable, but they are not an SI/BL pair.
        return {"review_reason": ReviewReason.WRONG_DOC_TYPE, "readers": readers}
    return {"si": si, "bl": bl, "readers": readers}
