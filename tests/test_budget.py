import pytest

from eventindex import config
from eventindex.budget import BudgetExceeded, check_budget, record_spend


import uuid


def _make_source(conn, monthly_budget_eur):
    return conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, monthly_budget_eur) "
        "VALUES ('t', %s, 'website', 3, 0.65, %s) RETURNING id",
        (f"http://{uuid.uuid4().hex[:10]}.test", monthly_budget_eur),
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


def test_llm_create_retries_provider_json_blips(monkeypatch):
    """A non-JSON gateway response must not kill a 40-minute crawl
    (2026-07-11): _create retries transient parse/connection failures."""
    import json
    from types import SimpleNamespace

    from eventindex import llm

    calls = {"n": 0}

    def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return "response"

    fake = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=flaky_create)))
    monkeypatch.setattr(llm, "_get_client", lambda: fake)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None) if hasattr(llm, "time") else None
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert llm._create() == "response"
    assert calls["n"] == 3


def test_llm_credit_outage_becomes_budget_signal(monkeypatch):
    """Resolver fallbacks re-raise BudgetExceeded; a raw 402 must use that
    path or one rebuild makes a rejected request for every uncached event."""
    import httpx
    from openai import APIStatusError
    from types import SimpleNamespace

    from eventindex import llm

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(402, request=request)

    def no_credit(**kwargs):
        raise APIStatusError(
            "Payment required", response=response,
            body={"error": {"message": "Insufficient credits"}},
        )

    fake = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=no_credit)))
    monkeypatch.setattr(llm, "_get_client", lambda: fake)
    with pytest.raises(BudgetExceeded, match="Error code: 402"):
        llm._create()
