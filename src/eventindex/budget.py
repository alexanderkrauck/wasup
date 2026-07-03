"""Budget enforcement (§5b): global daily cap + per-source monthly budget.

Every paid call must run between check_budget() and record_spend(). The LLM
client does this internally, so no LLM call can bypass the ledger.

Day/month boundaries are Europe/Vienna (business-logic timezone).
"""

from decimal import Decimal
from uuid import UUID

from eventindex import config


class BudgetExceeded(Exception):
    pass


_DAY_START = "date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s"
_MONTH_START = "date_trunc('month', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s"


def spent_today(tx) -> Decimal:
    row = tx.execute(
        f"SELECT coalesce(sum(amount_eur), 0) AS total FROM budget_spend "
        f"WHERE spent_at >= {_DAY_START}",
        {"tz": config.TIMEZONE},
    ).fetchone()
    return row["total"]


def source_spent_this_month(tx, source_id: UUID) -> Decimal:
    row = tx.execute(
        f"SELECT coalesce(sum(amount_eur), 0) AS total FROM budget_spend "
        f"WHERE source_id = %(source_id)s AND spent_at >= {_MONTH_START}",
        {"tz": config.TIMEZONE, "source_id": source_id},
    ).fetchone()
    return row["total"]


def check_budget(tx, source_id: UUID | None = None) -> None:
    """Raise BudgetExceeded if the global daily cap - or, when a source is
    given, its monthly budget - is exhausted."""
    today = spent_today(tx)
    if today >= config.GLOBAL_DAILY_LLM_CAP_EUR:
        raise BudgetExceeded(
            f"global daily cap reached: €{today} >= €{config.GLOBAL_DAILY_LLM_CAP_EUR}"
        )
    if source_id is not None:
        row = tx.execute(
            "SELECT monthly_budget_eur FROM source WHERE id = %s", (source_id,)
        ).fetchone()
        if row is not None:
            month = source_spent_this_month(tx, source_id)
            if month >= row["monthly_budget_eur"]:
                raise BudgetExceeded(
                    f"source {source_id} monthly budget reached: "
                    f"€{month} >= €{row['monthly_budget_eur']}"
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
) -> None:
    """Ledger a spend on its own connection, committed immediately.

    Deliberate exception to the everything-in-one-tx rule: the money is
    already spent at the provider, so the ledger row must survive a rollback
    of the failing job that spent it.
    """
    from eventindex import db

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO budget_spend "
            "(amount_eur, category, source_id, job_id, model, tokens_in, tokens_out, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (amount_eur, category, source_id, job_id, model, tokens_in, tokens_out, detail),
        )
        conn.commit()
