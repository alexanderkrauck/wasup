"""Job handlers: pure functions (job, tx) -> [jobs to enqueue].

Phase 0 ships only a dummy crawl proving the round-trip: worker -> crawl_log
-> budget spend -> digest. Real fetching arrives in phase 1.
"""

from pydantic import BaseModel, ConfigDict

from eventindex import config, llm
from eventindex.budget import record_spend


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


def crawl(job: dict, tx) -> list[dict]:
    if not job["payload"].get("dummy"):
        raise NotImplementedError("real crawling arrives in phase 1")
    if config.OPENROUTER_API_KEY:
        ping = llm.complete(
            tx, 'Reply with exactly {"ok": true}.', Ping, job_id=job["id"]
        )
        detail = f"dummy crawl, llm ping ok={ping.ok}"
    else:
        record_spend(
            tx, 0.0001, "other", job_id=job["id"], detail="synthetic spend, no API key"
        )
        detail = "dummy crawl, synthetic spend (OPENROUTER_API_KEY unset)"
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', 0, %s)",
        (job["id"], detail),
    )
    return []


HANDLERS = {"crawl": crawl}
