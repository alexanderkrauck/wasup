"""Recipe-based universal crawler (§5b): humans write zero per-site code.

A recipe is declarative data (synthesized by the onboarding agent, executed
here). The pagination taxonomy is a deliberately closed set - variability
lives in recipes, not in code. Selector-free recipes fall back to the
extraction cascade (JSON-LD -> LLM), so selectors are an optimization,
never a dependency.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventindex import config

log = logging.getLogger("eventindex.recipe")

# per-crawl hard caps (§cost-governance ring 1), recipe values are clamped.
# Deliberately generous (completeness > thrift; per-source budgets are the
# real governor): pagination depth must never be why we miss events.
# 400: a portal whose recurring events occupy one row per date needs
# ~250-350 pages for its full calendar in chunked date windows
# (linztermine caps any single query at ~1134 rows, 2026-07-11); dailies
# stop early via all_fingerprints_seen.
MAX_PAGES_HARD = 400
MAX_DETAIL_FETCHES = 60
PAGINATION_TYPES = Literal[
    "url_param", "next_link", "next_click", "load_more_click",
    "infinite_scroll", "calendar_nav", "date_range_param", "form_post", "none",
]


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: PAGINATION_TYPES
    param: str | None = Field(None, description="url_param: query param name")
    start: int = 1
    max_pages: int = 10
    next_selector: str | None = Field(
        None, description="next_link: CSS for the next <a>. next_click: CSS "
        "for a next-page control that swaps the list in place via JS "
        "(JSF/PrimeFaces paginators, href='#' + onclick) - every page state "
        "is harvested, so it also covers plain load-more/append UIs.")
    click_selector: str | None = Field(None, description="load_more_click: CSS for the button")
    months_ahead: int = Field(
        3, description="date/calendar template horizon in months. Cover the "
        "site's WHOLE published horizon (an open-ended range - empty to= - "
        "is better than many windows when the site allows it).")
    date_format: str | None = Field(
        None, description="strftime for {from}/{to}, e.g. '%d.%m.%Y' when the "
        "site wants German dates. Default ISO (%Y-%m-%d).")
    chunk_days: int | None = Field(
        None, description="split the {from}/{to} horizon into windows of N "
        "days (one URL each) for sites that cap results per query")


# completeness bound: index EVERYTHING a site publishes, up to 5 years out
# (Alexander 2026-07-10) - the real governors are MAX_PAGES_HARD + budgets,
# never an arbitrary calendar cutoff
MAX_MONTHS_AHEAD = 60


def _coerce_list(v):
    """LLM tool-call quirk: some models wrap array params as
    {'item': [...]} - even NESTED ({'item': {'item': [...]}}) - or send a
    lone string. Unwrap instead of burning whole onboarding sessions on
    schema retries (prod sessions failed on both shapes, 2026-07-11)."""
    while isinstance(v, dict) and len(v) == 1:
        v = next(iter(v.values()))
    if isinstance(v, str):
        return [v]
    return v


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_items: int = 3
    required_fields: list[str] = ["title", "starts_at"]
    date_parse_rate: float = 0.9

    _lists = field_validator("required_fields", mode="before")(_coerce_list)


class Recipe(BaseModel):
    """source.recipe - versioned, regenerable, never hand-written."""

    model_config = ConfigDict(extra="forbid")
    version: int = 1
    entry_urls: list[str] = Field(
        description="templates; {n}=page, {year}/{month}=calendar, {from}/{to}"
        "=dates (pagination.date_format, default ISO). Date templates expand "
        "for every pagination type. Plain URL list is the universal fallback (H3.3)."
    )
    render: Literal["http", "headless"] = "http"
    pagination: Pagination
    item_scope: str | None = Field(None, description="CSS narrowing what the extractor sees")
    field_selectors: dict[str, str] | None = Field(
        None,
        description="field -> CSS; append @attr for attributes, e.g. 'a@href'. "
        "If unstable, omit - extraction falls back to the LLM cascade.",
    )
    follow_detail: bool = False
    detail_url_selector: str | None = Field(None, description="CSS for the detail link")
    image_selector: str | None = Field(
        None,
        description="CSS for <img> elements that ARE the events (posters/"
        "flyers/program images). Only when event data exists as images the "
        "text extractors cannot see; each new image costs one vision call.",
    )
    stop_conditions: list[Literal["date_older_than_now", "all_fingerprints_seen"]] = []
    validation: Validation = Validation()

    _lists = field_validator("entry_urls", "stop_conditions", mode="before")(_coerce_list)

    @model_validator(mode="after")
    def _interactive_needs_headless(self):
        # clicking/scrolling only exists in a browser; an agent that forgets
        # render='headless' must not produce a recipe that silently no-ops
        if self.pagination.type in ("next_click", "load_more_click", "infinite_scroll"):
            self.render = "headless"
        return self


class ValidationResult(BaseModel):
    ok: bool
    items: int
    reasons: list[str]
    # set when a hard limit (page cap, state cap) cut the crawl short while
    # more content was demonstrably available - events are being MISSED and
    # that must never stay silent (Alexander 2026-07-10)
    truncated: str | None = None
    # pages/states actually fetched: coverage judgments must extrapolate
    # from measurement, never from the recipe's plan (a dead next_selector
    # fetches 1 state however large max_pages is)
    pages: int = 0
    # a next_click whose click changed nothing - dead control
    pagination_noop: str | None = None


# ------------------------------------------------------------- pagination

def _expand_dates(entry: str, p: Pagination, now: datetime) -> list[str]:
    """Fill {year}/{month} or {from}/{to} templates -> 1+ concrete URLs.
    Orthogonal to the pagination mechanics: a next_click or url_param recipe
    may also carry date windows (chunk_days splits the horizon for sites
    that cap results per query)."""
    months = min(p.months_ahead, MAX_MONTHS_AHEAD)
    if "{year}" in entry or "{month}" in entry:
        urls, (y, m) = [], (now.year, now.month)
        for _ in range(months):
            urls.append(entry.replace("{year}", str(y)).replace("{month}", f"{m:02d}"))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return urls
    if "{from}" in entry or "{to}" in entry:
        fmt = p.date_format or "%Y-%m-%d"
        horizon = 31 * months
        step = min(p.chunk_days or horizon, horizon)
        urls, day = [], 0
        while day < horizon:
            frm = (now + timedelta(days=day)).strftime(fmt)
            to = (now + timedelta(days=min(day + step - 1, horizon))).strftime(fmt)
            urls.append(entry.replace("{from}", frm).replace("{to}", to))
            day += step
        return urls
    return [entry]


def page_urls(recipe: Recipe, now: datetime | None = None) -> list[str]:
    """Expand entry_urls + pagination into the concrete URL sequence.
    Interactive types (next_click, load_more_click, infinite_scroll) return
    their (date-expanded) entry urls; the state expansion happens in the
    headless fetcher."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    p = recipe.pagination
    max_pages = min(p.max_pages, MAX_PAGES_HARD)
    urls: list[str] = []
    for entry in recipe.entry_urls:
        for dated in _expand_dates(entry, p, now):
            if p.type == "url_param" and "{n}" in dated:
                urls += [dated.replace("{n}", str(n))
                         for n in range(p.start, p.start + max_pages)]
            else:  # next_link (followed dynamically), none, interactive types
                urls.append(dated)
    return urls[: MAX_PAGES_HARD]


