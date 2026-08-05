"""Atomic cost governance for every paid provider.

External requests reserve a conservative maximum on their own committed DB
connection before they start, then settle that row to the actual charge.  The
short advisory lock serializes admission across workers and API processes;
network work never runs while the lock is held.

The total Vienna-day envelope covers OpenRouter and Google Places together.
Bulk recovery and interactive natural-language search have smaller nested
lanes so neither can starve routine collection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from eventindex import config


class BudgetExceeded(Exception):
    pass


class DailyBudgetExceeded(BudgetExceeded):
    def __init__(self, message: str, *, lane: str | None = None):
        super().__init__(message)
        self.lane = lane


class ProviderUnavailable(BudgetExceeded):
    def __init__(
        self, message: str, *, provider: str, blocked_until: datetime | None = None
    ):
        super().__init__(message)
        self.provider = provider
        self.blocked_until = blocked_until


@dataclass(frozen=True)
class Reservation:
    id: UUID
    amount_eur: Decimal
    lane: str
    provider: str | None


_DAY_START = "date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s"
_MONTH_START = "date_trunc('month', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s"
_ADMISSION_LOCK = "eventindex.paid_budget.authorize"
_PROVIDER_LOCK = "eventindex.provider_circuit"

# The nested recovery allowance is specifically for bulk fact/backfill queues.
# Discovery, onboarding and QA stay routine work; exhausting an enrichment
# backlog must not park the machinery that finds and diagnoses sources.
RECOVERY_JOB_KINDS = frozenset({
    "enrich", "ground_venue", "hydrate_event", "timefix", "verify_event",
})
PAID_JOB_KINDS = frozenset({
    "agent_extract", "crawl", "discover", "enrich", "ground_venue",
    "hydrate_event", "onboard", "parity_audit", "probe", "qa_check",
    "resolve", "timefix", "verify_event",
})
_LANE_CAPS = {
    "recovery": Decimal(str(config.RECOVERY_DAILY_PAID_CAP_EUR)),
    "interactive": Decimal(str(config.INTERACTIVE_DAILY_PAID_CAP_EUR)),
}


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _lane(conn, job_id: UUID | None, requested: str | None) -> str:
    if requested is not None:
        if requested not in ("core", "recovery", "interactive"):
            raise ValueError(f"unknown budget lane {requested!r}")
        return requested
    if job_id is None:
        return "core"
    row = conn.execute("SELECT kind FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return "recovery" if row and row["kind"] in RECOVERY_JOB_KINDS else "core"


def spent_today(tx, *, include_reserved: bool = False) -> Decimal:
    expression = "amount_eur + reserved_eur" if include_reserved else "amount_eur"
    row = tx.execute(
        f"SELECT coalesce(sum({expression}), 0) AS total FROM budget_spend "
        f"WHERE spent_at >= {_DAY_START}",
        {"tz": config.TIMEZONE},
    ).fetchone()
    return row["total"]


def source_spent_this_month(
    tx, source_id: UUID, *, include_reserved: bool = False
) -> Decimal:
    expression = "amount_eur + reserved_eur" if include_reserved else "amount_eur"
    row = tx.execute(
        f"SELECT coalesce(sum({expression}), 0) AS total FROM budget_spend "
        f"WHERE source_id = %(source_id)s AND spent_at >= {_MONTH_START}",
        {"tz": config.TIMEZONE, "source_id": source_id},
    ).fetchone()
    return row["total"]


def _lane_spent_today(tx, lane: str) -> Decimal:
    row = tx.execute(
        "SELECT coalesce(sum(amount_eur + reserved_eur), 0) AS total "
        f"FROM budget_spend WHERE lane = %(lane)s AND spent_at >= {_DAY_START}",
        {"tz": config.TIMEZONE, "lane": lane},
    ).fetchone()
    return row["total"]


def _check_source_budget(
    tx, source_id: UUID | None, *, proposed: Decimal = Decimal("0")
) -> None:
    if source_id is None:
        return
    row = tx.execute(
        "SELECT monthly_budget_eur FROM source WHERE id = %s", (source_id,)
    ).fetchone()
    if row is None:
        return
    month = source_spent_this_month(tx, source_id, include_reserved=True)
    cap = row["monthly_budget_eur"]
    exhausted = month >= cap if proposed == 0 else month + proposed > cap
    if exhausted:
        raise BudgetExceeded(
            f"source {source_id} monthly budget reached/would be exceeded: "
            f"€{month} + €{proposed} > €{cap}"
        )


def check_budget(
    tx,
    source_id: UUID | None = None,
    *,
    job_id: UUID | None = None,
    lane: str | None = None,
) -> None:
    """Read-only compatibility check; paid calls must use reserve_spend()."""
    resolved_lane = _lane(tx, job_id, lane)
    today = spent_today(tx, include_reserved=True)
    total_cap = Decimal(str(config.GLOBAL_DAILY_PAID_CAP_EUR))
    if today >= total_cap:
        raise DailyBudgetExceeded(
            f"global daily paid cap reached: €{today} >= €{total_cap}"
        )
    lane_cap = _LANE_CAPS.get(resolved_lane)
    if lane_cap is not None:
        lane_total = _lane_spent_today(tx, resolved_lane)
        if lane_total >= lane_cap:
            raise DailyBudgetExceeded(
                f"{resolved_lane} daily paid cap reached: "
                f"€{lane_total} >= €{lane_cap}",
                lane=resolved_lane,
            )
    _check_source_budget(tx, source_id)


def reserve_spend(
    amount_eur: Decimal | float,
    category: str,
    *,
    source_id: UUID | None = None,
    job_id: UUID | None = None,
    lane: str | None = None,
    provider: str | None = None,
    detail: str | None = None,
) -> Reservation:
    """Atomically admit one maximum charge and return its durable reservation."""
    from eventindex import db

    amount = _decimal(amount_eur)
    if amount <= 0:
        raise ValueError("reservation must be positive")
    with db.connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_ADMISSION_LOCK,))
        resolved_lane = _lane(conn, job_id, lane)
        _check_source_budget(conn, source_id, proposed=amount)
        today = spent_today(conn, include_reserved=True)
        total_cap = Decimal(str(config.GLOBAL_DAILY_PAID_CAP_EUR))
        if today + amount > total_cap:
            raise DailyBudgetExceeded(
                f"global daily paid cap would be exceeded: "
                f"€{today} + €{amount} > €{total_cap}"
            )
        lane_cap = _LANE_CAPS.get(resolved_lane)
        if lane_cap is not None:
            lane_total = _lane_spent_today(conn, resolved_lane)
            if lane_total + amount > lane_cap:
                raise DailyBudgetExceeded(
                    f"{resolved_lane} daily paid cap would be exceeded: "
                    f"€{lane_total} + €{amount} > €{lane_cap}",
                    lane=resolved_lane,
                )
        row = conn.execute(
            "INSERT INTO budget_spend "
            "(amount_eur, reserved_eur, state, category, lane, provider, "
            " source_id, job_id, detail) "
            "VALUES (0, %s, 'reserved', %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (amount, category, resolved_lane, provider, source_id, job_id, detail),
        ).fetchone()
    return Reservation(row["id"], amount, resolved_lane, provider)


def settle_spend(
    reservation: Reservation,
    actual_eur: Decimal | float,
    *,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    detail: str | None = None,
) -> bool:
    """Replace a reservation with actual spend; return whether it overran."""
    from eventindex import db

    actual = _decimal(actual_eur)
    if actual < 0:
        raise ValueError("actual spend cannot be negative")
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "SELECT reserved_eur, state FROM budget_spend WHERE id = %s FOR UPDATE",
            (reservation.id,),
        ).fetchone()
        if row is None or row["state"] != "reserved":
            raise RuntimeError(f"spend reservation {reservation.id} is not active")
        conn.execute(
            "UPDATE budget_spend SET amount_eur=%s, reserved_eur=0, "
            "state='settled', model=%s, tokens_in=%s, tokens_out=%s, "
            "detail=coalesce(%s, detail) WHERE id=%s",
            (actual, model, tokens_in, tokens_out, detail, reservation.id),
        )
        return actual > row["reserved_eur"]


def release_spend(reservation: Reservation) -> None:
    """Release a request definitely rejected before any billable work."""
    from eventindex import db

    with db.connect() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM budget_spend WHERE id=%s AND state='reserved'",
            (reservation.id,),
        )


def mark_spend_uncertain(reservation: Reservation, detail: str) -> None:
    """Fail closed when a connection/parser failure may have been billed."""
    from eventindex import db

    with db.connect() as conn, conn.transaction():
        conn.execute(
            "UPDATE budget_spend SET amount_eur=reserved_eur, reserved_eur=0, "
            "state='uncertain', detail=%s WHERE id=%s AND state='reserved'",
            (detail[:2000], reservation.id),
        )


def record_spend(
    amount_eur: Decimal | float,
    category: str,
    *,
    source_id: UUID | None = None,
    job_id: UUID | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    detail: str | None = None,
    lane: str | None = None,
    provider: str | None = None,
) -> None:
    """Record already-paid or synthetic spend; external calls use reservations."""
    from eventindex import db

    amount = _decimal(amount_eur)
    with db.connect() as conn, conn.transaction():
        resolved_lane = _lane(conn, job_id, lane)
        conn.execute(
            "INSERT INTO budget_spend "
            "(amount_eur, category, lane, provider, source_id, job_id, model, "
            " tokens_in, tokens_out, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                amount, category, resolved_lane, provider, source_id, job_id,
                model, tokens_in, tokens_out, detail,
            ),
        )


def provider_circuit_state(provider: str) -> dict | None:
    from eventindex import db

    with db.connect() as conn:
        return conn.execute(
            "SELECT provider, blocked_until, reason FROM provider_circuit "
            "WHERE provider=%s",
            (provider,),
        ).fetchone()


def trip_provider_circuit(
    provider: str, reason: str, *, seconds: int = 3600
) -> datetime:
    from eventindex import db

    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "INSERT INTO provider_circuit (provider, blocked_until, reason) "
            "VALUES (%s, now() + %s * interval '1 second', %s) "
            "ON CONFLICT (provider) DO UPDATE SET "
            "blocked_until=greatest(provider_circuit.blocked_until, "
            " excluded.blocked_until), "
            "reason=excluded.reason, updated_at=now() "
            "RETURNING blocked_until",
            (provider, seconds, reason[:1000]),
        ).fetchone()
        return row["blocked_until"]


def claim_provider_probe(provider: str, *, lease_seconds: int = 300) -> bool:
    """Return True to one caller when an expired circuit needs a free probe."""
    from eventindex import db

    with db.connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_PROVIDER_LOCK,))
        row = conn.execute(
            "SELECT blocked_until, reason FROM provider_circuit "
            "WHERE provider=%s FOR UPDATE",
            (provider,),
        ).fetchone()
        if row is None:
            return False
        now = conn.execute("SELECT now() AS ts").fetchone()["ts"]
        if row["blocked_until"] > now:
            raise ProviderUnavailable(
                f"{provider} circuit open: {row['reason']}",
                provider=provider,
                blocked_until=row["blocked_until"],
            )
        lease = conn.execute(
            "UPDATE provider_circuit SET "
            "blocked_until=now() + %s * interval '1 second', "
            "reason='credit balance probe in progress', updated_at=now() "
            "WHERE provider=%s RETURNING blocked_until",
            (lease_seconds, provider),
        ).fetchone()
        return bool(lease)


def clear_provider_circuit(provider: str) -> None:
    from eventindex import db

    with db.connect() as conn, conn.transaction():
        conn.execute("DELETE FROM provider_circuit WHERE provider=%s", (provider,))
