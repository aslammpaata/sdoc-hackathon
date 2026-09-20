"""Stage 5 — normalise, then compare. No LLM: every outcome here is reproducible.

SI is the source of truth; the BL is checked against it. The normalisers are plain
named functions so stage 6 and the review UI can reuse them and so they are quick
to fix under time pressure.

Two rules drive the design:
  - Language alone must never produce a mismatch. Ports resolve to UN/LOCODE
    through the alias table below, which carries non-Latin spellings.
  - A port's NAME decides, not a code in brackets beside it. Every planted port
    defect in this dataset keeps the SI's code and swaps the city
    ("MOMBASA, KENYA (KEMBA)" -> "TUTICORIN, INDIA (KEMBA)"), so trusting the
    bracketed code would launder the defect into a match.
"""

from __future__ import annotations

import re

from app.schema import COMPARED_FIELDS, Comparison, Extraction, ExtractedDocument, Reason

RULE_VERSION = "1.0"

# Weight differs between documents by formatting, never by this much: the smallest
# real discrepancy in the dataset is 500 kg, the largest formatting gap is zero.
WEIGHT_TOLERANCE_KG = 0.5

_BLANK = {"", "-", "--", "---", "N/A", "NA", "N.A.", "TBA", "TBD", "TBC", "NIL", "NONE", "PENDING", "UNKNOWN", "?"}

# Legal-form suffixes carry no identity: "MOORIM SP CO., LTD" is "MOORIM SP".
_SUFFIX = re.compile(
    r"\b(?:CO\.?\s*,?\s*LTD|CO\.?\s*,?\s*LIMITED|COMPANY|LIMITED|LTD|INC(?:ORPORATED)?|CORP(?:ORATION)?|"
    r"PTE|PVT|PRIVATE|L\.?L\.?C|PLC|GMBH|AG|B\.?V|N\.?V|S\.?A|SARL|SRL|SPA|OY|AB|A/S|"
    r"SDN\.?\s*BHD|BHD|FZE|FZ\s*-?\s*LLC|FZCO|DMCC|PTY|JSC|OOO|UAB|TBK|PT|CO)\b\.?",
    re.IGNORECASE,
)
# A party block puts the address after the first separator; only the head is the name.
_ADDRESS_SPLIT = re.compile(r"\s*(?:\||/|;|\n)\s*")

_LOCODE = re.compile(r"\(([A-Z]{2}[A-Z0-9]{3})\)")

# Only the ports this dataset actually uses, plus the non-Latin spellings the
# contract requires. Longest key wins, so "RUGAO/NANTONG/SHANGHAI" beats "NANTONG".
PORT_ALIASES: dict[str, str] = {
    "PORT KLANG": "MYPKG", "KELANG": "MYPKG", "巴生港": "MYPKG", "ميناء كلانج": "MYPKG",
    "RUGAO/NANTONG/SHANGHAI": "CNSHA", "SHANGHAI": "CNSHA", "上海": "CNSHA", "شنغهاي": "CNSHA",
    "NANTONG": "CNNTG", "南通": "CNNTG",
    "NHAVA SHEVA": "INNSA", "JAWAHARLAL NEHRU": "INNSA",
    "TUTICORIN": "INTUT", "THOOTHUKUDI": "INTUT",
    "BUATAN": "IDBUA",
    "SINGAPORE": "SGSIN", "新加坡": "SGSIN", "سنغافورة": "SGSIN",
    "CALLAO": "PECLL", "MERSIN": "TRMER", "CONAKRY": "GNCKY", "APAPA": "NGAPP", "LAGOS": "NGAPP",
    "GDANSK": "PLGDN", "KLAIPEDA": "LTKLJ", "SAVANNAH": "USSAV", "KOPER": "SIKOP",
    "YANGON": "MMRGN", "RANGOON": "MMRGN", "BRISBANE": "AUBNE", "NEW YORK": "USNYC",
    "ASHDOD": "ILASH", "KARACHI": "PKKHI", "HOCHIMINH": "VNSGN", "HO CHI MINH": "VNSGN",
    "SAIGON": "VNSGN", "LONG BEACH": "USLGB", "MOMBASA": "KEMBA", "FREMANTLE": "AUFRE",
    "PYEONGTAEK": "KRPTK", "VALPARAISO": "CLVAP", "HOUSTON": "USHOU", "BALTIMORE": "USBAL",
    "JEBEL ALI": "AEJEA", "جبل علي": "AEJEA", "BUSAN": "KRPUS", "PUSAN": "KRPUS", "釜山": "KRPUS",
    "AQABA": "JOAQB", "العقبة": "JOAQB", "CEBU": "PHCEB", "LE HAVRE": "FRLEH", "DUBAI": "AEDXB",
}
_PORT_KEYS = sorted(PORT_ALIASES, key=len, reverse=True)

