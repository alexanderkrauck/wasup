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

from pydantic import (
    BaseModel, ConfigDict, Field, create_model, field_validator,
    model_validator,
)

from eventindex import config, llm

VIENNA = ZoneInfo(config.TIMEZONE)

# the index is Linz: queries default to this circle unless the caller sends
# near=/radius= (radius="any" disables). Events with UNKNOWN location always
# pass the default gate - null = unknown must not hide half the index.
LINZ_CENTER = (48.3069, 14.2858)
DEFAULT_RADIUS_KM = 15


def _radius_m(radius: str) -> float:
    import re as _re

    m = _re.fullmatch(r"([\d.]+)\s*(km|m)?", radius.strip())
    if not m:
        raise ValueError("radius must look like '5km' or '500m' (or 'any')")
    return float(m.group(1)) * (1000 if (m.group(2) or "km") == "km" else 1)

# P(satisfied) is anchored at the coin flip: 0.5 + c/2 on a match,
# 0.5 - c/2 on a contradiction. Unknown sits just under a weak match and
# just above a weak contradiction: strong match .9 > weak match .6 >
# unknown .45 > weak contradiction .4 > strong contradiction .1
UNKNOWN_PRIOR = 0.45
ENUM_CONFIDENCE = 0.5  # legacy energy/interaction values lack own confidence

# The soft-queryable attributes and the valid names for importance. Tags share
# the same certainty-aware ranking surface even though they are stored in
# event_tag rather than ATTRIBUTES.
SOFT_ATTRIBUTES = frozenset({
    "age", "gender_split_min", "kid_friendly", "newcomer_friendly",
    "outdoor", "energy", "language", "solo_friendly", "interaction_structure",
    "sex_service_context", "price", "event_scale", "tags",
})

