"""Resolver v2 (H0): canon as a pure, re-runnable function of the claims log.

    rebuild(conn) == DELETE canon; INSERT resolve(all_claims)   -- one tx

Deterministic and idempotent: event ids come from the identity table, LLM
adjudications and recurrence verifications are cached in `adjudication`,
occurrence ids are uuid5(event_id, starts_at), and all canon timestamps
derive from claim timestamps, never now().

Stages: venue-first resolution (H2) -> series grouping (H1.3) -> one-off
blocking + weighted match + grey-zone LLM adjudication (§6) -> field merge
trust×confidence with asymmetric status (§6) -> occurrence expansion (H1) ->
swap + confirmation sweep.
"""

import hashlib
import logging
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as time_t, timedelta

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from eventindex import config, llm
from eventindex.budget import BudgetExceeded
from eventindex.extract import parse_dt
from eventindex.resolve import match, projection, recurrence
from eventindex.resolve.fingerprint import VIENNA, geo_cell, normalize_title
from eventindex.resolve.recurrence import Recurrence, series_fingerprint
from eventindex.resolve.venues import VenueResolver, is_generic_location

log = logging.getLogger("eventindex.rebuild")

OCC_NS = uuid.UUID("6d5a3c1e-0000-4000-8000-000000000001")
NEGATIVE_STATUSES = {"cancelled", "moved", "postponed"}
MIN_TRUST_FOR_NEGATIVE = 0.5
EXPLICIT_SERIES_MIN_DATES = 3

FIELD_KEYS = [
    "title", "description", "venue_name", "address", "url", "image_url",
    "price_min", "price_max", "category", "organizer", "tags",
    "booking_url", "registration_required",
]
PAST_RETENTION_DAYS = 90  # archives are not events (STWST shipped 2001-2019)


@dataclass
class Claim:
    id: uuid.UUID
    source_id: uuid.UUID
    fingerprint: str
    extracted_at: datetime
    payload: dict
    trust: float
    source_url: str
    source_lat: float | None
    source_lon: float | None
    source_name: str = ""
    source_hint: dict | None = None
    crawl_interval: object = None
    title: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_id: uuid.UUID | None = None
    lat: float | None = None
    lon: float | None = None
    # True when lat/lon fell back to the SOURCE's geo (an aggregator's own
    # point): fine for blocking, must never be published as the event's
    # location - null means unknown, never "roughly downtown"
    geo_source_fallback: bool = False

    def value(self, key):
        entry = self.payload.get(key)
        return entry["value"] if entry else None

    def confidence(self, key) -> float:
        entry = self.payload.get(key)
        return entry["confidence"] if entry else 0.0

    @property
    def mean_confidence(self) -> float:
        confs = [f["confidence"] for f in self.payload.values()]
        return sum(confs) / len(confs) if confs else 0.0

    @property
    def has_time(self) -> bool:
        if self.starts_at is None:
            return False
        return self.starts_at.astimezone(VIENNA).timetz() != time_t(0, 0, tzinfo=VIENNA)

    def candidate(self) -> match.Candidate:
        return match.Candidate(
            title=self.title, starts_at=self.starts_at, ends_at=self.ends_at,
            venue_id=self.venue_id, lat=self.lat, lon=self.lon,
            organizer=self.value("organizer"), url=self.value("url"),
            has_time=self.has_time,
        )


class SameEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    same_event: bool


def _cache_verdict(pair_key, fp_a, fp_b, title_a, title_b, score, verdict, decided_by):
    """Written on an own connection: LLM verdicts cost money, so they must
    survive a rollback of the rebuild that produced them (like budget_spend)."""
    from eventindex import db

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO adjudication (pair_key, fingerprint_a, fingerprint_b, "
            "title_a, title_b, score, same_event, decided_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (pair_key) DO NOTHING",
            (pair_key, fp_a, fp_b, title_a, title_b, score, verdict, decided_by),
        )
        conn.commit()


def _load_claims(tx) -> list[Claim]:
    rows = tx.execute(
        """
        SELECT DISTINCT ON (c.source_id, c.fingerprint)
               c.id, c.source_id, c.fingerprint, c.extracted_at, c.payload,
               s.trust, s.url AS source_url, s.crawl_interval,
               s.name AS source_name, s.extraction_hint AS source_hint,
               ST_Y(s.geo) AS source_lat, ST_X(s.geo) AS source_lon
        FROM event_claim c JOIN source s ON s.id = c.source_id
        ORDER BY c.source_id, c.fingerprint, c.extracted_at DESC
        """
    ).fetchall()
    from eventindex.extract import (
        is_non_event, is_placeholder_title, normalize_claim,
    )

    claims = []
    for r in rows:
        c = Claim(**r)
        # claims are immutable; hygiene added later (entity unescape, price
        # plausibility, non-event patterns) must therefore re-run at rebuild
        # time - this is what repairs the historical corpus for free
        c.payload = normalize_claim(dict(c.payload))
        c.title = c.value("title") or ""
        c.starts_at = parse_dt(c.value("starts_at"))
        c.ends_at = parse_dt(c.value("ends_at"))
        c.lat = c.value("lat")
        c.lon = c.value("lon")
        if not c.title or c.starts_at is None:
            continue
        # legacy placeholder rows ("Sandburg Events") must also be skipped
        # at rebuild time - checked against both the source name AND the
        # claim's own venue (aggregator feeds carry venue programs as
        # placeholder items too)
        against = f'{c.source_name} {c.value("venue_name") or ""}'
        if is_placeholder_title(c.title, against):
            continue
        if is_non_event(c.title):
            continue
        claims.append(c)
    return claims


def _resolve_venues(tx, claims: list[Claim]) -> VenueResolver:
    resolver = VenueResolver(tx)
    for c in claims:
        if name := c.value("venue_name"):
            c.venue_id = resolver.resolve(name, c.lat, c.lon)
    # events inherit venue geo when the claim itself had none
    venue_geo = {
        r["id"]: (r["lat"], r["lon"])
        for r in tx.execute(
            "SELECT id, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM venue WHERE geo IS NOT NULL"
        )
    }
    for c in claims:
        if c.lat is None and c.venue_id in venue_geo:
            c.lat, c.lon = venue_geo[c.venue_id]
        if c.lat is None and c.source_lat is not None:
            c.lat, c.lon = c.source_lat, c.source_lon
            c.geo_source_fallback = True
    return resolver


