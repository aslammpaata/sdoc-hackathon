"""Stage 4 — the 7 compared fields out of one document, into the canonical schema.

One model call per document, temperature 0, response shape fixed by a schema.
Labels are mapped to canonical field names in ANY language; the value is kept as
the document states it, because resolving a port to a UN/LOCODE here would hide
the very defect stage 5 is looking for (see app/compare.py). Every value carries
the verbatim text it came from — stage 6 renders that as the evidence trail.
"""

from __future__ import annotations

from app import compare, llm_client
from app.schema import COMPARED_FIELDS, DocType, Extraction, ExtractedDocument, empty_extraction

PROMPT_VERSION = "1.0"

# The same field under every label this domain uses, in the languages the dataset
# and the contract call for. The model matches meaning, not this list literally.
FIELD_LABELS = {
    "shipper": "Shipper, Shipper/Exporter, Exporter, Consignor, 发货人, 托运人, المصدر, Pengirim",
    "consignee": "Consignee, Consignee (Non-Negotiable), Buyer, 收货人, المرسل إليه, Penerima",
    "notify_party": "Notify, Notify Party, Notify Address, 通知人, الطرف المخطر, Pihak Untuk Dimaklumkan",
    "port_of_loading": "Port of Loading, POL, Load Port, Loading Port, 装货港, 起运港, ميناء الشحن, Pelabuhan Muat",
    "port_of_discharge": "Port of Discharge, POD, Discharge Port, Destination Port, 卸货港, 目的港, ميناء التفريغ, Pelabuhan Bongkar",
    "container_count": "Container Count, No. of Containers, Total Containers, Containers, 箱数, 集装箱数量, عدد الحاويات",
    "gross_weight_kg": "Gross Weight, Gross Wt, G.W., Total Gross Weight, 毛重, الوزن الإجمالي, Berat Kotor",
}

SYSTEM_PROMPT = (
    "You read one shipping document and report seven fields. The document may be in any "
    "language; match each field by meaning, never by the exact label text.\n\n"
    + "\n".join(f"- {f}: {labels}" for f, labels in FIELD_LABELS.items())
    + "\n\nRules:\n"
    "1. A party (shipper/consignee/notify_party) value is the legal entity NAME only — no "
    "address, no PO box, no phone, no 'on behalf of' continuation. Put the whole block, "
    "addresses included, in original_text.\n"
    "2. A port value is the port EXACTLY as this document writes it, including any code in "
    "brackets. Do NOT convert it to a UN/LOCODE, do not translate it, do not expand it.\n"
    "3. container_count is the number of containers only: \"6 x 40'HC\" -> \"6\".\n"
    "4. gross_weight_kg is the TOTAL gross weight in KILOGRAMS, not a per-container figure. "
    "If the document uses another unit, convert it and say so in original_text, e.g. "
    "\"24 MT (converted: 24000 kg)\".\n"
    "5. Never guess. If the document does not state a field, return value null, "
    "original_text null, confidence 0. A missing value costs far less than an invented one.\n"
    "6. If a label is present but its value is a placeholder — TBA, TBD, N/A, a blank rule "
    "like ____ — return value null, original_text the placeholder verbatim, confidence 0.\n"
    "7. original_text is verbatim from the document and a human will read it beside the "
    "value. Never return a value without it.\n"
    "8. source is the nearest location marker in the text, e.g. \"p2\" or \"S.I.!B7\". "
    "If the text carries no markers, use the file name.\n"
    "9. confidence is 0.0-1.0: how sure you are the value belongs to that field."
)

_FIELD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "value": {"type": "STRING", "nullable": True},
        "original_text": {"type": "STRING", "nullable": True},
        "source": {"type": "STRING", "nullable": True},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["value", "original_text", "source", "confidence"],
}
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {f: _FIELD_SCHEMA for f in COMPARED_FIELDS},
    "required": list(COMPARED_FIELDS),
}


def build_prompt(text: str, doc_type: DocType | str, filename: str) -> str:
    return (
        f"Document type: {doc_type}\nFile name: {filename}\n"
        "Text follows; [...] markers are page or cell locations you can cite as source.\n"
        f'"""\n{text[:20000]}\n"""\n\nReport the seven fields.'
    )


def _canonical(field: str, value):
    """container_count -> int, gross_weight_kg -> float kg, everything else a clean string."""
    if value is None or str(value).strip() == "":
        return None
    if field == "container_count":
        return compare.norm_count(value)
    if field == "gross_weight_kg":
        return compare.norm_weight(value)
    return str(value).strip()


def extract_document(text: str, filename: str, doc_type: DocType | str, method: str) -> ExtractedDocument:
    """The 7 fields from one document. Raises on model failure — the caller records it."""
    result = llm_client.generate_json(
        build_prompt(text, doc_type, filename),
        RESPONSE_SCHEMA,
        system=SYSTEM_PROMPT,
        max_output_tokens=2048,
    )
    document: ExtractedDocument = {}
    for field in COMPARED_FIELDS:
        reported = result.get(field) or {}
        value = _canonical(field, reported.get("value"))
        document[field] = Extraction(
            value=value,
            original_text=(reported.get("original_text") or "").strip() or None,
            source=(reported.get("source") or "").strip() or filename,
            method=method,
            confidence=float(reported.get("confidence") or 0.0) if value is not None else 0.0,
        )
    return document


def empty_document() -> ExtractedDocument:
    """All seven fields, nothing found — what a case that never reached extraction carries."""
    return {f: empty_extraction() for f in COMPARED_FIELDS}
