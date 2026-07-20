"""Vision extraction (fence fired 2026-07-20): event posters, flyers, and
page screenshots whose text never reaches the DOM. Same schema, same
validation, same budget path as the text tier - only the input modality
differs. Rides the multimodal mid model (config.MODEL_VISION)."""

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from eventindex import config, llm
from eventindex.extract.llm_text import LLMExtraction, to_payloads

MAX_IMAGE_BYTES = 4_000_000  # posters compress well; bigger is a photo dump

_PROMPT = """This image is from a Linz (Austria) event source: typically a
poster, flyer, program page, or a screenshot of an event listing. Today is
{today}.

Extract every upcoming event the image shows, following these rules:
- Only actual events with a concrete date; skip decoration and past events.
- title identifies the SPECIFIC act/program, verbatim from the image.
- starts_at/ends_at: ISO 8601; date alone (YYYY-MM-DD) when no time is shown.
  Never invent times, prices, or venues - omit unknown fields (null).
- category: one of {categories}, or null.
- confidence: your certainty (0-1) the event and date are read correctly -
  be honest about hard-to-read text.
- recurrence only if the image states a repeating pattern; copy the wording
  into as_stated. One-off events: recurrence=null.
- If the image contains no readable events, return an empty list."""


def extract_image(tx, image: bytes, mime: str, source: dict,
                  job_id=None) -> list[dict]:
    """One poster/screenshot -> claim payloads (may be empty)."""
    if not image or len(image) > MAX_IMAGE_BYTES:
        return []
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    prompt = _PROMPT.format(
        today=datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat(),
        categories=", ".join(config.CATEGORIES),
    )
    result = llm.complete(
        tx, prompt, LLMExtraction, model=config.MODEL_VISION,
        source_id=source.get("id"), job_id=job_id, images=[data_url],
    )
    return to_payloads(result)
