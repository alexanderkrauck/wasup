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
import uuid
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import datetime, time as time_t

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from eventindex import config, llm
from eventindex.extract import parse_dt
from eventindex.resolve import match, recurrence
from eventindex.resolve.fingerprint import VIENNA, geo_cell, normalize_title
from eventindex.resolve.recurrence import Recurrence, series_fingerprint
from eventindex.resolve.venues import VenueResolver

log = logging.getLogger("eventindex.rebuild")

OCC_NS = uuid.UUID("6d5a3c1e-0000-4000-8000-000000000001")
NEGATIVE_STATUSES = {"cancelled", "moved", "postponed"}
MIN_TRUST_FOR_NEGATIVE = 0.5
EXPLICIT_SERIES_MIN_DATES = 3

FIELD_KEYS = [
    "title", "description", "venue_name", "address", "url", "image_url",
    "price_min", "price_max", "category", "organizer",
]


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
    crawl_interval: object = None
    title: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_id: uuid.UUID | None = None
    lat: float | None = None
    lon: float | None = None

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
               ST_Y(s.geo) AS source_lat, ST_X(s.geo) AS source_lon
        FROM event_claim c JOIN source s ON s.id = c.source_id
        ORDER BY c.source_id, c.fingerprint, c.extracted_at DESC
        """
    ).fetchall()
    from eventindex.extract import is_placeholder_title

    source_names = {
        r["id"]: r["name"] for r in tx.execute("SELECT id, name FROM source")
    }
    claims = []
    for r in rows:
        c = Claim(**r)
        c.title = c.value("title") or ""
        c.starts_at = parse_dt(c.value("starts_at"))
        c.ends_at = parse_dt(c.value("ends_at"))
        c.lat = c.value("lat")
        c.lon = c.value("lon")
        if not c.title or c.starts_at is None:
            continue
        # claims are immutable, so legacy placeholder rows ("Sandburg
        # Events") must also be skipped at rebuild time - checked against
        # both the source name AND the claim's own venue (aggregator feeds
        # carry venue programs as placeholder items too)
        against = f'{source_names.get(c.source_id, "")} {c.value("venue_name") or ""}'
        if is_placeholder_title(c.title, against):
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
    return resolver


def _venue_key(c: Claim) -> str:
    return str(c.venue_id) if c.venue_id else geo_cell(c.lat, c.lon)


_REC_DEFAULTS = {
    "weekday": None, "week_of_month": None, "interval": 1, "time": None,
    "duration_minutes": None, "except_holidays": [], "valid_from": None,
    "valid_until": None,
}


def _recurrence_of(c: Claim) -> Recurrence | None:
    raw = c.value("recurrence")
    if not raw:
        return None
    try:
        # stored payloads may omit null keys; the schema wants them explicit
        return Recurrence.model_validate(_REC_DEFAULTS | raw)
    except ValidationError:
        return None


# ---------------------------------------------------------------- grouping

def _group_claims(tx, claims: list[Claim]) -> list[dict]:
    """Returns groups: {"key": identity key, "claims": [...], "series": ...}."""
    groups: dict[str, dict] = {}
    oneoffs: list[Claim] = []

    # pass 1: recurrence-bearing claims -> H1.3 series fingerprint
    for c in claims:
        rec = _recurrence_of(c)
        rrule_raw = c.value("rrule_raw")
        if rec is None and rrule_raw is None:
            oneoffs.append(c)
            continue
        key = series_fingerprint(c.title, _venue_key(c), c.starts_at)
        g = groups.setdefault(
            key, {"key": key, "claims": [], "recurrence": None, "rrule_raw": None}
        )
        g["claims"].append(c)
        g["recurrence"] = g["recurrence"] or rec
        g["rrule_raw"] = g["rrule_raw"] or rrule_raw

    # pass 2: explicit multi-date series (same title+venue, >=3 dates)
    by_title_venue: dict[tuple, list[Claim]] = defaultdict(list)
    for c in oneoffs:
        by_title_venue[(normalize_title(c.title), _venue_key(c))].append(c)
    remaining: list[Claim] = []
    for (ntitle, vkey), cs in by_title_venue.items():
        days = {c.starts_at.astimezone(VIENNA).date() for c in cs}
        if len(days) >= EXPLICIT_SERIES_MIN_DATES:
            key = f"series|{ntitle}|{vkey}"
            groups[key] = {
                "key": key, "claims": cs, "recurrence": None, "rrule_raw": None,
            }
        else:
            remaining.extend(cs)

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
        return (f"'{c.title}' am {local:%Y-%m-%d} ({time}), "
                f"Ort: {c.value('venue_name') or 'unbekannt'}, "
                f"Veranstalter: {c.value('organizer') or 'unbekannt'}")

    try:
        verdict = llm.complete(
            tx,
            "Two event listings from different websites (Linz, Austria):\n"
            f"A: {fmt(a)}\nB: {fmt(b)}\n"
            "Do A and B describe the SAME real-world happening? Guidance: "
            "series/festival naming vs. act naming is the SAME event "
            "('Klassik am Dom: X' = 'X' on that date); a festival's headline "
            "act on the same evening is the festival concert itself; unknown "
            "venue or time is missing data, not evidence of difference. "
            "DIFFERENT means: genuinely separate happenings (two films, two "
            "shows at different times, different acts).",
            SameEvent,
        ).same_event
    except Exception:
        verdict = False  # unadjudicable -> keep distinct (precision first)
    _cache_verdict(pair_key, fp_a, fp_b, a.title, b.title, score, verdict, "llm")
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
    trust × field_confidence wins. Deterministic tie-break by claim id."""
    values, provenance = {}, {}
    for key in FIELD_KEYS:
        best = max(
            (c for c in g["claims"] if c.value(key) is not None),
            key=lambda c: (c.trust * c.confidence(key), str(c.id)),
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


def _verified(tx, key: str, rec: Recurrence, occs: list[datetime]) -> bool:
    """H1.1 verify-call, cached so rebuilds stay free."""
    check_key = hashlib.md5(
        f"verify|{key}|{rec.as_stated}".encode()
    ).hexdigest()
    cached = tx.execute(
        "SELECT same_event FROM adjudication WHERE pair_key = %s", (check_key,)
    ).fetchone()
    if cached is not None:
        return cached["same_event"]
    ok = recurrence.verify(tx, rec, occs)
    _cache_verdict(
        check_key, key, rec.as_stated[:200], rec.as_stated[:200], None, 0, ok,
        "recurrence_verify",
    )
    return ok


# ---------------------------------------------------------------- canon

def _occurrences_for(tx, g: dict, holidays, now: datetime) -> tuple[list, bool, str | None]:
    """Returns ([(starts, ends)], tentative, rrule_text)."""
    rec = g["recurrence"]
    if rec is not None:
        anchor = min(c.starts_at for c in g["claims"])
        pairs = recurrence.expand(rec, holidays, now=now, anchor=anchor)
        rule = recurrence.compile_rrule(
            rec, pairs[0][0] if pairs else now
        )
        tentative = not _verified(tx, g["key"], rec, [p[0] for p in pairs])
        return pairs, tentative, str(rule) if rule else None
    if g["rrule_raw"]:
        from dateutil.rrule import rrulestr

        anchor = min(c.starts_at for c in g["claims"])
        try:
            rule = rrulestr(g["rrule_raw"], dtstart=anchor)
            horizon = now + recurrence.timedelta(weeks=recurrence.EXPANSION_WEEKS)
            duration = None
            first = g["claims"][0]
            if first.ends_at:
                duration = first.ends_at - first.starts_at
            pairs = [
                (o, o + duration if duration else None)
                for o in rule.between(now - recurrence.timedelta(hours=12), horizon, inc=True)
            ]
            return pairs, False, g["rrule_raw"]
        except (ValueError, TypeError):
            pass
    # explicit dates: union over claims
    seen, pairs = set(), []
    for c in sorted(g["claims"], key=lambda c: c.starts_at):
        if c.starts_at not in seen:
            seen.add(c.starts_at)
            pairs.append((c.starts_at, c.ends_at))
    return pairs, False, None


def rebuild(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(VIENNA)
    stats = {"claims": 0, "events": 0, "occurrences": 0, "venues_created": 0}
    with conn.transaction():
        previous_status = {
            r["id"]: r["status"]
            for r in conn.execute("SELECT id, status FROM occurrence")
        }
        holidays = recurrence.load_holidays(conn)
        claims = _load_claims(conn)
        stats["claims"] = len(claims)
        resolver = _resolve_venues(conn, claims)
        stats["venues_created"] = len(resolver.created)
        groups = _group_claims(conn, claims)
        _assign_identity(conn, groups)

        conn.execute("DELETE FROM occurrence")
        conn.execute("DELETE FROM event")

        newly_negative: list[uuid.UUID] = []
        for g in groups:
            pairs, tentative, rrule_text = _occurrences_for(conn, g, holidays, now)
            if not pairs and g["recurrence"] is None and g["rrule_raw"] is None:
                continue
            values, provenance = _merge_fields(g)
            status_by_day = _merge_status(g["claims"])
            last_seen = max(c.extracted_at for c in g["claims"])
            is_series = bool(g["key"].startswith("series|"))
            rep = max(g["claims"], key=lambda c: (c.trust, str(c.id)))
            event_status = "tentative" if tentative else "confirmed"

            conn.execute(
                """
                INSERT INTO event (id, kind, title, description, rights, category,
                                   venue_id, geo, is_recurring, rrule,
                                   price_min, price_max, url, image_url,
                                   field_provenance, confidence, status,
                                   expected_cadence,
                                   first_seen, last_seen, updated_at)
                VALUES (%(id)s, %(kind)s, %(title)s, %(description)s, 'quoted',
                        %(category)s, %(venue_id)s,
                        CASE WHEN %(lat)s::float IS NULL THEN NULL
                             ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                        %(is_recurring)s, %(rrule)s,
                        %(price_min)s, %(price_max)s, %(url)s, %(image_url)s,
                        %(provenance)s, %(confidence)s, %(status)s,
                        %(cadence)s,
                        %(first_seen)s, %(last_seen)s, %(last_seen)s)
                """,
                {
                    "id": g["event_id"],
                    "kind": "series" if is_series else "one_off",
                    "title": values.get("title", ""),
                    "description": values.get("description"),
                    "category": [values["category"]] if values.get("category") else [],
                    "venue_id": rep.venue_id,
                    "lat": rep.lat, "lon": rep.lon,
                    "is_recurring": is_series,
                    "rrule": rrule_text,
                    "price_min": values.get("price_min"),
                    "price_max": values.get("price_max"),
                    "url": values.get("url") or rep.source_url,
                    "image_url": values.get("image_url"),
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
                    "status, last_confirmed_at) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (occ_id, g["event_id"], starts, ends, occ_status, last_seen),
                )
                stats["occurrences"] += 1
                if occ_status in NEGATIVE_STATUSES and \
                        previous_status.get(occ_id) not in NEGATIVE_STATUSES:
                    newly_negative.append(g["event_id"])
            stats["events"] += 1

        _confirmation_sweep(conn, newly_negative)
        _dump_venue_review(resolver.created, now)
        stats["enrich_pending"] = _apply_enrichment(conn)
    return stats


def _apply_enrichment(tx) -> list:
    """Re-apply cached inferred attributes to the fresh canon (free); return
    event ids that still need an enrich LLM call."""
    from eventindex.enrich import apply_to_event, content_key

    rows = tx.execute(
        """
        SELECT e.id, e.title, e.description, e.category, v.name AS venue_name
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
            apply_to_event(tx, row["id"], cached[key])
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
        WHERE i.event_id = ANY(%s)
        """,
        (event_ids,),
    ).fetchall()
    for r in rows:
        enqueue(tx, "crawl", {"source_id": str(r["source_id"])})


def _dump_venue_review(created: list[str], now: datetime) -> None:
    if not created:
        return
    review_dir = config.VAR_DIR / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"venues-{now:%Y-%m-%d}.md"
    lines = [f"- [ ] {name}" for name in sorted(set(created))]
    with path.open("a") as f:
        f.write(f"\n## rebuild {now:%H:%M}\n" + "\n".join(lines) + "\n")
