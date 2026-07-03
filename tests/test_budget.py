import pytest

from eventindex import config
from eventindex.budget import BudgetExceeded, check_budget, record_spend


def _make_source(conn, monthly_budget_eur):
    return conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, monthly_budget_eur) "
        "VALUES ('t', 'http://x', 'website', 3, 0.65, %s) RETURNING id",
        (monthly_budget_eur,),
    ).fetchone()["id"]


def test_global_daily_cap(conn):
    record_spend(config.GLOBAL_DAILY_LLM_CAP_EUR - 0.01, "llm")
    check_budget(conn)  # still under the cap
    record_spend(0.02, "llm")
    with pytest.raises(BudgetExceeded, match="global daily cap"):
        check_budget(conn)


def test_source_monthly_budget(conn):
    source_id = _make_source(conn, monthly_budget_eur=0.05)
    conn.commit()  # record_spend's own connection must see the source row
    record_spend(0.05, "llm", source_id=source_id)
    with pytest.raises(BudgetExceeded, match="monthly budget"):
        check_budget(conn, source_id=source_id)
    check_budget(conn)  # global cap untouched by the tiny amount


def test_source_spend_does_not_hit_other_sources(conn):
    exhausted = _make_source(conn, monthly_budget_eur=0.01)
    fresh = _make_source(conn, monthly_budget_eur=0.01)
    conn.commit()
    record_spend(0.01, "llm", source_id=exhausted)
    check_budget(conn, source_id=fresh)