def _select_attr(node, selector: str):
    """CSS with optional @attr suffix: 'h3 a@href' -> href of first match."""
    attr = None
    if "@" in selector:
        selector, attr = selector.rsplit("@", 1)
    found = node.select_one(selector.strip()) if selector.strip() else node
    if found is None:
        return None
    if attr:
        return found.get(attr)
    text = found.get_text(" ", strip=True)
    return text or None


def extract_with_selectors(recipe: Recipe, html: bytes, base_url: str) -> list[dict]:
    """Free extraction when the recipe carries stable selectors."""
    from eventindex.extract import field

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(recipe.item_scope) if recipe.item_scope else [soup]
    payloads = []
    for item in items:
        fields = {}
        for name, selector in (recipe.field_selectors or {}).items():
            value = _select_attr(item, selector)
            if value is None:
                continue
            if name.endswith("_url") or name == "url":
                value = urljoin(base_url, value)
            fields[name] = value
        if fields.get("title") and fields.get("starts_at"):
            payloads.append({k: field(v, 0.85) for k, v in fields.items()})
    return payloads


def detail_urls(recipe: Recipe, html: bytes, base_url: str) -> list[str]:
    if not recipe.detail_url_selector:
        return []
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.select(recipe.item_scope) if recipe.item_scope else [soup]
    urls = []
    for item in scope:
        if (u := _select_attr(item, recipe.detail_url_selector)) is not None:
            full = urljoin(base_url, u)
            if full not in urls:
                urls.append(full)
    return urls[:MAX_DETAIL_FETCHES]