def _venue_key(c: Claim) -> str:
    return str(c.venue_id) if c.venue_id else geo_cell(c.lat, c.lon)


_REC_DEFAULTS = {
    "weekday": None, "week_of_month": None, "interval": 1, "time": None,
    "duration_minutes": None, "except_holidays": [], "valid_from": None,
    "valid_until": None,
}


def _recurrence_of_raw(raw) -> Recurrence | None:
    if not raw:
        return None
    try:
        # stored payloads may omit null keys; the schema wants them explicit
        return Recurrence.model_validate(_REC_DEFAULTS | raw)
    except ValidationError:
        return None


def _recurrence_of(c: Claim) -> Recurrence | None:
    rec = _recurrence_of_raw(c.value("recurrence"))
    stated = rec.as_stated if rec is not None else ""
    if rec is not None and rec.freq == "daily" and (
        not _DAILY_CADENCE_RE.search(stated or "")
        or (
            _RECURRENCE_EXCEPTION_RE.search(stated or "")
            and _WEEKDAY_WORD_RE.search(stated or "")
        )
    ):
        # A bare validity range (live example: "bis 16.08.2026") is not
        # evidence that an event happens every day.  The constrained schema
        # promises `as_stated` is the source wording; reject an invented
        # daily cadence deterministically and keep the observed dates only.
        # The schema also cannot represent "daily except Tue/Sat"; expanding
        # that as all seven days is knowingly wrong, so it fails closed too.
        return None
    # NOTE (2026-07-20, Alexander: "weekdays are not special", "regex = bad"):
    # rule-vs-event coherence is judged by the H1.1 verifier with full event
    # context (title + anchor), never by vocabulary heuristics here. The
    # _DAILY_CADENCE_RE gate above is migration debt toward that same call.
    return rec


_DAILY_CADENCE_RE = re.compile(
    r"\b(täglich|taeglich|jeden\s+tag|alle\s+tage|daily|every\s+day"
    r"|mehr(mals|fach)\s+(am|pro)\s+tag"
    r"|(several|multiple)\s+times\s+(a|per)\s+day)\b",
    re.IGNORECASE,
)
_RECURRENCE_EXCEPTION_RE = re.compile(
    r"\b(außer|ausser|ausgenommen|except|not\s+on|nicht\s+(am|an))\b",
    re.IGNORECASE,
)
_WEEKDAY_WORD_RE = re.compile(
    r"\b(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)(s|en)?\b",
    re.IGNORECASE,
)


_WD_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _rule_fits_claim(rec: Recurrence, starts_at: datetime) -> bool:
    """A rule that cannot generate the claim's own weekday must not absorb
    the claim: in production a Thursdays-rule (shared series description)
    was stamped onto the Fri/Sat/Sun/Wed claims of a daily concert series
    and their real dates were discarded (audit A2a)."""
    if rec.freq not in ("weekly", "monthly_by_weekday") or rec.weekday is None:
        return True
    return _WD_CODES[starts_at.astimezone(VIENNA).weekday()] == rec.weekday


# German recurrence wording in free-text descriptions (aggregators often bury
# "jeden Mittwoch 3.6.-26.8." in prose the extractor stored as a one-off).
_TEXT_REC_RE = re.compile(
    r"\b(jede[nrm]?\s|wöchentlich|monatlich|täglich|vierzehntägig|14-?tägig"
    r"|alle\s+(zwei|drei|vier|zwölf|14)\s"
    r"|(montags|dienstags|mittwochs|donnerstags|freitags|samstags|sonntags))",
    re.IGNORECASE,
)


def _cache_text_recurrence(content_key: str, rec: Recurrence | None) -> None:
    """Own connection, like adjudication verdicts: the call cost money."""
    from eventindex import db

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO text_recurrence (content_key, recurrence) "
            "VALUES (%s, %s) ON CONFLICT (content_key) DO NOTHING",
            (content_key, Jsonb(rec.model_dump()) if rec else None),
        )
        conn.commit()


def _text_recurrence(tx, c: Claim) -> Recurrence | None:
    """Regex-gated free-text recurrence: only descriptions that literally use
    recurrence wording trigger an LLM call; verdicts are content-cached so
    rebuilds stay free."""
    desc = c.value("description")
    if not desc or not _TEXT_REC_RE.search(desc):
        return None
    # v2 key (2026-07-20): the verdict now depends on the claim's title and
    # anchor, so a shared umbrella description must not share verdicts
    # across the group's members
    content_key = hashlib.md5(
        f"textrec2|{c.title}|{desc}".encode()).hexdigest()
    cached = tx.execute(
        "SELECT recurrence FROM text_recurrence WHERE content_key = %s",
        (content_key,),
    ).fetchone()
    if cached is not None:
        return _recurrence_of_raw(cached["recurrence"])
    try:
        rec = llm.complete(
            tx,
            "This event description (German, Linz) may state a recurrence "
            "rule. Fill the schema from the TEXT ONLY - no guessing. If the "
            "text does not clearly state a repeating schedule, set freq to "
            f"'once'. The event's known date: {c.starts_at:%Y-%m-%d %H:%M}.\n\n"
            f"TEXT: {desc[:1500]}",
            Recurrence,
            source_id=c.source_id,
        )
    except BudgetExceeded:
        raise  # consistent with adjudication: park the rebuild, don't degrade
    except Exception:
        return None  # not cached: transient failures may retry next rebuild
    if rec.freq in ("once", "irregular"):
        rec = None
    if rec is not None:
        # verify AT BIRTH: an inconsistent rule (live case: 'Wednesday Tempo'
        # extracted as weekday=MO) must never enter grouping - a wrong-weekday
        # series served 'tentative' is worse than the observed dates alone
        holidays = recurrence.load_holidays(tx)
        pairs = recurrence.expand(rec, holidays, anchor=c.starts_at)
        try:
            ok = recurrence.verify(tx, rec, [p[0] for p in pairs],
                                   title=c.title, anchor=c.starts_at,
                                   source_id=c.source_id)
        except BudgetExceeded:
            raise
        except Exception:
            return None  # unverifiable now: retry next rebuild, cache nothing
        if not ok:
            rec = None  # cached as null: rejected for good
    _cache_text_recurrence(content_key, rec)
    return rec


