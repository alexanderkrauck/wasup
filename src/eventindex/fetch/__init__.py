"""Fetcher (§5): polite HTTP with content-hash early exit.

Sync httpx: the worker runs one job at a time and the per-domain rate limit
forbids concurrency within a source anyway; async returns when the scheduler
fetches many sources at once.
"""

import hashlib
import time
import urllib.robotparser
from dataclasses import dataclass

import httpx

from eventindex import config

FETCHED = "fetched"
UNCHANGED = "unchanged"
BLOCKED = "blocked"  # robots.txt disallows


@dataclass
class FetchResult:
    status: str  # fetched | unchanged | blocked
    url: str
    content: bytes = b""
    content_type: str = ""
    content_hash: str = ""
    etag: str | None = None
    last_modified: str | None = None


def _robots_allows(client: httpx.Client, url: str) -> bool:
    parsed = httpx.URL(url)
    robots_url = f"{parsed.scheme}://{parsed.host}/robots.txt"
    try:
        resp = client.get(robots_url)
    except httpx.HTTPError:
        return True
    if resp.status_code >= 400:
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp.can_fetch(config.USER_AGENT, url)


def fetch_source(source: dict) -> FetchResult:
    """One conditional GET of the source URL, honoring robots.txt on tier 2-3."""
    headers = {"User-Agent": config.USER_AGENT}
    if source.get("http_etag"):
        headers["If-None-Match"] = source["http_etag"]
    if source.get("http_last_modified"):
        headers["If-Modified-Since"] = source["http_last_modified"]

    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": config.USER_AGENT}
    ) as client:
        if source["tier"] >= 2 and not _robots_allows(client, source["url"]):
            return FetchResult(status=BLOCKED, url=source["url"])

        time.sleep(config.CRAWL_DELAY_S)  # politeness gap after the robots fetch
        resp = client.get(source["url"], headers=headers)
        if resp.status_code == 304:
            return FetchResult(status=UNCHANGED, url=source["url"])
        resp.raise_for_status()

    content_hash = hashlib.sha256(resp.content).hexdigest()
    if content_hash == source.get("last_content_hash"):
        return FetchResult(status=UNCHANGED, url=str(resp.url), content_hash=content_hash)

    return FetchResult(
        status=FETCHED,
        url=str(resp.url),
        content=resp.content,
        content_type=resp.headers.get("content-type", ""),
        content_hash=content_hash,
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
    )