def next_url(recipe: Recipe, html: bytes, base_url: str) -> str | None:
    if recipe.pagination.type != "next_link" or not recipe.pagination.next_selector:
        return None
    soup = BeautifulSoup(html, "html.parser")
    if (u := _select_attr(soup, recipe.pagination.next_selector)) is not None:
        return urljoin(base_url, u)
    return None


# ------------------------------------------------------------- validation

def validate(recipe: Recipe, payloads: list[dict]) -> ValidationResult:
    """The contract that detects breakage (§5b self-healing)."""
    from eventindex.extract import parse_dt

    v = recipe.validation
    reasons = []
    if len(payloads) < v.min_items:
        reasons.append(f"items {len(payloads)} < min_items {v.min_items}")
    if payloads:
        for f in v.required_fields:
            have = sum(1 for p in payloads if p.get(f, {}).get("value"))
            if have / len(payloads) < 0.8:
                reasons.append(f"required field '{f}' present in only {have}/{len(payloads)}")
        with_dates = [p for p in payloads if "starts_at" in p]
        if with_dates:
            parsed = sum(
                1 for p in with_dates if parse_dt(p["starts_at"]["value"]) is not None
            )
            rate = parsed / len(with_dates)
            if rate < v.date_parse_rate:
                reasons.append(f"date_parse_rate {rate:.2f} < {v.date_parse_rate}")
    return ValidationResult(ok=not reasons, items=len(payloads), reasons=reasons)


# ------------------------------------------------------------- interpreter

def run_recipe(
    recipe: Recipe,
    source: dict,
    tx,
    job_id=None,
    fetch_page: Callable[[str], bytes | None] | None = None,
    now: datetime | None = None,
    record_image_cache: bool = True,
) -> tuple[list[dict], ValidationResult]:
    """Execute a recipe -> (claim payloads, validation result).

    fetch_page is injectable for fixture replay (H3.4); the default does
    polite HTTP or headless rendering per recipe.render.
    """
    from eventindex.extract import extract as cascade_extract, parse_dt

    owns_fetcher = fetch_page is None
    if owns_fetcher:
        fetch_page = _default_fetcher(recipe)

    seen_fps = _known_fingerprints(tx, source) if (
        "all_fingerprints_seen" in recipe.stop_conditions
    ) else None

    urls = page_urls(recipe, now=now)
    visited: set[str] = set()
    queue = list(urls)
    # the pre-expanded URL list (calendar months x entry urls) is already
    # bounded by MAX_MONTHS_AHEAD; max_pages must not silently truncate it -
    # it governs dynamic next_link chains beyond the expansion. next_click
    # harvests up to max_pages STATES per url, so its budget multiplies
    # (window x states) - the old max() capped a 3-window validation at 3
    # total states and starved the coverage gate's measurement (2026-07-11)
    if recipe.pagination.type == "next_click":
        page_cap = min(len(urls) * recipe.pagination.max_pages, MAX_PAGES_HARD)
    else:
        page_cap = min(max(recipe.pagination.max_pages, len(urls)), MAX_PAGES_HARD)
    detail_budget = MAX_DETAIL_FETCHES  # per CRAWL, as the §cost-governance caps promise
    try:
        payloads, truncated, pages, image_urls = _crawl_pages(
            recipe, source, tx, job_id, fetch_page, queue, visited, page_cap,
            detail_budget, seen_fps, now, cascade_extract, parse_dt,
        )
        if image_urls:
            payloads += _vision_payloads(
                image_urls, source, tx, job_id,
                record_cache=record_image_cache,
            )
    finally:
        if owns_fetcher and (close := getattr(fetch_page, "close", None)):
            close()
    truncated = truncated or getattr(fetch_page, "truncated", None)

    # dedupe identical payloads (listing + detail double-extraction);
    # url-bearing payloads win so detail links survive the dedupe (A7)
    by_key: dict = {}
    for p in payloads:
        key = (p.get("title", {}).get("value"), p.get("starts_at", {}).get("value"))
        held = by_key.get(key)
        if held is None or (not held.get("url") and p.get("url")):
            by_key[key] = p
    unique = list(by_key.values())
    result = validate(recipe, unique)
    result.truncated = truncated
    result.pages = pages
    result.pagination_noop = getattr(fetch_page, "pagination_noop", None)
    return unique, result