# ---------------------------------------------------------------- grouping

def _group_claims(tx, claims: list[Claim], venue_notes: list[str]) -> list[dict]:
    """Returns groups: {"key": identity key, "claims": [...], "series": ...}."""
    groups: dict[str, dict] = {}
    oneoffs: list[Claim] = []

    # pass 1: recurrence-bearing claims -> H1.3 series fingerprint
    for c in claims:
        rec = _recurrence_of(c)
        rrule_raw = c.value("rrule_raw")
        if rec is None and rrule_raw is None:
            rec = _text_recurrence(tx, c)
        if rec is not None and not _rule_fits_claim(rec, c.starts_at):
            rec = None  # the claim's own date contradicts the rule
        if rec is None and rrule_raw is None:
            oneoffs.append(c)
            continue
        key = series_fingerprint(c.title, _venue_key(c))
        g = groups.setdefault(
            key, {"key": key, "claims": [], "recurrence": None, "rrule_raw": None}
        )
        g["claims"].append(c)
        g["recurrence"] = g["recurrence"] or rec
        g["rrule_raw"] = g["rrule_raw"] or rrule_raw

    # pass 2: explicit multi-date series (same title+venue, >=3 dates).
    # One-offs whose (title, venue) names an existing series join it -
    # THE anti-fragmentation rule: next week's cinema program and a
    # portal's per-date rows are the same series, not new events (A2).
    by_title_venue: dict[tuple, list[Claim]] = defaultdict(list)
    for c in oneoffs:
        by_title_venue[(normalize_title(c.title), _venue_key(c))].append(c)
    remaining: list[Claim] = []
    for (ntitle, vkey), cs in by_title_venue.items():
        days = {c.starts_at.astimezone(VIENNA).date() for c in cs}
        key = f"series|{ntitle}|{vkey}"
        if key in groups:
            groups[key]["claims"].extend(cs)
        elif len(days) >= EXPLICIT_SERIES_MIN_DATES:
            groups[key] = {
                "key": key, "claims": cs, "recurrence": None, "rrule_raw": None,
            }
        else:
            remaining.extend(cs)

    # pass 2.5: a group whose venue never RESOLVED must not duplicate the
    # same-titled group that did - the 176 dup pairs remaining after the
    # first repair rebuild were all {no-venue, venue} twins of one series
    # (2026-07-13). Merge only unambiguous, corroborated cases: every claim
    # venue-less, exactly ONE venue-bearing group with the same normalized
    # title, and at least one shared local date (two different "Sommerfest"
    # series must not collapse; same-title events at two REAL venues -
    # Clubabend - stay distinct anyway).
    def _days(claims: list[Claim]) -> set:
        return {c.starts_at.astimezone(VIENNA).date() for c in claims}

    with_venue: dict[str, list[str]] = defaultdict(list)
    for key, g in groups.items():
        if any(c.venue_id for c in g["claims"]):
            with_venue[normalize_title(g["claims"][0].title)].append(key)

    def _venue_twin_target(ntitle: str, days: set,
                           rec: Recurrence | None) -> dict | None:
        owners = with_venue.get(ntitle, [])
        if len(owners) != 1:
            return None
        target = groups.get(owners[0])
        if target is None:
            return None
        if days & _days(target["claims"]):
            return target
        # portals emit ONE row per date of a weekly course, venue present
        # on some rows only: disjoint observed days, identical rule - that
        # rule IS the corroboration (prod: Basic Training, 2026-07-13)
        t_rec = target["recurrence"]
        if (rec is not None and t_rec is not None
                and (rec.freq, rec.weekday, rec.time)
                == (t_rec.freq, t_rec.weekday, t_rec.time)):
            return target
        return None

    for key in list(groups):
        g = groups[key]
        if any(c.venue_id for c in g["claims"]):
            continue
        target = _venue_twin_target(
            normalize_title(g["claims"][0].title), _days(g["claims"]),
            g["recurrence"],
        )
        if target is not None and target is not g:
            target["claims"].extend(g["claims"])
            target["recurrence"] = target["recurrence"] or g["recurrence"]
            target["rrule_raw"] = target["rrule_raw"] or g["rrule_raw"]
            del groups[key]
    still_remaining = []
    for c in remaining:
        target = None
        if c.venue_id is None:
            target = _venue_twin_target(
                normalize_title(c.title),
                {c.starts_at.astimezone(VIENNA).date()},
                None,
            )
        if target is not None:
            target["claims"].append(c)
        else:
            still_remaining.append(c)
    remaining = still_remaining

    # pass 3: one-offs - block by (day, venue-cell) AND by (day, leading
    # title words). The second key catches cross-venue duplicates where one
    # source's claims carried the wrong/no venue (red-team 2026-07-07: the
    # Ahoi acts existed twice, Posthof-geo vs Donaupark). Venue-mismatched
    # pairs land in the grey zone, so the adjudicator - not geometry - makes
    # the call; the gold set gates its precision.
    blocks: dict[tuple, dict[str, list[Claim]]] = defaultdict(lambda: defaultdict(list))
    by_fp: dict[str, list[Claim]] = defaultdict(list)
    for c in remaining:
        day = c.starts_at.astimezone(VIENNA).date()
        blocks[("v", day, _venue_key(c))][c.fingerprint].append(c)
        # word-level: "Klassik am Dom 2026 - Erwin Schrott" must block with
        # "Erwin Schrott" - any shared informative word on the same day
        for word in set(normalize_title(c.title).split()):
            if len(word) > 3:
                blocks[("t", day, word)][c.fingerprint].append(c)
        by_fp[c.fingerprint].append(c)

    parent = {fp: fp for fp in by_fp}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    MAX_BLOCK = 20  # common-word blocks ("sommer") explode quadratically
    compared: set = set()
    for block in blocks.values():
        if len(block) > MAX_BLOCK:
            continue
        fps = sorted(block.keys())
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                pair = (fps[i], fps[j])
                if pair in compared:
                    continue
                compared.add(pair)
                a, b = block[fps[i]][0], block[fps[j]][0]
                score = match.pair_score(a.candidate(), b.candidate())
                verdict = match.classify(score)
                if verdict == match.ADJUDICATE:
                    same = _adjudicate(tx, a, b, score)
                    verdict = match.MERGE if same else match.DISTINCT
                    if same:
                        _reconcile_venues(tx, a, b, venue_notes)
                if verdict == match.MERGE:
                    parent[find(fps[i])] = find(fps[j])

    merged: dict[str, list[Claim]] = defaultdict(list)
    for fp in by_fp:
        merged[find(fp)].extend(by_fp[fp])
    for cs in merged.values():
        key = min(c.fingerprint for c in cs)  # deterministic group key
        groups[key] = {
            "key": key, "claims": cs, "recurrence": None, "rrule_raw": None,
        }
    return _merge_shared_fingerprints(list(groups.values()))