# Hard-required attributes are deliberately narrower. Price already has the
# exact-fact max_price/is_free contract, while tags have min_tag_match.
REQUIRED_ATTRIBUTES = SOFT_ATTRIBUTES - {"price", "tags"}


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_dt: str | None = Field(description="ISO datetime, start of wanted window")
    to_dt: str | None = Field(description="ISO datetime, end of wanted window")

    @field_validator("from_dt", "to_dt")
    @classmethod
    def _valid_window(cls, v: str | None, info) -> str | None:
        """LLM output must not reach SQL unvalidated: parse, and pin naive
        datetimes to Vienna (the model thinks in local time; the session
        timezone must never decide what 'heute abend' means). A bare date in
        to_dt means the WHOLE day - 'bis 18.7.' must not lose the 18th."""
        if v is None:
            return None
        bare_date = len(v.strip()) == 10
        dt = datetime.fromisoformat(v)  # ValueError -> llm.complete retries
        if bare_date and info.field_name == "to_dt":
            dt = dt.replace(hour=23, minute=59, second=59)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VIENNA)
        return dt.isoformat()

    categories: list[str] | None = Field(description="wanted categories from the taxonomy, null = any")
    exclude_categories: list[str] = Field(description="categories the user does NOT want - hard guarantee")

    @field_validator("categories", "exclude_categories")
    @classmethod
    def _known_categories(cls, v: list[str] | None) -> list[str] | None:
        """A typo'd category silently returned nothing (and in
        exclude_categories silently WEAKENED a guarantee) - audit B3."""
        unknown = set(v or []) - set(config.CATEGORIES)
        if unknown:
            raise ValueError(
                f"unknown categories {sorted(unknown)}; "
                f"valid: {sorted(config.CATEGORIES)}"
            )
        return v
    exclude_terms: list[str] = Field(
        description="words that must NOT appear in title/tags/venue; hard "
        "guarantee, so unknown never counts as excluded")
    name: str | None = Field(
        description="literal event-title search, including German compound "
        "suffixes: 'ball' matches 'Maturaball' but not 'Ballett'. This is a "
        "hard candidate filter. Put activities, topics, format, atmosphere, "
        "and audience concepts in tags instead"
    )
    organizer: str | None = Field(
        description="literal organizer-name substring; hard candidate filter. "
        "Example: organizer='WKO' with tags=['startup']"
    )
    venue: str | None = Field(
        description="literal venue-name substring; hard candidate filter"
    )
    source: str | None = Field(
        description="literal reporting-source name or URL substring; hard "
        "candidate filter. Example: source='WKO' with tags=['startup']"
    )
    age_min: int | None
    age_max: int | None
    gender_split_min: float | None = Field(
        description="0=all male..1=all female; 'mostly women'/'at least half "
        "women' -> 0.5; null unless the query implies audience gender mix"
    )
    max_price: float | None = Field(
        description="hard maximum stated price in EUR; events whose price is "
        "unknown or merely estimated do not match. Use preferred_max_price "
        "when the user says ideally/preferably")
    preferred_max_price: float | None = Field(
        description="soft preferred maximum price in EUR; stated and estimated "
        "prices rank by their confidence and unknown stays visible")
    is_free: bool | None = Field(
        description="true is a hard explicitly-free requirement; unknown and "
        "estimated-free events do not match")
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
    participant_count_min: int | None = Field(
        description="soft preferred minimum estimated participant count; use "
        "required_attributes=['event_scale'] only when the user says it must "
        "be at least this large")
    participant_count_max: int | None = Field(
        description="soft preferred maximum estimated participant count; use "
        "required_attributes=['event_scale'] only when the user makes it a "
        "hard limit")
    sex_service_context: bool | None = Field(
        description="event at a commercial sex establishment (Bordell, "
        "strip club, swinger club) - NOT mere 18+ nightlife. On the public "
        "query API this remains a soft preference. The ChatGPT search tool "
        "hard-excludes known true values when omitted or false; true is an "
        "explicit request to permit that context"
    )
    required_attributes: list[str] = Field(
        description="attribute names the user makes NON-NEGOTIABLE "
        "('unbedingt', 'muss') - enforced as hard filters instead of "
        "preferences; from: age, gender_split_min, kid_friendly, "
        "newcomer_friendly, outdoor, energy, language, solo_friendly, "
        "interaction_structure, sex_service_context, event_scale"
    )

    @field_validator("required_attributes")
    @classmethod
    def _known_attributes(cls, v: list[str]) -> list[str]:
        unknown = set(v) - REQUIRED_ATTRIBUTES
        if unknown:
            raise ValueError(
                f"unknown attributes {sorted(unknown)}; "
                f"valid: {sorted(REQUIRED_ATTRIBUTES)}"
            )
        return v
    tags: list[str] = Field(
        description="desired 1-3-word activity/topic/format concepts, matched "
        "multilingually against the event's confidence-bearing tags; soft "
        "ranking unless min_tag_match is set"
    )

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, values: list[str]) -> list[str]:
        from eventindex.tags import clean_desired

        return clean_desired(values)

    min_tag_match: float | None = Field(
        default=None, ge=0, le=1,
        description="explicit hard threshold for certainty-weighted semantic "
        "tag matching; null keeps tags rank-only"
    )
    near: str | None = Field(
        description="'lat,lon' center overriding the default 15km-around-"
        "Linz gate; use for 'near X' queries with known coordinates"
    )
    radius: str | None = Field(
        description="radius for near ('5km', '800m'); 'any' disables the "
        "geo gate entirely"
    )

    @model_validator(mode="after")
    def _sane_ranges(self):
        """Impossible ranges returned silently-empty results (audit B11)."""
        if (self.from_dt and self.to_dt
                and datetime.fromisoformat(self.from_dt)
                > datetime.fromisoformat(self.to_dt)):
            raise ValueError("from_dt is after to_dt")
        if (self.age_min is not None and self.age_max is not None
                and self.age_min > self.age_max):
            raise ValueError("age_min is greater than age_max")
        if (self.participant_count_min is not None
                and self.participant_count_max is not None
                and self.participant_count_min > self.participant_count_max):
            raise ValueError(
                "participant_count_min is greater than participant_count_max"
            )
        if self.max_price is not None and self.max_price < 0:
            raise ValueError("max_price must be non-negative")
        if self.preferred_max_price is not None and self.preferred_max_price < 0:
            raise ValueError("preferred_max_price must be non-negative")
        for field in ("name", "organizer", "venue", "source"):
            value = getattr(self, field)
            if value is not None:
                setattr(self, field, " ".join(value.split())[:120] or None)
        if self.near is not None:
            try:
                lat, lon = (float(x) for x in self.near.split(","))
            except ValueError:
                raise ValueError("near must be 'lat,lon'")
        if self.radius is not None:
            _radius_m(self.radius)  # raises ValueError on junk
        if self.min_tag_match is not None and not self.tags:
            raise ValueError("min_tag_match requires at least one tag")
        return self