def _crawl_pages(recipe, source, tx, job_id, fetch_page, queue, visited,
                 page_cap, detail_budget, seen_fps, now, cascade_extract,
                 parse_dt) -> tuple[list[dict], str | None, int, list[str]]:
    payloads: list[dict] = []
    truncated: str | None = None
    pages = 0
    image_urls: list[str] = []
    seen_streak = 0  # all_fingerprints_seen needs sustained evidence
    while queue and pages < page_cap:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        fetched = fetch_page(url)
        if fetched is None:
            continue
        # a next_click fetch returns one HTML per harvested page state; each
        # state counts against page_cap like a fetched page
        states = fetched if isinstance(fetched, list) else [fetched]
        if len(states) > page_cap - pages:
            truncated = (f"page cap {page_cap} dropped "
                         f"{len(states) - (page_cap - pages)} harvested states")

        skip_url = False
        for html in states[: max(page_cap - pages, 0)]:
            pages += 1

            if recipe.field_selectors:
                page_payloads = extract_with_selectors(recipe, html, url)
            else:
                _, page_payloads = cascade_extract(
                    source, _FakeResult(html, url), tx, job_id=job_id
                )
            if recipe.image_selector:
                for src in _page_image_urls(recipe, html, url):
                    if src not in image_urls:
                        image_urls.append(src)

            if recipe.follow_detail:
                for durl in detail_urls(recipe, html, url)[:detail_budget]:
                    if durl in visited:
                        continue
                    visited.add(durl)
                    detail_budget -= 1
                    dhtml = fetch_page(durl)
                    if dhtml is None:
                        continue
                    _, detail_payloads = cascade_extract(
                        source, _FakeResult(dhtml, durl), tx, job_id=job_id
                    )
                    for p in detail_payloads:
                        # the fetched detail page IS the event's url; the
                        # extractors can't see it (audit A7)
                        p.setdefault("url", {"value": durl, "confidence": 0.6})
                    page_payloads += detail_payloads

            payloads += page_payloads

            # stop conditions: they end THIS listing walk, never the whole
            # crawl - a chunked-window recipe has more windows in the queue
            # whose content the current one says nothing about (2026-07-11)
            if "date_older_than_now" in recipe.stop_conditions and page_payloads:
                dates = [
                    parse_dt(p["starts_at"]["value"]) for p in page_payloads if "starts_at" in p
                ]
                dates = [d for d in dates if d]
                if dates and max(dates) < (now or datetime.now()).astimezone():
                    skip_url = True
                    break
            if seen_fps is not None and page_payloads:
                # one fully-known page is NOT proof the rest is known: a
                # history of shallow crawls leaves page 1 claimed and pages
                # 2+ virgin (a 75-page walk was discarded over this,
                # 2026-07-11). Demand a sustained streak.
                if _all_seen(page_payloads, seen_fps, source):
                    seen_streak += 1
                    if seen_streak >= 3:
                        skip_url = True
                        break
                else:
                    seen_streak = 0
            if (nxt := next_url(recipe, html, url)) is not None:
                queue.append(nxt)
        if skip_url:
            seen_streak = 0
            continue

    if queue and any(u not in visited for u in queue):
        # the loop ended on page_cap, not on an empty queue: pre-expanded
        # URLs (date windows, calendar months) were never fetched
        truncated = truncated or (
            f"page cap {page_cap} hit with {len(queue)} queued urls unfetched")
    return payloads, truncated, pages, image_urls


# ------------------------------------------------- vision (fence 2026-07-20)

MAX_NEW_IMAGES_PER_CRAWL = 5  # cost ring: each new image is one vision call
IMAGE_CACHE_CAP = 50


