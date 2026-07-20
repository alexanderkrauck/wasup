"""Tier d: PDF text extraction (fence fired 2026-07-20).

Monthly-program PDFs are THE long-tail format (Vereine, churches,
Kulturhäuser). Text-layer PDFs feed the ordinary LLM tier; a scanned PDF
yields no text here and falls through - the extractor agent's vision path
covers it.
"""

import logging
from io import BytesIO

log = logging.getLogger("eventindex.pdf")

MAX_PAGES = 30  # a season program fits; a 300-page council annex is not events


def is_pdf(content: bytes, content_type: str = "") -> bool:
    return "pdf" in content_type.lower() or content[:5] == b"%PDF-"


def to_text(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PAGES]]
    except Exception as e:  # malformed PDFs are common; never kill a crawl
        log.warning("pdf text extraction failed: %s", e)
        return ""
    return " ".join(" ".join(pages).split())