# SearchFilters keeps every field required-but-nullable (strict structured
# output for the internal parser). External callers of POST /v1/query send
# partial bodies; these defaults fill the gaps.
FILTER_DEFAULTS: dict = {
    "from_dt": None, "to_dt": None, "categories": None,
    "exclude_categories": [], "exclude_terms": [], "name": None,
    "organizer": None, "venue": None, "source": None,
    "age_min": None,
    "age_max": None, "gender_split_min": None, "max_price": None,
    "preferred_max_price": None,
    "is_free": None, "kid_friendly": None, "newcomer_friendly": None,
    "outdoor": None, "solo_friendly": None, "interaction_structure": None,
    "energy": None, "language": None, "sex_service_context": None,
    "participant_count_min": None, "participant_count_max": None,
    "required_attributes": [], "tags": [], "min_tag_match": None,
    "near": None, "radius": None,
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
        {}, description="0..1 weight per soft attribute, including tags, "
        "price, and event_scale; default 1.0 for every preference the request "
        "actually supplies. Stored certainty is always part of match_score")),
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
    "sex_service_context": _inferred_bool("sex_service_context"),
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
        kind="enum", value_sql="e.inferred->'language'->>'value'",
        conf_sql="(e.inferred->'language'->>'confidence')::float",
        hard_sql="e.inferred->'language'->>'value' = %({p})s",
    ),
    "price": Attribute(
        kind="max_float",
        value_sql=(
            "coalesce(e.price_min, "
            "(e.inferred->'price'->>'min')::float)"
        ),
        conf_sql=(
            "CASE WHEN e.price_min IS NOT NULL THEN coalesce("
            "(e.field_provenance->'price_min'->>'confidence')::float, 0.8) "
            "ELSE (e.inferred->'price'->>'confidence')::float END"
        ),
        hard_sql="e.price_min <= %({p})s",
    ),
    "event_scale": Attribute(
        kind="range",
        value_sql="e.expected_attendance",
        conf_sql="e.expected_attendance_confidence",
        hard_sql=(
            "(%({p}min)s::int IS NULL OR e.expected_attendance >= %({p}min)s) "
            "AND (%({p}max)s::int IS NULL OR "
            "e.expected_attendance <= %({p}max)s)"
        ),
    ),
}


def _wanted(f: SearchFilters) -> dict:
    """The attribute constraints the query actually states: name -> wanted."""
    wanted: dict = {}
    if f.age_min is not None and f.age_max is not None:
        wanted["age"] = (f.age_min, f.age_max)
    for name in ("gender_split_min", "kid_friendly", "newcomer_friendly",
                 "outdoor", "solo_friendly", "interaction_structure",
                 "energy", "language", "sex_service_context"):
        if (v := getattr(f, name)) is not None:
            wanted[name] = v
    if f.preferred_max_price is not None:
        wanted["price"] = f.preferred_max_price
    if (f.participant_count_min is not None
            or f.participant_count_max is not None):
        wanted["event_scale"] = (
            f.participant_count_min, f.participant_count_max
        )
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
        "a literal event title, put it in name; literal organizer, venue, and "
        "reporting-source names have their own fields. Put "
        "activity, topic, format, and mood concepts in tags as concise 1-3 "
        "word phrases; multilingual semantic matching handles translations. "
        "Put ALL jointly desired concepts in one tags list, never treat them "
        "as separate searches. max_price/is_free are hard stated-price facts; "
        "preferred_max_price is soft. participant_count_min/max describe the "
        "event_scale estimate and are soft unless event_scale is required. "
        "Set min_tag_match only for an explicit must/only requirement.\n"
        "Audience attributes (age, gender_split_min, kid_friendly, ...) are "
        "soft preferences by default; add a name to required_attributes ONLY "
        "when the user is emphatic ('unbedingt', 'muss', 'nur wenn').\n\n"
        f"QUERY: {q}",
        SearchFilters,
    )