def _page_image_urls(recipe: Recipe, html: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.select(recipe.image_selector)[:20]:
        src = img.get("src") or img.get("data-src")
        if src and not src.startswith("data:"):
            urls.append(urljoin(base_url, src))
    return urls


def _vision_payloads(image_urls: list[str], source: dict, tx, job_id,
                     record_cache: bool = True) -> list[dict]:
    """Fetch poster images and vision-extract the ones not seen before.
    The sha cache makes a daily crawl of an unchanged poster free; birth
    validation passes record_cache=False so its sightings never rob the
    first real crawl of its claims."""
    import hashlib
    import time

    import httpx

    from eventindex.extract.vision import MAX_IMAGE_BYTES, extract_image

    seen = dict(((source.get("extraction_hint") or {}).get("image_seen") or {}))
    payloads, processed = [], 0
    for url in image_urls:
        if processed >= MAX_NEW_IMAGES_PER_CRAWL:
            break
        try:
            time.sleep(config.CRAWL_DELAY_S)
            resp = httpx.get(url, timeout=30, follow_redirects=True,
                             headers={"User-Agent": config.USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("poster fetch failed %s: %s", url, e)
            continue
        if len(resp.content) > MAX_IMAGE_BYTES:
            continue
        sha = hashlib.sha256(resp.content).hexdigest()
        if sha in seen:
            continue
        processed += 1
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        payloads += extract_image(tx, resp.content, mime, source, job_id=job_id)
        seen[sha] = 1
    if record_cache and processed:
        from psycopg.types.json import Jsonb

        capped = dict(list(seen.items())[-IMAGE_CACHE_CAP:])
        tx.execute(
            "UPDATE source SET extraction_hint = coalesce(extraction_hint, "
            "'{}'::jsonb) || jsonb_build_object('image_seen', %s::jsonb) "
            "WHERE id = %s",
            (Jsonb(capped), source["id"]),
        )
    return payloads


class _FakeResult:
    """Duck-typed fetch.FetchResult for the extraction cascade."""

    def __init__(self, content: bytes, url: str):
        self.content = content
        self.url = url
        self.content_type = "text/html"


def _default_fetcher(recipe: Recipe) -> Callable[[str], bytes | None]:
    import time

    import httpx

    if recipe.render == "headless":
        from eventindex.fetch.headless import render_page, render_states

        if recipe.pagination.type == "next_click":
            max_states = min(recipe.pagination.max_pages, MAX_PAGES_HARD)

            def fetch_next_click(url: str) -> list[bytes] | None:
                time.sleep(config.CRAWL_DELAY_S)
                result = render_states(
                    url, recipe.pagination.next_selector, max_states=max_states
                )
                if result is None:
                    return None
                states, reason = result
                if reason == "cap":
                    fetch_next_click.truncated = (
                        f"state cap {max_states} hit at {url} with the next "
                        "control still active")
                elif reason == "noop":
                    fetch_next_click.pagination_noop = (
                        f"clicking {recipe.pagination.next_selector!r} at {url} "
                        f"changed nothing after state {len(states)}")
                return states

            fetch_next_click.truncated = None
            fetch_next_click.pagination_noop = None
            return fetch_next_click

        def fetch_headless(url: str) -> bytes | None:
            time.sleep(config.CRAWL_DELAY_S)
            return render_page(
                url,
                click_selector=recipe.pagination.click_selector
                if recipe.pagination.type == "load_more_click" else None,
                scroll=recipe.pagination.type == "infinite_scroll",
            )

        return fetch_headless

    client = httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": config.USER_AGENT}
    )

    def fetch_http(url: str) -> bytes | None:
        time.sleep(config.CRAWL_DELAY_S)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            log.warning("recipe fetch failed %s: %s", url, e)
            return None

    fetch_http.close = client.close  # run_recipe closes the pool when done
    return fetch_http


def _known_fingerprints(tx, source) -> set[str]:
    return {
        r["fingerprint"] for r in tx.execute(
            "SELECT DISTINCT fingerprint FROM event_claim WHERE source_id = %s",
            (source["id"],),
        )
    }


def _all_seen(payloads: list[dict], seen: set[str], source: dict) -> bool:
    from eventindex.extract import parse_dt
    from eventindex.resolve.fingerprint import fingerprint

    checked = 0
    for p in payloads:
        starts = parse_dt(p.get("starts_at", {}).get("value"))
        title = p.get("title", {}).get("value")
        if not title or starts is None:
            continue
        checked += 1
        fp = fingerprint(title, starts, lat=source.get("lat"), lon=source.get("lon"))
        if fp not in seen:
            return False
    # a page of unparseable garbage is NOT evidence that everything was seen -
    # stopping here is how a layout change silently truncates the crawl
    return checked > 0
