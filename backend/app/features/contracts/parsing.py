"""Document intake, classification and text extraction — F3 sub-features 1-3.

Three stages, in a deliberate order:

1. **Safety** — a contract is an untrusted file from the company under review. It is
   checked before any parser touches it.
2. **Classification** — is there a usable text layer? Native extraction is exact and
   free; OCR is lossy and slow. Running OCR on a digital PDF wastes both, and
   *skipping* it on a scan silently yields an empty contract.
3. **Extraction with coordinates** — every block of text keeps its page number and
   bounding box. This is the whole reason Feature 3 is hard: extracting a number is
   easy, proving which page it came from is not, and core_resoruces.md requires the
   verifier to re-fetch the cited span rather than trust a generated citation.

Text is never merged into one blob. Page and offset are carried through so a
citation can be reconstructed deterministically.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.events import EventKind, Severity, emit

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGES = 300
# Below this many characters per page, a "digital" PDF is really a scan with a
# thin text layer (a cover sheet, a stamp) and must still go through OCR.
MIN_CHARS_PER_PAGE_FOR_DIGITAL = 120
OCR_DPI = 200


class DocumentError(ValueError):
    """The document was rejected before or during parsing."""


@dataclass
class TextBlock:
    """One block of text with everything needed to cite it."""

    page: int                      # 1-indexed, as a human would refer to it
    text: str
    bbox: tuple[float, float, float, float] | None = None
    # Character offset within the page's full text, for span citations.
    start: int = 0
    end: int = 0
    source: str = "native"         # native | ocr

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "start": self.start,
            "end": self.end,
            "source": self.source,
        }


@dataclass
class ParsedDocument:
    page_count: int = 0
    blocks: list[TextBlock] = field(default_factory=list)
    # Full text per page, 1-indexed. The authority for span offsets.
    page_text: dict[int, str] = field(default_factory=dict)
    is_scanned: bool = False
    ocr_applied: bool = False
    ocr_confidence: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(self.page_text[p] for p in sorted(self.page_text))

    @property
    def total_chars(self) -> int:
        return sum(len(text) for text in self.page_text.values())

    @property
    def is_usable(self) -> bool:
        """Whether there is enough text to attempt extraction at all.

        A document that fails this is routed to human review rather than being
        extracted into a contract worth zero — spec §18 lists an unreadable scan as
        a case the system must handle safely.
        """
        return self.total_chars >= 200


# ---------------------------------------------------------------------------
# 1. Safe intake
# ---------------------------------------------------------------------------


def check_document_safety(content: bytes, filename: str) -> None:
    """OWASP file-upload checks before any parser sees the bytes."""
    if not content:
        raise DocumentError("the document is empty")
    if len(content) > MAX_PDF_BYTES:
        raise DocumentError(
            f"document is {len(content) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_PDF_BYTES // 1_048_576} MB"
        )

    # Content sniffing: the extension is a claim, the magic bytes are evidence.
    if content.startswith(b"%PDF"):
        return
    if content.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx"):
        return
    raise DocumentError(
        f"{filename!r} is not a PDF or DOCX; its content begins with "
        f"{content[:8]!r}"
    )


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------


def classify(content: bytes) -> dict[str, Any]:
    """Decide whether a PDF has a usable text layer, without fully parsing it."""
    import pymupdf

    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise DocumentError(f"could not open the document: {exc}") from exc

    try:
        if document.page_count == 0:
            raise DocumentError("the document has no pages")
        if document.page_count > MAX_PAGES:
            raise DocumentError(
                f"document has {document.page_count} pages; the limit is {MAX_PAGES}"
            )

        # Sample rather than read everything: classification only needs a signal.
        sample = min(document.page_count, 5)
        chars = sum(len(document[i].get_text().strip()) for i in range(sample))
        per_page = chars / sample

        return {
            "page_count": document.page_count,
            "chars_per_page": round(per_page, 1),
            "is_scanned": per_page < MIN_CHARS_PER_PAGE_FOR_DIGITAL,
            "encrypted": document.is_encrypted,
        }
    finally:
        document.close()


# ---------------------------------------------------------------------------
# 3. Extraction
# ---------------------------------------------------------------------------


def parse_native(content: bytes) -> ParsedDocument:
    """Extract text blocks with page numbers and bounding boxes."""
    import pymupdf

    parsed = ParsedDocument()
    document = pymupdf.open(stream=content, filetype="pdf")
    try:
        parsed.page_count = document.page_count
        for index in range(document.page_count):
            page = document[index]
            page_number = index + 1
            page_text_parts: list[str] = []
            offset = 0

            # `sort=True` returns blocks in reading order rather than creation
            # order, which matters for multi-column contract layouts.
            for block in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, *_ = block
                text = (text or "").strip()
                if not text:
                    continue
                parsed.blocks.append(
                    TextBlock(
                        page=page_number,
                        text=text,
                        bbox=(x0, y0, x1, y1),
                        start=offset,
                        end=offset + len(text),
                        source="native",
                    )
                )
                page_text_parts.append(text)
                offset += len(text) + 1  # +1 for the newline joiner below

            parsed.page_text[page_number] = "\n".join(page_text_parts)
    finally:
        document.close()
    return parsed


def parse_with_ocr(content: bytes, *, dpi: int = OCR_DPI) -> ParsedDocument:
    """OCR every page, preserving page numbers and word-level boxes.

    Tesseract is the free local fallback. core_resoruces.md ranks Google Document AI
    higher for layout-aware citation, and that remains the upgrade path — but it
    needs a billable Cloud project, and an OCR path that only works with a paid
    credential is not a fallback at all.
    """
    import pymupdf
    import pytesseract
    from PIL import Image

    parsed = ParsedDocument(is_scanned=True, ocr_applied=True)
    document = pymupdf.open(stream=content, filetype="pdf")
    confidences: list[float] = []

    try:
        parsed.page_count = document.page_count
        for index in range(document.page_count):
            page_number = index + 1
            pixmap = document[index].get_pixmap(dpi=dpi)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))

            # `image_to_data` gives per-word boxes and confidences, which
            # `image_to_string` discards — and boxes are what make a citation
            # verifiable rather than merely plausible.
            data = pytesseract.image_to_data(
                image, output_type=pytesseract.Output.DICT
            )

            lines: dict[tuple[int, int, int], list[int]] = {}
            for i, word in enumerate(data["text"]):
                if not word.strip():
                    continue
                try:
                    confidence = float(data["conf"][i])
                except (ValueError, TypeError):
                    confidence = -1.0
                if confidence >= 0:
                    confidences.append(confidence)
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                lines.setdefault(key, []).append(i)

            page_parts: list[str] = []
            offset = 0
            # Scale OCR pixel coordinates back to PDF points so boxes from the OCR
            # and native paths mean the same thing to the citation verifier.
            scale = 72.0 / dpi

            for key in sorted(lines):
                indices = lines[key]
                text = " ".join(data["text"][i].strip() for i in indices).strip()
                if not text:
                    continue
                x0 = min(data["left"][i] for i in indices) * scale
                y0 = min(data["top"][i] for i in indices) * scale
                x1 = max(data["left"][i] + data["width"][i] for i in indices) * scale
                y1 = max(data["top"][i] + data["height"][i] for i in indices) * scale

                parsed.blocks.append(
                    TextBlock(
                        page=page_number,
                        text=text,
                        bbox=(x0, y0, x1, y1),
                        start=offset,
                        end=offset + len(text),
                        source="ocr",
                    )
                )
                page_parts.append(text)
                offset += len(text) + 1

            parsed.page_text[page_number] = "\n".join(page_parts)
    finally:
        document.close()

    parsed.ocr_confidence = (
        round(sum(confidences) / len(confidences), 1) if confidences else None
    )
    return parsed


def parse_document(
    content: bytes, filename: str, *, workspace_id: str = "_system"
) -> ParsedDocument:
    """Full intake: safety → classification → native or OCR extraction.

    Native extraction is attempted first even on a document classified as scanned,
    because the classifier samples only the first pages; a contract with a scanned
    cover and digital body should not lose its digital text.
    """
    check_document_safety(content, filename)
    info = classify(content)

    if info["encrypted"]:
        raise DocumentError(
            f"{filename!r} is encrypted; it must be decrypted before review"
        )

    parsed = parse_native(content)
    parsed.is_scanned = info["is_scanned"]

    if parsed.is_usable and not info["is_scanned"]:
        emit(
            EventKind.RULE,
            f"{filename}: digital PDF, {parsed.page_count} pages, "
            f"{parsed.total_chars} characters — no OCR needed",
            workspace_id=workspace_id,
            feature=3,
            severity=Severity.DEBUG,
        )
        return parsed

    emit(
        EventKind.AGENT_STEP,
        f"OCR Validation Agent: {filename} has {info['chars_per_page']} chars/page — "
        f"routing to OCR",
        workspace_id=workspace_id,
        feature=3,
        severity=Severity.INFO,
    )
    try:
        ocr_parsed = parse_with_ocr(content)
    except Exception as exc:
        # An OCR failure leaves whatever native text exists, plus a warning. The
        # document is then judged unusable and sent to review rather than being
        # extracted into a contract with invented or missing terms.
        parsed.warnings.append(f"OCR failed: {type(exc).__name__}: {exc}")
        emit(
            EventKind.ERROR,
            f"OCR failed for {filename}: {exc}",
            workspace_id=workspace_id,
            feature=3,
            severity=Severity.WARNING,
        )
        return parsed

    # Keep whichever pass actually recovered more text.
    if ocr_parsed.total_chars > parsed.total_chars:
        ocr_parsed.warnings = parsed.warnings
        emit(
            EventKind.RESULT,
            f"OCR recovered {ocr_parsed.total_chars} characters from {filename} "
            f"(confidence {ocr_parsed.ocr_confidence})",
            workspace_id=workspace_id,
            feature=3,
            severity=Severity.SUCCESS,
        )
        return ocr_parsed

    parsed.warnings.append("OCR produced no more text than native extraction")
    return parsed


# ---------------------------------------------------------------------------
# Clause segmentation — F3 sub-feature 4
# ---------------------------------------------------------------------------

# Numbered headings ("4.1 FEES"), lettered schedules, and common contract captions.
_CLAUSE_HEADING = re.compile(
    r"^\s*(?:"
    r"(?:clause\s+)?\d{1,2}(?:\.\d{1,2})*\s*[.):]?\s+[A-Z]"
    r"|schedule\s+[A-Z0-9]"
    r"|annexure\s+[A-Z0-9]"
    r"|appendix\s+[A-Z0-9]"
    r"|(?:[A-Z][A-Z\s]{4,40})$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Clause:
    """A retrievable contract passage that remembers where it came from."""

    index: int
    heading: str
    text: str
    page: int
    start: int
    end: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "heading": self.heading,
            "text": self.text,
            "page": self.page,
            "start": self.start,
            "end": self.end,
        }


def segment_clauses(parsed: ParsedDocument, *, max_chars: int = 1800) -> list[Clause]:
    """Split a document into clause-sized passages, each keeping its page.

    Segmenting on headings rather than a fixed character window keeps a fee clause
    intact. A pricing sentence split across two chunks is a pricing term the
    retriever will never surface whole.
    """
    clauses: list[Clause] = []
    index = 0

    for page in sorted(parsed.page_text):
        text = parsed.page_text[page]
        if not text.strip():
            continue

        boundaries = [match.start() for match in _CLAUSE_HEADING.finditer(text)]
        if not boundaries or boundaries[0] != 0:
            boundaries.insert(0, 0)
        boundaries.append(len(text))

        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            segment = text[start:end].strip()
            if len(segment) < 20:
                continue

            # Long clauses are windowed, but only after heading segmentation, so a
            # split happens inside a long clause rather than across two.
            for offset in range(0, len(segment), max_chars):
                piece = segment[offset : offset + max_chars]
                if len(piece) < 20:
                    continue
                heading = piece.split("\n", 1)[0][:120].strip()
                clauses.append(
                    Clause(
                        index=index,
                        heading=heading,
                        text=piece,
                        page=page,
                        start=start + offset,
                        end=start + offset + len(piece),
                    )
                )
                index += 1

    return clauses