def _merge_shared_fingerprints(groups: list[dict]) -> list[dict]:
    """Claims from different sources can carry the SAME fingerprint string yet
    land in different groups (one in a series, one as a one-off). Such groups
    are the same identity - union them, or two events would claim one id."""
    parent = list(range(len(groups)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, g in enumerate(groups):
        for fp in {c.fingerprint for c in g["claims"]}:
            if fp in owner:
                parent[find(i)] = find(owner[fp])
            else:
                owner[fp] = i

    merged: dict[int, dict] = {}
    for i, g in enumerate(groups):
        m = merged.setdefault(find(i), {
            "claims": [], "recurrence": None, "rrule_raw": None, "keys": [],
        })
        m["claims"].extend(g["claims"])
        m["recurrence"] = m["recurrence"] or g["recurrence"]
        m["rrule_raw"] = m["rrule_raw"] or g["rrule_raw"]
        m["keys"].append(g["key"])

    out = []
    for m in merged.values():
        series_keys = sorted(k for k in m["keys"] if k.startswith("series|"))
        m["key"] = series_keys[0] if series_keys else min(m["keys"])
        seen: set = set()
        m["claims"] = [c for c in m["claims"] if not (c.id in seen or seen.add(c.id))]
        out.append(m)
    return out


VENUE_ALIAS_MAX_M = 300


def _reconcile_venues(tx, a: Claim, b: Claim, notes: list[str]) -> None:
    """An adjudicated merge across two DIFFERENT resolved venues means the
    venue table has a duplicate spelling: alias it when the geos agree
    (<300m), otherwise leave it for the human review dump."""
    if not a.venue_id or not b.venue_id or a.venue_id == b.venue_id:
        return
    keep, drop = (a, b) if a.trust >= b.trust else (b, a)
    alias = (drop.value("venue_name") or "").strip()
    dist = tx.execute(
        "SELECT ST_Distance(a.geo::geography, b.geo::geography) AS m "
        "FROM venue a, venue b WHERE a.id = %s AND b.id = %s",
        (keep.venue_id, drop.venue_id),
    ).fetchone()
    if (alias and not is_generic_location(alias)
            and dist and dist["m"] is not None and dist["m"] < VENUE_ALIAS_MAX_M):
        tx.execute(
            "UPDATE venue SET aliases = array_append(aliases, %(n)s) "
            "WHERE id = %(id)s AND NOT (%(n)s = ANY(aliases)) "
            "AND lower(name) != lower(%(n)s)",
            {"n": alias, "id": keep.venue_id},
        )
    else:
        notes.append(
            f"merged across venues (geo unknown or apart): "
            f"'{keep.value('venue_name')}' vs '{alias}' for '{a.title}'"
        )


def _adjudicate(tx, a: Claim, b: Claim, score: float) -> bool:
    fp_a, fp_b = sorted((a.fingerprint, b.fingerprint))
    pair_key = hashlib.md5(f"{fp_a}|{fp_b}".encode()).hexdigest()
    cached = tx.execute(
        "SELECT same_event FROM adjudication WHERE pair_key = %s", (pair_key,)
    ).fetchone()
    if cached is not None:
        return cached["same_event"]

    def fmt(c: Claim) -> str:
        local = c.starts_at.astimezone(VIENNA)
        time = "Uhrzeit unbekannt" if not c.has_time else f"{local:%H:%M}"
        desc = " ".join((c.value("description") or "").split())[:200]
        price = c.value("price_min")
        return (f"'{c.title}' am {local:%Y-%m-%d} ({time}), "
                f"Ort: {c.value('venue_name') or 'unbekannt'}, "
                f"Veranstalter: {c.value('organizer') or 'unbekannt'}, "
                f"Quelle: {c.source_name}, "
                f"URL: {c.value('url') or '-'}, "
                f"Preis: {price if price is not None else '?'}, "
                f"Beschreibung: {desc or '-'}")

    prompt = (
        "Two event listings from different websites (Linz, Austria):\n"
        f"A: {fmt(a)}\nB: {fmt(b)}\n"
        "Do A and B describe the SAME real-world happening? Guidance: "
        "series/festival naming vs. act naming is the SAME event "
        "('Klassik am Dom: X' = 'X' on that date); a festival's headline "
        "act on the same evening is the festival concert itself; unknown "
        "venue or time is missing data, not evidence of difference. "
        "DIFFERENT means: genuinely separate happenings (two films, two "
        "shows at different times, different acts)."
    )
    decided_by = "llm"
    try:
        verdict = llm.complete(tx, prompt, SameEvent).same_event
        if not verdict and score >= match.MID_ESCALATION:
            # a high-score pair the mini model keeps apart is exactly where
            # marquee duplicates hide - one mid-model second opinion, cached
            verdict = llm.complete(
                tx, prompt, SameEvent, model=config.MODEL_MID
            ).same_event
            decided_by = "llm_mid"
    except BudgetExceeded:
        raise  # no verdicts without money - park the rebuild, don't guess
    except Exception:
        # unadjudicable -> keep distinct (precision first), but do NOT cache
        # a failure as a verdict: the next rebuild gets to retry
        return False
    _cache_verdict(pair_key, fp_a, fp_b, a.title, b.title, score, verdict, decided_by)
    return verdict


# ---------------------------------------------------------------- identity

def _assign_identity(tx, groups: list[dict]) -> None:
    rows = tx.execute("SELECT fingerprint, event_id, first_seen FROM identity").fetchall()
    known = {r["fingerprint"]: r for r in rows}
    taken: set = set()
    for g in groups:
        fps = set(g.get("keys", [])) | {g["key"]} | {c.fingerprint for c in g["claims"]}
        owners = sorted(
            {known[fp]["event_id"] for fp in fps if fp in known},
            key=lambda eid: min(
                r["first_seen"] for r in known.values() if r["event_id"] == eid
            ),
        )
        if owners:
            survivor = owners[0]
            if len(owners) > 1:  # merge: repoint lineage to the survivor
                tx.execute(
                    "UPDATE identity SET event_id = %s WHERE event_id = ANY(%s)",
                    (survivor, owners[1:]),
                )
                for r in known.values():
                    if r["event_id"] in owners[1:]:
                        r["event_id"] = survivor
        else:
            survivor = uuid.uuid4()
        if survivor in taken:
            # lineage split: a fingerprint set that left its old group gets a
            # fresh id rather than colliding with the group that kept it
            survivor = uuid.uuid4()
            tx.execute(
                "UPDATE identity SET event_id = %s WHERE fingerprint = ANY(%s)",
                (survivor, list(fps)),
            )
            for fp in fps:
                if fp in known:
                    known[fp]["event_id"] = survivor
        taken.add(survivor)
        first_seen = min(c.extracted_at for c in g["claims"])
        for fp in fps:
            if fp not in known:
                tx.execute(
                    "INSERT INTO identity (fingerprint, event_id, first_seen) "
                    "VALUES (%s, %s, %s) ON CONFLICT (fingerprint) DO NOTHING",
                    (fp, survivor, first_seen),
                )
                known[fp] = {"fingerprint": fp, "event_id": survivor,
                             "first_seen": first_seen}
        g["event_id"] = survivor
        g["first_seen"] = min(known[fp]["first_seen"] for fp in fps)


# ---------------------------------------------------------------- merging

def _merge_fields(g: dict) -> tuple[dict, dict]:
    """§6 cross-source merge: per field the claim with highest
    trust × field_confidence wins. At an exact title or URL weight tie,
    the claim with the more specific cleaned title wins; claim id remains
    the final tie-break."""
    values, provenance = {}, {}
    for key in FIELD_KEYS:
        best = max(
            (c for c in g["claims"] if c.value(key) is not None),
            key=lambda c: (
                c.trust * c.confidence(key),
                len(c.value("title") or "") if key in {"title", "url"} else 0,
                str(c.id),
            ),
            default=None,
        )
        if best is not None:
            values[key] = best.value(key)
            provenance[key] = {
                "claim": str(best.id), "confidence": best.confidence(key),
            }
    return values, provenance


def _merge_status(claims: list[Claim]) -> dict:
    """Asymmetric recency-first status merge (§6), per Vienna day.

    A trusted negative beats all OLDER positives; only a NEWER positive from
    an equal-or-higher-trust source reverts it."""
    per_day: dict = {}
    for c in sorted(claims, key=lambda c: (c.extracted_at, str(c.id))):
        day = c.starts_at.astimezone(VIENNA).date()
        status = c.value("status")
        negative = status in NEGATIVE_STATUSES
        current = per_day.get(day)
        if negative and c.trust > MIN_TRUST_FOR_NEGATIVE:
            per_day[day] = {"status": status, "trust": c.trust, "at": c.extracted_at}
        elif not negative and current and c.extracted_at > current["at"] \
                and c.trust >= current["trust"]:
            per_day[day] = None  # reverted by newer equal-or-higher-trust positive
    return {d: v for d, v in per_day.items() if v}


def _event_confidence(claims: list[Claim]) -> float:
    """§7: independent confirmations compound, per distinct source."""
    by_source: dict = {}
    for c in claims:
        by_source[c.source_id] = max(
            by_source.get(c.source_id, 0), c.trust * c.mean_confidence
        )
    p_none = 1.0
    for v in by_source.values():
        p_none *= 1 - v
    return 1 - p_none


def _fallback_source_url(claims: list, rep) -> str | None:
    """The representative claim's source URL, unless that source is
    internal (red team 2026-07-20: the QA verifier's trust 0.9 made it
    'rep' on 29 events and internal://qa-verifier shipped as their
    canonical URL). Internal sources never publish URLs."""
    if not rep.source_url.startswith("internal://"):
        return rep.source_url
    return next((c.source_url for c in claims
                 if not c.source_url.startswith("internal://")), None)


def _verified(tx, key: str, rec: Recurrence, occs: list[datetime],
              title: str = "", anchor: datetime | None = None) -> bool:
    """H1.1 verify-call, cached so rebuilds stay free. verify2 key
    (2026-07-20): verdicts now weigh the event's own title/anchor, so the
    old as_stated-only verdicts must not be reused."""
    check_key = hashlib.md5(
        f"verify2|{key}|{title}|{rec.as_stated}".encode()
    ).hexdigest()
    cached = tx.execute(
        "SELECT same_event FROM adjudication WHERE pair_key = %s", (check_key,)
    ).fetchone()
    if cached is not None:
        return cached["same_event"]
    try:
        ok = recurrence.verify(tx, rec, occs, title=title, anchor=anchor)
    except BudgetExceeded:
        raise  # no verdicts without money - park the rebuild
    except Exception:
        return False  # tentative for THIS rebuild only, never cached
    _cache_verdict(
        check_key, key, rec.as_stated[:200], rec.as_stated[:200], None, 0, ok,
        "recurrence_verify",
    )
    return ok


# ---------------------------------------------------------------- canon

def _coverage_edge(claims: list[Claim], last_observed: date) -> date:
    """How far the claims' sources demonstrably see into the future: a date
    inside this edge that the feeds did NOT show is evidence of absence, so
    projection may only start beyond it."""
    edge = last_observed
    for c in claims:
        horizon = (c.source_hint or {}).get("horizon_days")
        if horizon is not None:
            reach = c.extracted_at.astimezone(VIENNA).date() + timedelta(
                days=float(horizon)
            )
            edge = max(edge, reach)
    return edge


def _project_series(g: dict, pairs: list) -> list:
    """Completeness contract: continue a regular implicit series past its
    sources' feed horizon (flagged, capped at PROJECTION_WEEKS)."""
    days = sorted({p[0].astimezone(VIENNA).date() for p in pairs})
    cadence = projection.detect_cadence(days)
    if cadence is None:
        return []
    edge = _coverage_edge(g["claims"], days[-1])
    at = Counter(p[0].astimezone(VIENNA).timetz() for p in pairs).most_common(1)[0][0]
    durations = Counter(p[1] - p[0] for p in pairs if p[1] is not None)
    duration = durations.most_common(1)[0][0] if durations else None
    return [
        (starts, starts + duration if duration else None)
        for d in projection.project(days, cadence, edge)
        for starts in [datetime.combine(d, at)]
    ]


_BARE_DOMAIN_RE = re.compile(r"^https?://[^/]+/?$")


def _deep_url(url: str | None) -> bool:
    return bool(url) and not _BARE_DOMAIN_RE.match(url)


_MAX_EVENT_SPAN = timedelta(days=14)
_MAX_EXHIBITION_SPAN = timedelta(days=370)
_MAX_SINGLE_TIMED_OCCURRENCE_SPAN = timedelta(hours=12)


def _sane_end(
    starts: datetime, ends: datetime | None, categories: list[str] | None = None,
) -> datetime | None:
    """Reject validity periods masquerading as continuous occurrences.

    Long art/culture exhibitions are real overlapping events. For ordinary
    activities, courses, markets, and series, a span beyond two weeks is a
    schedule validity range and must not make the event appear to run every
    minute for months.
    """
    max_span = (
        _MAX_EXHIBITION_SPAN
        if set(categories or []) & {"art", "culture"}
        else _MAX_EVENT_SPAN
    )
    if ends is None or ends < starts or ends - starts > max_span:
        return None
    return ends


def _fold_pairs(cands: list[tuple[datetime, datetime | None, bool]]) -> list:
    """One occurrence per local day for date-only candidates; timed starts
    keep their exact timestamps (double screenings are real). A date-only
    candidate never duplicates a timed one on the same local day - that
    exact pattern put a midnight phantom next to the real 19:30 occurrence
    on 404 event-days (audit A9)."""
    seen_ts, seen_day, out = set(), set(), []
    # Equal starts from a wrapper and a specific performance page are one
    # occurrence. Prefer a known, shorter end: wrapper listing windows often
    # run to 23:59 while the specific page carries the actual concert end.
    for starts, ends, _ in sorted(
        (c for c in cands if c[2]),
        key=lambda t: (
            t[0], t[1] is None,
            t[1] - t[0] if t[1] is not None else timedelta.max,
        ),
    ):
        if starts in seen_ts:
            continue
        seen_ts.add(starts)
        seen_day.add(starts.astimezone(VIENNA).date())
        out.append((starts, ends))
    for starts, ends, _ in sorted((c for c in cands if not c[2]),
                                  key=lambda t: t[0]):
        day = starts.astimezone(VIENNA).date()
        if day in seen_day:
            continue
        seen_day.add(day)
        out.append((starts, ends))
    out.sort(key=lambda p: p[0])
    return out


def _claim_cands(
    claims: list, *, is_series: bool = False,
) -> list[tuple[datetime, datetime | None, bool]]:
    """Turn claims into occurrence candidates with schedule-end hygiene.

    A timed performance cannot continuously run across a later performance
    of the same series. In that exact shape, a far end is the program/run's
    validity boundary, not the occurrence's DTEND. Standalone multi-day
    festivals and date-only camps have no later sibling performance and keep
    their real end.
    """
    starts = {c.starts_at for c in claims if c.starts_at is not None}
    out = []
    for c in claims:
        ends = _sane_end(c.starts_at, c.ends_at, c.value("category"))
        if (
            is_series
            and c.has_time
            and ends is not None
            and ends - c.starts_at > _MAX_SINGLE_TIMED_OCCURRENCE_SPAN
            and any(c.starts_at < other < ends for other in starts)
        ):
            ends = None
        out.append((c.starts_at, ends, c.has_time))
    return out


def _occurrences_for(
    tx, g: dict, holidays, now: datetime
) -> tuple[list, bool, str | None, set]:
    """Returns ([(starts, ends)], tentative, rrule_text, projected starts)."""
    rec = g["recurrence"]
    if rec is not None:
        anchor = min(c.starts_at for c in g["claims"])
        expanded = recurrence.expand(rec, holidays, now=now, anchor=anchor)
        # observed claim dates are ground truth - the rule only EXTENDS
        # them; discarding them lost the Fri-Sun concerts (audit A2a)
        cands = [
            (s, _sane_end(s, e),
             s.astimezone(VIENNA).timetz() != recurrence.time_t(0, 0, tzinfo=VIENNA))
            for s, e in expanded
        ] + _claim_cands(g["claims"], is_series=True)
        pairs = _fold_pairs(cands)
        rule = recurrence.compile_rrule(
            rec, pairs[0][0] if pairs else anchor
        )
        rep = g["claims"][0]
        tentative = not _verified(tx, g["key"], rec, [p[0] for p in pairs],
                                  title=rep.title, anchor=rep.starts_at)
        return pairs, tentative, str(rule) if rule else None, set()
    if g["rrule_raw"]:
        from dateutil.rrule import rrulestr

        anchor = min(c.starts_at for c in g["claims"])
        try:
            rule = rrulestr(g["rrule_raw"], dtstart=anchor)
            horizon = now + recurrence.timedelta(weeks=recurrence.EXPANSION_WEEKS)
            duration = None
            first = g["claims"][0]
            if first.ends_at and _sane_end(
                first.starts_at, first.ends_at, first.value("category")
            ):
                duration = first.ends_at - first.starts_at
            pairs = [
                (o, o + duration if duration else None)
                for o in rule.between(now - recurrence.timedelta(hours=12), horizon, inc=True)
            ]
            return pairs, False, g["rrule_raw"], set()
        except (ValueError, TypeError):
            pass
    # explicit dates: union over claims, folded per local day
    pairs = _fold_pairs(
        _claim_cands(g["claims"], is_series=g["key"].startswith("series|"))
    )
    projected: set = set()
    if g["key"].startswith("series|"):
        extra = _project_series(g, pairs)
        projected = {p[0] for p in extra}
        pairs.extend(extra)
    return pairs, False, None, projected


def rebuild(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(VIENNA)
    stats = {"claims": 0, "events": 0, "occurrences": 0, "venues_created": 0,
             "projected": 0}
    with conn.transaction():
        # two rebuilds racing duplicate every LLM call and collide on venue
        # creation (UniqueViolation on prod, 2026-07-13); the xact lock
        # releases with this transaction
        locked = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext('eventindex.rebuild')) AS ok"
        ).fetchone()["ok"]
        if not locked:
            stats["skipped"] = "another rebuild is running"
            return stats
        previous_status = {
            r["id"]: r["status"]
            for r in conn.execute("SELECT id, status FROM occurrence")
        }
        holidays = recurrence.load_holidays(conn)
        claims = _load_claims(conn)
        stats["claims"] = len(claims)
        resolver = _resolve_venues(conn, claims)
        stats["venues_created"] = len(resolver.created)
        venue_notes: list[str] = []
        groups = _group_claims(conn, claims, venue_notes)
        _assign_identity(conn, groups)

        conn.execute("DELETE FROM occurrence")
        conn.execute("DELETE FROM event")

        newly_negative: list[uuid.UUID] = []
        suppressed: list[str] = []
        aggregator_junk: list[str] = []
        for g in groups:
            pairs, tentative, rrule_text, projected = _occurrences_for(
                conn, g, holidays, now
            )
            # canon is forward-looking: a recipe gap once ingested a 2001-
            # 2019 archive feed as events (audit A5); claims keep history
            pairs = [
                p for p in pairs
                if p[0] >= now - timedelta(days=PAST_RETENTION_DAYS)
            ]
            # a zero-occurrence event is invisible to every API read path;
            # 115 rule-bearing events with DTSTART=rebuild-time proved the
            # old recurrence exemption wrong (audit A3)
            if not pairs:
                continue
            values, provenance = _merge_fields(g)
            status_by_day = _merge_status(g["claims"])
            last_seen = max(c.extracted_at for c in g["claims"])
            is_series = bool(g["key"].startswith("series|"))
            rep = max(g["claims"], key=lambda c: (c.trust, str(c.id)))
            event_status = "tentative" if tentative else "confirmed"
            # publish only real locations (claim or venue geo); the source-
            # fallback point exists for blocking, not for the API - three
            # unrelated events at the aggregator's own coordinates was how
            # the first external consumer caught this (2026-07-09)
            lat, lon = (None, None) if rep.geo_source_fallback else (rep.lat, rep.lon)
            if rep.venue_id is None and _is_private_intent(
                values.get("address"), values.get("organizer")
            ):
                # §9b: technically-public != intended-public - publish the
                # event but not the (likely residential) location
                lat = lon = None
                suppressed.append(
                    f"{values.get('title', '')} | {values.get('address')} | "
                    f"{values.get('organizer')}"
                )
            if _is_global_aggregator_junk(
                g["claims"], values.get("url"), rep.venue_id, lat
            ):
                # not published at all (claims stay; the gate is part of the
                # pure rebuild function, so a later local claim or .at URL
                # resurrects the event automatically)
                aggregator_junk.append(
                    f"{values.get('title', '')} | {values.get('url')}"
                )
                continue

            # a bare-domain url asserts a detail link that isn't one (49% of
            # events shipped the source homepage, audit A7): any claim's
            # deep link beats the merged/fallback homepage
            url = values.get("url") or _fallback_source_url(g["claims"], rep)
            if url and not _deep_url(url):
                url = next(
                    (u for c in g["claims"]
                     if (u := c.value("url")) and _deep_url(u)),
                    url,
                )
            conn.execute(
                """
                INSERT INTO event (id, kind, title, description, rights, category,
                                   venue_id, geo, is_recurring, rrule,
                                   price_min, price_max, url, image_url,
                                   organizer, tags, booking_url,
                                   registration_required,
                                   field_provenance, confidence, status,
                                   expected_cadence,
                                   first_seen, last_seen, updated_at)
                VALUES (%(id)s, %(kind)s, %(title)s, %(description)s, 'quoted',
                        %(category)s, %(venue_id)s,
                        CASE WHEN %(lat)s::float IS NULL THEN NULL
                             ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                        %(is_recurring)s, %(rrule)s,
                        %(price_min)s, %(price_max)s, %(url)s, %(image_url)s,
                        %(organizer)s, %(tags)s, %(booking_url)s,
                        %(registration_required)s,
                        %(provenance)s, %(confidence)s, %(status)s,
                        %(cadence)s,
                        %(first_seen)s, %(last_seen)s, %(last_seen)s)
                """,
                {
                    "id": g["event_id"],
                    "kind": "series" if is_series else "one_off",
                    "title": values.get("title", ""),
                    "description": values.get("description"),
                    # taxonomy gate: deterministic extractors pass source-
                    # native categories through ("Allgemein", "Schnellschach")
                    # - canon publishes taxonomy values or unknown, never junk
                    "category": (
                        [c] if (c := str(values.get("category") or "").strip().lower())
                        in config.CATEGORIES else []
                    ),
                    "venue_id": rep.venue_id,
                    "lat": lat, "lon": lon,
                    "is_recurring": is_series,
                    "rrule": rrule_text,
                    "price_min": values.get("price_min"),
                    "price_max": values.get("price_max"),
                    "url": url,
                    "image_url": values.get("image_url"),
                    "organizer": values.get("organizer"),
                    "tags": values.get("tags") or [],
                    "booking_url": values.get("booking_url"),
                    "registration_required": values.get("registration_required"),
                    "provenance": Jsonb(provenance),
                    "confidence": _event_confidence(g["claims"]),
                    "status": event_status,
                    "cadence": min(
                        (c.crawl_interval for c in g["claims"] if c.crawl_interval),
                        default=None,
                    ),
                    "first_seen": g["first_seen"],
                    "last_seen": last_seen,
                },
            )
            for starts, ends in pairs:
                day = starts.astimezone(VIENNA).date()
                neg = status_by_day.get(day)
                occ_status = neg["status"] if neg else "scheduled"
                occ_id = uuid.uuid5(OCC_NS, f"{g['event_id']}|{starts.isoformat()}")
                conn.execute(
                    "INSERT INTO occurrence (id, event_id, starts_at, ends_at, "
                    "status, last_confirmed_at, projected, time_unknown) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (occ_id, g["event_id"], starts, ends, occ_status, last_seen,
                     starts in projected,
                     starts.astimezone(VIENNA).timetz() == time_t(0, 0, tzinfo=VIENNA)),
                )
                stats["occurrences"] += 1
                stats["projected"] += starts in projected
                if occ_status in NEGATIVE_STATUSES and \
                        previous_status.get(occ_id) not in NEGATIVE_STATUSES:
                    newly_negative.append(g["event_id"])
            stats["events"] += 1

        _confirmation_sweep(conn, newly_negative)
        _dump_venue_review(resolver.created, now, venue_notes)
        _dump_review("suppressed", suppressed, now)
        _dump_review("aggregator-junk", aggregator_junk, now)
        stats["aggregator_junk"] = len(aggregator_junk)
        stats["enrich_pending"] = _apply_enrichment(conn)
    return stats