_UNIT_TO_KG = {
    "KG": 1.0, "KGS": 1.0, "KGM": 1.0, "KILO": 1.0, "KILOS": 1.0, "KILOGRAM": 1.0, "KILOGRAMS": 1.0,
    "公斤": 1.0, "千克": 1.0, "كجم": 1.0, "كغ": 1.0,
    "LB": 0.45359237, "LBS": 0.45359237, "POUND": 0.45359237, "POUNDS": 0.45359237,
    "MT": 1000.0, "T": 1000.0, "TON": 1000.0, "TONS": 1000.0, "TONNE": 1000.0, "TONNES": 1000.0,
    "METRICTON": 1000.0, "METRICTONS": 1000.0, "吨": 1000.0, "طن": 1000.0,
}


def _clean(value) -> str | None:
    """Collapsed whitespace, or None for an empty value or a form placeholder."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return None if text.strip("_-.·").strip().upper() in _BLANK else text or None


# ---------------------------------------------------------------------------
# Normalisers — one per kind of field
# ---------------------------------------------------------------------------


def norm_party(value) -> str | None:
    """Comparable company identity: name only, no legal suffix, no address, no punctuation."""
    text = _clean(value)
    if text is None:
        return None
    text = _ADDRESS_SPLIT.split(text.upper())[0]  # drop address / "on behalf of" lines
    text = _SUFFIX.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)  # keeps CJK/Arabic letters
    return re.sub(r"\s+", " ", text).strip() or None


def norm_port(value) -> str | None:
    """UN/LOCODE. "上海" / "Shanghai, China" / "CNSHA" all resolve to CNSHA."""
    text = _clean(value)
    if text is None:
        return None
    text = text.upper()
    bracketed = _LOCODE.findall(text)
    name = _LOCODE.sub(" ", text)  # the name decides; the bracketed code is a fallback
    for key in _PORT_KEYS:
        if key in name:
            return PORT_ALIASES[key]
    stripped = re.sub(r"[^A-Z0-9]", "", name)
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", stripped):  # the value was already a LOCODE
        return stripped
    if bracketed:
        return bracketed[-1]
    return stripped or None


def norm_count(value) -> int | None:
    """Digits out of any surrounding text: "2x40HC" -> 2, "12 x 20'FCL" -> 12."""
    text = _clean(value)
    if text is None:
        return None
    match = re.search(r"\d[\d,]*", text)
    return int(match.group().replace(",", "")) if match else None


def norm_weight(value) -> float | None:
    """Kilograms: "24,000 KGS" -> 24000.0, "24 MT" -> 24000.0, "52910 LBS" -> 23999.4."""
    text = _clean(value)
    if text is None:
        return None
    text = text.upper().replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        return None
    unit = re.sub(r"[^A-Z一-鿿؀-ۿ]", "", text[match.end():])
    return float(match.group()) * _UNIT_TO_KG.get(unit, 1.0)


NORMALISERS = {
    "shipper": norm_party,
    "consignee": norm_party,
    "notify_party": norm_party,
    "port_of_loading": norm_port,
    "port_of_discharge": norm_port,
    "container_count": norm_count,
    "gross_weight_kg": norm_weight,
}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _equal(field: str, si_value, bl_value) -> bool:
    if field == "gross_weight_kg":
        return abs(si_value - bl_value) <= max(WEIGHT_TOLERANCE_KG, 1e-4 * max(si_value, bl_value))
    return si_value == bl_value


def compare_field(field: str, si: Extraction, bl: Extraction) -> Reason:
    """One field's outcome. SI is the reference side."""
    si_value, bl_value = si.get("value"), bl.get("value")
    if si_value is None or bl_value is None:
        # Something was on the page but unusable (a "TBA", a blank rule) vs nothing at all.
        saw_something = (si_value is None and si.get("original_text")) or (
            bl_value is None and bl.get("original_text")
        )
        return Reason.UNREADABLE if saw_something else Reason.MISSING
    # The documents' own words first, so normalisation only ever rescues a match.
    if str(si.get("original_text") or si_value).strip() == str(bl.get("original_text") or bl_value).strip():
        return Reason.MATCHED
    normalise = NORMALISERS[field]
    si_norm, bl_norm = normalise(si_value), normalise(bl_value)
    if si_norm is None or bl_norm is None:
        return Reason.UNREADABLE
    return Reason.MATCHED_AFTER_NORMALISATION if _equal(field, si_norm, bl_norm) else Reason.MISMATCHED


def compare(si: ExtractedDocument, bl: ExtractedDocument) -> Comparison:
    """A reason code for each of the 7 compared fields."""
    return {f: compare_field(f, si.get(f) or {}, bl.get(f) or {}) for f in COMPARED_FIELDS}
