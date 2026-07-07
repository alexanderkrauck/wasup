"""Recurrence (H1): LLM fills a constrained schema; everything after that is
deterministic. The LLM NEVER writes RRULE.

schema -> compile() -> dateutil.rrule -> expand() -> occurrence datetimes,
with Austrian holiday/Ferien exceptions from the holiday table.
"""

from datetime import date, datetime, time as time_t, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dateutil import rrule as rr
from pydantic import BaseModel, ConfigDict, Field

from eventindex import config
from eventindex.budget import BudgetExceeded

VIENNA = ZoneInfo(config.TIMEZONE)
EXPANSION_WEEKS = 8

_WEEKDAYS = {"MO": rr.MO, "TU": rr.TU, "WE": rr.WE, "TH": rr.TH,
             "FR": rr.FR, "SA": rr.SA, "SU": rr.SU}


class Recurrence(BaseModel):
    """The constrained schema the LLM fills - enums and numbers only.

    Every field is required-but-nullable: OpenAI-style strict structured
    output wants all keys in `required`.
    """

    model_config = ConfigDict(extra="forbid")
    freq: Literal["once", "daily", "weekly", "monthly_by_weekday", "irregular"]
    weekday: Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"] | None
    week_of_month: int | None = Field(
        description="for monthly_by_weekday: 1-5 or -1 for last"
    )
    interval: int = Field(description="1 = every, 2 = every second, ...")
    time: str | None = Field(description="HH:MM, 24h")
    duration_minutes: int | None
    except_holidays: list[Literal["school_holidays", "public_holidays"]]
    valid_from: str | None = Field(description="ISO date")
    valid_until: str | None = Field(description="ISO date")
    as_stated: str = Field(description="verbatim source wording, always kept")


def load_holidays(tx) -> dict[str, list[tuple[date, date]]]:
    rows = tx.execute("SELECT kind, starts_on, ends_on FROM holiday").fetchall()
    out: dict[str, list[tuple[date, date]]] = {
        "public_holidays": [], "school_holidays": [],
    }
    for r in rows:
        key = "public_holidays" if r["kind"] == "public_holiday" else "school_holidays"
        out[key].append((r["starts_on"], r["ends_on"]))
    return out


def _in_holiday(d: date, ranges: list[tuple[date, date]]) -> bool:
    return any(a <= d <= b for a, b in ranges)


def _parse_time(value: str | None) -> time_t:
    if not value:
        return time_t(0, 0)
    try:
        h, m = value.split(":")
        return time_t(int(h), int(m))
    except ValueError:
        return time_t(0, 0)


def compile_rrule(rec: Recurrence, dtstart: datetime) -> rr.rrule | None:
    """Deterministic schema -> rrule. Returns None for once/irregular."""
    if rec.freq in ("once", "irregular"):
        return None
    if rec.freq == "daily":
        return rr.rrule(rr.DAILY, interval=rec.interval, dtstart=dtstart)
    if rec.weekday is None:
        return None  # weekly/monthly without a weekday is not expandable
    wd = _WEEKDAYS[rec.weekday]
    if rec.freq == "weekly":
        return rr.rrule(rr.WEEKLY, interval=rec.interval, byweekday=wd, dtstart=dtstart)
    # monthly_by_weekday
    ordinal = rec.week_of_month or 1
    return rr.rrule(rr.MONTHLY, interval=rec.interval, byweekday=wd(ordinal), dtstart=dtstart)


def expand(
    rec: Recurrence,
    holidays: dict[str, list[tuple[date, date]]],
    now: datetime | None = None,
    anchor: datetime | None = None,
) -> list[tuple[datetime, datetime | None]]:
    """Concrete (starts_at, ends_at) pairs for the next EXPANSION_WEEKS.

    anchor: a known real occurrence (the claim's starts_at). Without it, an
    interval>1 rule that lacks valid_from would phase-lock to `now` and can
    land on the wrong week (bit us live: STWST biweekly, off by one week).
    """
    now = now or datetime.now(VIENNA)
    horizon = now + timedelta(weeks=EXPANSION_WEEKS)
    at = _parse_time(rec.time)

    if rec.valid_from:
        valid_from = date.fromisoformat(rec.valid_from)
    elif anchor is not None:
        valid_from = anchor.astimezone(VIENNA).date()
    else:
        valid_from = now.date()
    valid_until = date.fromisoformat(rec.valid_until) if rec.valid_until else None

    if rec.freq == "irregular":
        return []
    if rec.freq == "once":
        starts = datetime.combine(valid_from, at, tzinfo=VIENNA)
        if now - timedelta(days=1) <= starts <= horizon:
            return [(starts, _ends(starts, rec))]
        return []

    # anchor dtstart at valid_from so interval>1 keeps its phase
    dtstart = datetime.combine(valid_from, at, tzinfo=VIENNA)
    rule = compile_rrule(rec, dtstart)
    if rule is None:
        return []

    skip_ranges = [r for key in rec.except_holidays for r in holidays.get(key, [])]
    out = []
    for occ in rule.between(now - timedelta(hours=12), horizon, inc=True):
        if valid_until and occ.date() > valid_until:
            break
        if _in_holiday(occ.date(), skip_ranges):
            continue
        out.append((occ, _ends(occ, rec)))
    return out


def _ends(starts: datetime, rec: Recurrence) -> datetime | None:
    if rec.duration_minutes:
        return starts + timedelta(minutes=rec.duration_minutes)
    return None


def series_fingerprint(title: str, venue_key: str, starts_at: datetime) -> str:
    """H1.3: series identity deliberately excludes dates -
    (venue, weekday, time bucket ±30min, normalized title)."""
    from eventindex.resolve.fingerprint import normalize_title

    local = starts_at.astimezone(VIENNA)
    bucket = (local.hour * 60 + local.minute) // 30
    return f"series|{normalize_title(title)}|{venue_key}|{local.strftime('%a')}|{bucket}"


class ConsistencyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consistent: bool


def verify(tx, rec: Recurrence, occurrences: list[datetime], **llm_kwargs) -> bool:
    """H1.1: checking is easier than extracting - a mini model compares the
    verbatim wording against the first compiled dates."""
    from eventindex import llm

    if not occurrences:
        return True
    sample = ", ".join(
        o.astimezone(VIENNA).strftime("%A %Y-%m-%d %H:%M") for o in occurrences[:4]
    )
    prompt = (
        f'A recurring event was described as (German): "{rec.as_stated}"\n'
        f"The compiled first occurrences are: {sample}\n"
        "Are the compiled occurrences consistent with the description? "
        "Check weekday, time and frequency."
    )
    try:
        return llm.complete(tx, prompt, ConsistencyCheck, **llm_kwargs).consistent
    except BudgetExceeded:
        raise  # a broke verifier must not cache 'tentative' forever
    except Exception:
        return False  # unverifiable -> caller marks tentative