def _apply_enrichment(tx) -> list:
    """Re-apply cached inferred attributes to the fresh canon (free); return
    event ids that still need an enrich LLM call. The cache holds the pure
    LLM verdict - curated venue facts (venue.sex_service) must be re-applied
    here too, or every rebuild would strip the flag from events whose own
    text looked innocent."""
    from eventindex.enrich import apply_to_event, content_key, venue_override

    rows = tx.execute(
        """
        SELECT e.id, e.title, e.description, e.category, v.name AS venue_name,
               v.sex_service AS venue_sex_service
        FROM event e LEFT JOIN venue v ON v.id = e.venue_id
        """
    ).fetchall()
    cached = {
        r["content_key"]: r["attributes"]
        for r in tx.execute("SELECT content_key, attributes FROM enrichment")
    }
    pending = []
    for row in rows:
        key = content_key(row)
        if key in cached:
            apply_to_event(tx, row["id"], venue_override(row, dict(cached[key])))
        else:
            pending.append(row["id"])
    return pending


def _confirmation_sweep(tx, event_ids: list[uuid.UUID]) -> None:
    """§6: every fresh negative triggers immediate re-crawl of the event's
    other sources."""
    if not event_ids:
        return
    from eventindex.jobs.worker import enqueue

    rows = tx.execute(
        """
        SELECT DISTINCT c.source_id FROM identity i
        JOIN event_claim c ON c.fingerprint = i.fingerprint
        JOIN source s ON s.id = c.source_id
        WHERE i.event_id = ANY(%s) AND s.kind != 'internal'
        """,
        (event_ids,),
    ).fetchall()
    for r in rows:
        enqueue(tx, "crawl", {"source_id": str(r["source_id"])})