# ------------------------------------------------------------------- SQL

def build_sql(
    f: SearchFilters, *, exclude_sex_service_context: bool = False,
) -> tuple[str, dict]:
    """HARD conditions only: guarantees + attributes marked required.
    null attribute = unknown = never matches a hard constraint (§7)."""
    conditions = ["o.status = 'scheduled'"]
    params: dict = {}
    # overlap semantics (Alexander 2026-07-13): something still RUNNING at
    # `from` is in the window - 95 ongoing exhibitions were invisible under
    # starts_at-only filtering (audit A21). Rows expose `ongoing`.
    conditions.append("coalesce(o.ends_at, o.starts_at) >= %(from)s")
    params["from"] = (
        datetime.fromisoformat(f.from_dt) if f.from_dt else datetime.now(VIENNA)
    )
    if f.to_dt:
        conditions.append("o.starts_at <= %(to)s")
        params["to"] = datetime.fromisoformat(f.to_dt)

    radius_any = (f.radius or "").strip().lower() == "any"
    if f.near is not None or (f.radius is not None and not radius_any):
        # explicit geo ask = hard filter: unknown location never matches
        lat, lon = (
            tuple(float(x) for x in f.near.split(",")) if f.near
            else LINZ_CENTER
        )
        conditions.append(
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(g_lon)s, %(g_lat)s), 4326)::geography, "
            "%(g_m)s)"
        )
        params.update(g_lat=lat, g_lon=lon, g_m=_radius_m(f.radius or "5km"))
    elif not radius_any:
        # default gate: the index is Linz - but unknown location stays IN
        conditions.append(
            "(coalesce(e.geo, v.geo) IS NULL OR "
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(g_lon)s, %(g_lat)s), 4326)::geography, "
            "%(g_m)s))"
        )
        params.update(
            g_lat=LINZ_CENTER[0], g_lon=LINZ_CENTER[1],
            g_m=DEFAULT_RADIUS_KM * 1000,
        )
    if f.categories:
        conditions.append("e.category && %(cats)s")
        params["cats"] = f.categories
    if f.exclude_categories:
        conditions.append("NOT (e.category && %(not_cats)s)")
        params["not_cats"] = f.exclude_categories
    for i, term in enumerate(f.exclude_terms):
        key = f"not_term_{i}"
        # coalesce: an unenriched event (inferred IS NULL) must be judged by
        # its title alone, not NULL-poisoned out of every negation query;
        # same for venue-less events (v.name NULL)
        conditions.append(
            f"NOT (e.title ILIKE %({key})s "
            f"OR coalesce(v.name ILIKE %({key})s, false) "
            f"OR coalesce(e.organizer ILIKE %({key})s, false) "
            f"OR EXISTS (SELECT 1 FROM event_tag et WHERE et.event_id = e.id "
            f"AND et.name ILIKE %({key})s))"
        )
        params[key] = f"%{term}%"
        params[key + "raw"] = term
    if f.name:
        # Event names are title-scoped. Suffix-boundary matching supports
        # German head-final compounds (Maturaball) without treating Ballett as
        # a "ball" hit. This regex is mechanical token handling, not semantic
        # content interpretation; semantic concepts belong in tags.
        import re as _re

        pat = r"[-\s]?".join(_re.escape(t) for t in f.name.split())
        conditions.append("e.title ~* %(event_name)s")
        params["event_name"] = rf"{pat}\M"
    if f.organizer:
        conditions.append("e.organizer ILIKE %(organizer_name)s")
        params["organizer_name"] = f"%{f.organizer}%"
    if f.venue:
        conditions.append("v.name ILIKE %(venue_name)s")
        params["venue_name"] = f"%{f.venue}%"
    if f.source:
        conditions.append(
            "EXISTS (SELECT 1 FROM identity src_i "
            "JOIN event_claim src_c ON src_c.fingerprint = src_i.fingerprint "
            "JOIN source src_s ON src_s.id = src_c.source_id "
            "WHERE src_i.event_id = e.id AND src_s.kind <> 'internal' "
            "AND (src_s.name ILIKE %(source_name)s "
            "OR src_s.url ILIKE %(source_name)s))"
        )
        params["source_name"] = f"%{f.source}%"
    if f.is_free:
        conditions.append("e.price_min = 0")
    elif f.max_price is not None:
        conditions.append("e.price_min <= %(max_price)s")
        params["max_price"] = f.max_price

    if exclude_sex_service_context:
        # MCP-safe default: suppress only a positively known commercial-sex
        # context. NULL stays visible because null means unknown throughout
        # the public index. The public API never enables this implicitly.
        conditions.append(
            "coalesce(v.sex_service, false) IS DISTINCT FROM TRUE AND "
            "(e.inferred->'sex_service_context'->>'value')::bool "
            "IS DISTINCT FROM TRUE"
        )

    wanted = _wanted(f)
    for name in f.required_attributes:
        if name not in wanted or name not in ATTRIBUTES:
            continue  # required without a stated value is a no-op
        attr = ATTRIBUTES[name]
        p = f"req_{name}"
        conditions.append(attr.hard_sql.format(p=p))
        if attr.kind == "age":
            params[p + "min"], params[p + "max"] = wanted["age"]
        elif attr.kind == "range":
            params[p + "min"], params[p + "max"] = wanted[name]
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
        elif attr.kind == "max_float":
            ok = float(value) <= want
        elif attr.kind == "range":
            lower, upper = want
            ok = (
                (lower is None or float(value) >= lower)
                and (upper is None or float(value) <= upper)
            )
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


