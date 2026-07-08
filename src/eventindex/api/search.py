"""Agent search (§9, redefined 2026-07-06/08): guarantees are set logic in
SQL; audience attributes are weighted preferences.

Two kinds of query inputs, deliberately different:
- HARD (guarantees): time window, categories, exclusions, price facts, and
  any attribute the caller marks `required`. Set logic, applied before
  ranking, null = unknown never matches.
- SOFT (preferences): inferred audience attributes (age, gender split,
  kid_friendly, ...). Every stored attribute carries a certainty; the caller
  states an importance; ranking combines them: an event contributes
  P(satisfied) = certainty if it matches, 1 - certainty if it contradicts,
  UNKNOWN_PRIOR if unenriched - averaged weighted by importance.

The ATTRIBUTES registry is the single extension point: a future inferred
attribute (e.g. for_children) = one field in enrich.Enrichment + one row here.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from eventindex import config, llm

VIENNA = ZoneInfo(config.TIMEZONE)

# P(satisfied) is anchored at the coin flip: 0.5 + c/2 on a match,
# 0.5 - c/2 on a contradiction. Unknown sits just under a weak match and
# just above a weak contradiction: strong match .9 > weak match .6 >
# unknown .45 > weak contradiction .4 > strong contradiction .1
UNKNOWN_PRIOR = 0.45
ENUM_CONFIDENCE = 0.5  # energy/language are stored without their own confidence

# the soft-queryable audience attributes; also the valid names for
# required_attributes and importance. Must stay in lockstep with ATTRIBUTES
# below (pinned by test) - note "age" here vs age_min/age_max filter fields.
SOFT_ATTRIBUTES = frozenset({
    "age", "gender_split_min", "kid_friendly", "newcomer_friendly",
    "outdoor", "energy", "language", "solo_friendly", "interaction_structure",
})


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
    include_terms: list[str] = Field(
        description="synonym set of which at least ONE must appear in "
        "title/tags (word-prefix/suffix match) - hard filter for 'I want "
        "specifically X' queries, e.g. ['lauf','run'] for running; keep "
        "empty for broad/mood queries"
    )
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
    solo_friendly: bool | None = Field(
        description="normal to attend alone; set true for 'bin allein' queries"
    )
    interaction_structure: Literal["none", "optional", "built_in"] | None = Field(
        description="built_in = the format FORCES interaction (rotation, "
        "teams, pair work) - set for shy/meet-people queries; optional = "
        "easy to talk; none = silent attendance"
    )
    energy: Literal["low", "medium", "high"] | None
    language: Literal["de", "en"] | None
    required_attributes: list[str] = Field(
        description="attribute names the user makes NON-NEGOTIABLE "
        "('unbedingt', 'muss') - enforced as hard filters instead of "
        "preferences; from: age, gender_split_min, kid_friendly, "
        "newcomer_friendly, outdoor, energy, language"
    )

    @field_validator("required_attributes")
    @classmethod
    def _known_attributes(cls, v: list[str]) -> list[str]:
        unknown = set(v) - SOFT_ATTRIBUTES
        if unknown:
            raise ValueError(
                f"unknown attributes {sorted(unknown)}; valid: {sorted(SOFT_ATTRIBUTES)}"
            )
        return v
    vibe_terms: list[str] = Field(
        description="residual descriptive words for RANKING only (e.g. 'dance', "
        "'high energy' -> ['dance','energetic']); never constraints"
    )


# SearchFilters keeps every field required-but-nullable (strict structured
# output for the internal parser). External callers of POST /v1/query send
# partial bodies; these defaults fill the gaps.
FILTER_DEFAULTS: dict = {
    "from_dt": None, "to_dt": None, "categories": None,
    "exclude_categories": [], "exclude_terms": [], "include_terms": [],
    "age_min": None,
    "age_max": None, "gender_split_min": None, "max_price": None,
    "is_free": None, "kid_friendly": None, "newcomer_friendly": None,
    "outdoor": None, "solo_friendly": None, "interaction_structure": None,
    "energy": None, "language": None,
    "required_attributes": [], "vibe_terms": [],
}

# The public body of POST /v1/query: SearchFilters with every field optional
# (derived programmatically - one source of truth) plus importance weights.
# Window/required validation still runs via SearchFilters afterwards.
QueryBody = create_model(
    "QueryBody",
    __config__=ConfigDict(extra="forbid"),
    **{name: (f.annotation, Field(FILTER_DEFAULTS[name], description=f.description))
       for name, f in SearchFilters.model_fields.items()},
    importance=(dict[str, float], Field(
        {}, description="0..1 weight per soft attribute "
        "(age, gender_split_min, kid_friendly, newcomer_friendly, outdoor, "
        "energy, language); default 1.0 each. Combined with each event's "
        "stored certainty into match_score - see /llms.txt")),
)


# ------------------------------------------------------- attribute registry

@dataclass(frozen=True)
class Attribute:
    """How one queryable audience attribute lives in the event row.

    value_sql/conf_sql are trusted constants (never user input); kind picks
    the satisfaction comparator. Adding an inferred attribute later means one
    field in enrich.Enrichment + one row here - nothing else.
    """

    kind: str  # bool | min_float | enum | age
    value_sql: str
    conf_sql: str
    hard_sql: str  # condition template used when the attribute is `required`


def _inferred_bool(key: str) -> Attribute:
    return Attribute(
        kind="bool",
        value_sql=f"(e.inferred->'{key}'->>'value')::bool",
        conf_sql=f"(e.inferred->'{key}'->>'confidence')::float",
        hard_sql=f"(e.inferred->'{key}'->>'value')::bool = %({{p}})s",
    )


ATTRIBUTES: dict[str, Attribute] = {
    "gender_split_min": Attribute(
        kind="min_float",
        value_sql="e.expected_gender_split",
        conf_sql="e.expected_gender_split_confidence",
        hard_sql="e.expected_gender_split >= %({p})s",
    ),
    "age": Attribute(
        kind="age",
        value_sql="e.expected_age_range",  # placeholder; age uses lo/hi below
        conf_sql="e.expected_age_range_confidence",
        hard_sql="e.expected_age_range && int4range(%({p}min)s, %({p}max)s, '[]')",
    ),
    "kid_friendly": _inferred_bool("kid_friendly"),
    "newcomer_friendly": _inferred_bool("newcomer_friendly"),
    "outdoor": _inferred_bool("outdoor"),
    "solo_friendly": _inferred_bool("solo_friendly"),
    "interaction_structure": Attribute(
        kind="enum", value_sql="e.inferred->>'interaction_structure'",
        conf_sql=str(ENUM_CONFIDENCE),
        hard_sql="e.inferred->>'interaction_structure' = %({p})s",
    ),
    "energy": Attribute(
        kind="enum", value_sql="e.inferred->>'energy'", conf_sql=str(ENUM_CONFIDENCE),
        hard_sql="e.inferred->>'energy' = %({p})s",
    ),
    "language": Attribute(
        kind="enum", value_sql="e.inferred->>'language'", conf_sql=str(ENUM_CONFIDENCE),
        hard_sql="e.inferred->>'language' = %({p})s",
    ),
}


def _wanted(f: SearchFilters) -> dict:
    """The attribute constraints the query actually states: name -> wanted."""
    wanted: dict = {}
    if f.age_min is not None and f.age_max is not None:
        wanted["age"] = (f.age_min, f.age_max)
    for name in ("gender_split_min", "kid_friendly", "newcomer_friendly",
                 "outdoor", "solo_friendly", "interaction_structure",
                 "energy", "language"):
        if (v := getattr(f, name)) is not None:
            wanted[name] = v
    return wanted


def attribute_select() -> str:
    """Extra SELECT columns the scorer needs (trusted constants only)."""
    cols = []
    for name, attr in ATTRIBUTES.items():
        if attr.kind == "age":
            cols += [
                "lower(e.expected_age_range) AS age__lo",
                "upper(e.expected_age_range) AS age__hi",
                f"{attr.conf_sql} AS age__conf",
            ]
        else:
            cols += [f"{attr.value_sql} AS {name}__value",
                     f"{attr.conf_sql} AS {name}__conf"]
    return ", ".join(cols)


# ---------------------------------------------------------------- parsing

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
        "Negations (nicht/kein/ohne X) go to exclude_*. When the user wants "
        "a SPECIFIC activity/thing, put its German+English synonyms in "
        "include_terms (['lauf','run'] for running); mood/vibe words go to "
        "vibe_terms instead.\n"
        "Audience attributes (age, gender_split_min, kid_friendly, ...) are "
        "soft preferences by default; add a name to required_attributes ONLY "
        "when the user is emphatic ('unbedingt', 'muss', 'nur wenn').\n\n"
        f"QUERY: {q}",
        SearchFilters,
    )


# ------------------------------------------------------------------- SQL

def build_sql(f: SearchFilters) -> tuple[str, dict]:
    """HARD conditions only: guarantees + attributes marked required.
    null attribute = unknown = never matches a hard constraint (§7)."""
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
    if f.include_terms:
        # at least one synonym must appear: word-prefix (\mterm) or compound
        # suffix (term\M) in the title, or as an exact vibe tag. Same
        # boundary semantics as the ranker: "run" != "Führung",
        # "lauf" == "Orientierungslauf"
        import re as _re

        alts = []
        for i, term in enumerate(f.include_terms):
            key = f"inc_term_{i}"
            alts.append(
                f"e.title ~* %({key})s "
                f"OR coalesce(e.inferred->'vibe_tags' @> to_jsonb(lower(%({key}raw)s)::text), false)"
            )
            params[key] = rf"\m{_re.escape(term)}|{_re.escape(term)}\M"
            params[key + "raw"] = term
        conditions.append("(" + " OR ".join(alts) + ")")
    if f.is_free:
        conditions.append("e.price_min = 0")
    elif f.max_price is not None:
        conditions.append("e.price_min <= %(max_price)s")
        params["max_price"] = f.max_price

    wanted = _wanted(f)
    for name in f.required_attributes:
        if name not in wanted or name not in ATTRIBUTES:
            continue  # required without a stated value is a no-op
        attr = ATTRIBUTES[name]
        p = f"req_{name}"
        conditions.append(attr.hard_sql.format(p=p))
        if attr.kind == "age":
            params[p + "min"], params[p + "max"] = wanted["age"]
        else:
            params[p] = wanted[name]
    return " AND ".join(conditions), params


# --------------------------------------------------------------- scoring

def _satisfaction(row: dict, name: str, want) -> float:
    """P(this event satisfies the constraint), from stored value+certainty,
    anchored at the coin flip: 0.5 + c/2 on a match, 0.5 - c/2 on a
    contradiction, UNKNOWN_PRIOR when the attribute is unknown."""
    attr = ATTRIBUTES[name]
    if attr.kind == "age":
        lo, hi = row.get("age__lo"), row.get("age__hi")
        if lo is None or hi is None:
            return UNKNOWN_PRIOR
        conf = row.get("age__conf") or ENUM_CONFIDENCE
        ok = want[0] <= hi - 1 and want[1] >= lo  # int4range upper is exclusive
    else:
        value = row.get(f"{name}__value")
        if value is None:
            return UNKNOWN_PRIOR
        raw_conf = row.get(f"{name}__conf")
        conf = float(raw_conf) if raw_conf is not None else ENUM_CONFIDENCE
        if attr.kind == "min_float":
            ok = float(value) >= want
        else:  # bool, enum
            ok = value == want
    return 0.5 + conf / 2 if ok else 0.5 - conf / 2


def preference_score(row: dict, f: SearchFilters,
                     importance: dict[str, float] | None = None) -> float:
    """Importance-weighted expected satisfaction over the SOFT constraints
    (required ones were already enforced in SQL). 1.0 when none stated."""
    importance = importance or {}
    soft = {n: w for n, w in _wanted(f).items()
            if n not in f.required_attributes}
    if not soft:
        return 1.0
    total_w = sum(importance.get(n, 1.0) for n in soft)
    if total_w <= 0:
        return 1.0
    return sum(
        importance.get(n, 1.0) * _satisfaction(row, n, want)
        for n, want in soft.items()
    ) / total_w


def rank(rows: list[dict], f: SearchFilters,
         importance: dict[str, float] | None = None) -> list[dict]:
    """Rank WITHIN the allowed set: preference score x vibe-term overlap x
    effective confidence. Nothing is dropped here - hard filtering already
    happened in SQL."""
    terms = [t.lower() for t in f.vibe_terms]

    def hit(term: str, tokens: list[str]) -> bool:
        # token prefix/suffix, not substring: "lauf" must match
        # "orientierungslauf" (German compounds) but "run" must NOT match
        # "führung"/"stadtrundfahrten"
        return any(t.startswith(term) or t.endswith(term) for t in tokens)

    def score(row) -> float:
        s = preference_score(row, f, importance) * (row["confidence"] or 0.0)
        if terms:
            tokens = " ".join([
                (row["title"] or "").lower(),
                " ".join(row.get("vibe_tags") or []),
                " ".join(row.get("category") or []),
            ]).split()
            hits = sum(1 for t in terms if hit(t, tokens))
            s *= 0.5 + hits / len(terms)
        row["match_score"] = round(s, 4)  # exposed: consumers see the weighting
        return s

    return sorted(rows, key=score, reverse=True)