# §9b private-intent suppression: residential-looking address + personal-name
# organizer + no resolved venue -> geo withheld. Deliberately conservative:
# both signals must fire, and only the location is suppressed, never the event.
_STREET_RE = re.compile(
    r"\b\w+(straße|strasse|gasse|weg|platz|ring|allee)\s+\d+", re.IGNORECASE
)
_ORG_WORDS = {
    "verein", "e.v.", "ev", "gmbh", "og", "kg", "club", "pfarre", "kirche",
    "universität", "uni", "institut", "verband", "chor", "schule", "zentrum",
    "haus", "bar", "café", "cafe", "theater", "museum", "galerie", "stadt",
    "linz", "team", "gruppe", "band", "orchester", "kulturverein",
}


# global platforms pad thin city listings with online/foreign events (found
# live 2026-07-10: Boston career fairs and a NASA launch served as Linz
# events, all via Eventbrite). Austria-local aggregators (linztermine, tips,
# meinbezirk, eventfinder) are NOT in this set - their whole scope is local,
# so placeless events from them are real events with lazy markup.
_GLOBAL_AGGREGATOR_RE = re.compile(r"eventbrite|meetup", re.I)


def _is_global_aggregator_junk(claims, url: str | None, venue_id, lat) -> bool:
    """No venue, no geo, ONLY global-platform provenance, and an event URL
    outside .at => an online/foreign event padded into the city listing.
    Conservative by construction: any local corroboration or .at URL keeps
    the event published."""
    from urllib.parse import urlparse

    if venue_id is not None or lat is not None:
        return False
    if not all(_GLOBAL_AGGREGATOR_RE.search(c.source_name or "") for c in claims):
        return False
    return not urlparse(url or "").netloc.endswith(".at")


def _is_private_intent(address: str | None, organizer: str | None) -> bool:
    if not address or not organizer or not _STREET_RE.search(address):
        return False
    words = organizer.split()
    if not 2 <= len(words) <= 3:
        return False
    if any(w.lower().strip(".,()") in _ORG_WORDS for w in words):
        return False
    return all(w[:1].isupper() for w in words)  # looks like a personal name


def _dump_review(prefix: str, lines: list[str], now: datetime) -> None:
    if not lines:
        return
    review_dir = config.VAR_DIR / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"{prefix}-{now:%Y-%m-%d}.md"
    items = [f"- [ ] {line}" for line in sorted(set(lines))]
    with path.open("a") as f:
        f.write(f"\n## rebuild {now:%H:%M}\n" + "\n".join(items) + "\n")


def _dump_venue_review(created: list[str], now: datetime,
                       notes: list[str] | None = None) -> None:
    _dump_review("venues", created + (notes or []), now)