def rank(
    rows: list[dict], f: SearchFilters,
    importance: dict[str, float] | None = None,
    tag_scores: dict | None = None,
) -> list[dict]:
    """Rank the allowed set by certainty-aware preferences and unified tags."""
    tag_scores = tag_scores or {}

    soft_attributes = {
        name: wanted for name, wanted in _wanted(f).items()
        if name not in f.required_attributes
    }

    def score(row) -> float:
        weighted_scores: list[tuple[float, float]] = []
        for name, want in soft_attributes.items():
            weight = (importance or {}).get(name, 1.0)
            if weight > 0:
                weighted_scores.append(
                    (weight, _satisfaction(row, name, want))
                )
        if f.tags:
            tag_match = float(tag_scores.get(row["event_id"], 0.0))
            row["tag_match"] = round(tag_match, 4)
            tag_weight = (importance or {}).get("tags", 1.0)
            if tag_weight > 0:
                weighted_scores.append((tag_weight, tag_match))
        else:
            row["tag_match"] = None
        preference = (
            sum(weight * value for weight, value in weighted_scores)
            / sum(weight for weight, _ in weighted_scores)
            if weighted_scores else 1.0
        )
        s = preference * (row["confidence"] or 0.0)
        row["match_score"] = round(s, 4)  # exposed: consumers see the weighting
        return s

    scored = [(score(row), row) for row in rows]
    if f.tags and f.min_tag_match is not None:
        scored = [
            pair for pair in scored
            if pair[1]["tag_match"] >= f.min_tag_match
        ]
    return [
        row for _, row in sorted(
            scored,
            key=lambda pair: (
                -pair[0],
                pair[1].get("starts_at")
                or datetime.max.replace(tzinfo=VIENNA),
                str(pair[1]["event_id"]),
            ),
        )
    ]
