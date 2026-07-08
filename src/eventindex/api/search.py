"""Agent search (§9, redefined 2026-07-06): a mini model parses the natural
query into HARD filters (set logic in SQL - exclusions and constraints are
guarantees, never similarity); the residual vibe terms only RANK within the
allowed set. No embeddings on the constraint path.
"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eventindex import config, llm

VIENNA = ZoneInfo(config.TIMEZONE)


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_dt: str | None = Field(description="ISO datetime, start of wanted window")
    to_dt: str | None = Field(description="ISO datetime, end of wanted window")

    @field_validator("from_dt", "to_dt")
    @classmethod
    def _valid_window(cls, v: str | None) -> str | None:
        """LLM output must not reach SQL unvalidated: parse, and pin naive
        datetimes to Vienna (the model thinks in local time; the session
        timezone must never decide what 'heute abend' means)."""
        if v is None:
            return None
        dt = datetime.fromisoformat(v)  # ValueError -> llm.complete retries
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VIENNA)
        return dt.isoformat()
    categories: list[str] | None = Field(description="wanted categories from the taxonomy, null = any")
    exclude_categories: list[str] = Field(description="categories the user does NOT want - hard guarantee")
    exclude_terms: list[str] = Field(description="words that must NOT appear in title/tags - hard guarantee")
    age_min: int | None
    age_max: int | None
    gender_split_min: float | None = Field(
        description="0=all male..1=all female; 'mostly women'/'at least half "
        "women' -> 0.5; null unless the query implies audience gender mix"
    )
    max_price: float | None
    is_free: bool | None
    kid_friendly: bool | None
    newcomer_friendly: bool | None
    outdoor: bool | None
    energy: Literal["low", "medium", "high"] | None
    language: Literal["de", "en"] | None
    vibe_terms: list[str] = Field(
        description="residual descriptive words for RANKING only (e.g. 'dance', "
        "'high energy' -> ['dance','energetic']); never constraints"
    )


def parse_query(tx, q: str, now: datetime | None = None) -> SearchFilters:
    now = (now or datetime.now(VIENNA)).astimezone(VIENNA)
    return llm.complete(
        tx,
        f"Parse this event-search query into filters. Now: {now:%A %Y-%m-%d %H:%M} "
        f"(Europe/Vienna).\nTaxonomy: {', '.join(config.CATEGORIES)}\n"
        'Time words: "heute abend"/"tonight" = today 17:00-23:59; "morgen '
        'abend" = tomorrow 17:00-23:59; "am wochenende" = next Sat 00:00 - Sun '
        "23:59. No time mentioned = from now, no end.\n"
        "Only set a filter the query actually implies; everything else null/empty. "
        "Negations (nicht/kein/ohne X) go to exclude_*.\n\n"
        f"QUERY: {q}",
        SearchFilters,
    )


def build_sql(f: SearchFilters) -> tuple[str, dict]:
    """Hard filters -> WHERE conditions. null attribute = unknown = never
    matches a constraint (§7 contract)."""
    conditions = ["o.status = 'scheduled'"]
    params: dict = {}
    conditions.append("o.starts_at >= %(from)s")
    params["from"] = (
        datetime.fromisoformat(f.from_dt) if f.from_dt else datetime.now(VIENNA)
    )
    if f.to_dt:
        conditions.append("o.starts_at <= %(to)s")
        params["to"] = datetime.fromisoformat(f.to_dt)
    if f.categories:
        conditions.append("e.category && %(cats)s")
        params["cats"] = f.categories
    if f.exclude_categories:
        conditions.append("NOT (e.category && %(not_cats)s)")
        params["not_cats"] = f.exclude_categories
    for i, term in enumerate(f.exclude_terms):
        key = f"not_term_{i}"
        # coalesce: an unenriched event (inferred IS NULL) must be judged by
        # its title alone, not NULL-poisoned out of every negation query
        conditions.append(
            f"NOT (e.title ILIKE %({key})s "
            f"OR coalesce(e.inferred->'vibe_tags' @> to_jsonb(lower(%({key}raw)s)::text), false))"
        )
        params[key] = f"%{term}%"
        params[key + "raw"] = term
    if f.age_min is not None and f.age_max is not None:
        conditions.append("e.expected_age_range && int4range(%(age_min)s, %(age_max)s, '[]')")
        params["age_min"], params["age_max"] = f.age_min, f.age_max
    if f.gender_split_min is not None:
        conditions.append("e.expected_gender_split >= %(gender_min)s")
        params["gender_min"] = f.gender_split_min
    if f.is_free:
        conditions.append("e.price_min = 0")
    elif f.max_price is not None:
        conditions.append("e.price_min <= %(max_price)s")
        params["max_price"] = f.max_price
    for attr in ("kid_friendly", "newcomer_friendly", "outdoor"):
        want = getattr(f, attr)
        if want is not None:
            conditions.append(
                f"(e.inferred->'{attr}'->>'value')::bool = %({attr})s"
            )
            params[attr] = want
    if f.energy:
        conditions.append("e.inferred->>'energy' = %(energy)s")
        params["energy"] = f.energy
    if f.language:
        conditions.append("e.inferred->>'language' = %(language)s")
        params["language"] = f.language
    return " AND ".join(conditions), params


def rank(rows: list[dict], vibe_terms: list[str]) -> list[dict]:
    """Rank WITHIN the allowed set: vibe-term overlap × effective confidence.
    Pure text overlap for now; embeddings may later refine this - never the
    constraints."""
    terms = [t.lower() for t in vibe_terms]

    def score(row) -> float:
        if not terms:
            return row["confidence"] or 0.0
        haystack = " ".join([
            (row["title"] or "").lower(),
            " ".join(row.get("vibe_tags") or []),
            " ".join(row.get("category") or []),
        ])
        hits = sum(1 for t in terms if t in haystack)
        return (0.5 + hits / len(terms)) * (row["confidence"] or 0.0)

    return sorted(rows, key=score, reverse=True)
