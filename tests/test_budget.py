import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest

from eventindex import config
from eventindex.budget import (
    BudgetExceeded,
    DailyBudgetExceeded,
    PAID_JOB_KINDS,
    ProviderUnavailable,
    RECOVERY_JOB_KINDS,
    check_budget,
    record_spend,
    release_spend,
    reserve_spend,
    settle_spend,
    trip_provider_circuit,
)


def test_audience_essentials_are_paid_core_not_recovery():
    assert "estimate_audience" in PAID_JOB_KINDS
    assert "estimate_audience" not in RECOVERY_JOB_KINDS


def _make_source(conn, monthly_budget_eur):
    return conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, monthly_budget_eur) "
        "VALUES ('t', %s, 'website', 3, 0.65, %s) RETURNING id",
        (f"http://{uuid.uuid4().hex[:10]}.test", monthly_budget_eur),
    ).fetchone()["id"]


def test_global_daily_cap_reserves_before_spend(conn):
    cap = Decimal(str(config.GLOBAL_DAILY_PAID_CAP_EUR))
    record_spend(cap - Decimal("0.01"), "llm")
    check_budget(conn)
    with pytest.raises(DailyBudgetExceeded, match="would be exceeded"):
        reserve_spend(Decimal("0.02"), "places", provider="google_places")


def test_settlement_returns_unused_capacity(conn):
    reservation = reserve_spend(Decimal("0.20"), "llm", provider="openrouter")
    assert settle_spend(reservation, Decimal("0.01")) is False
    row = conn.execute(
        "SELECT amount_eur, reserved_eur, state FROM budget_spend WHERE id=%s",
        (reservation.id,),
    ).fetchone()
    assert row == {
        "amount_eur": Decimal("0.01"),
        "reserved_eur": Decimal("0"),
        "state": "settled",
    }


def test_parallel_admission_cannot_overbook_cap(conn):
    cap = Decimal(str(config.GLOBAL_DAILY_PAID_CAP_EUR))
    record_spend(cap - Decimal("1.00"), "llm")

    def admit():
        try:
            return reserve_spend(Decimal("0.80"), "llm", provider="openrouter")
        except DailyBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: admit(), range(2)))
    admitted = [row for row in results if row is not None]
    assert len(admitted) == 1
    release_spend(admitted[0])


def test_recovery_lane_cannot_consume_core_allowance(conn):
    recovery_cap = Decimal(str(config.RECOVERY_DAILY_PAID_CAP_EUR))
    record_spend(recovery_cap - Decimal("0.10"), "llm", lane="recovery")
    with pytest.raises(DailyBudgetExceeded) as caught:
        reserve_spend(Decimal("0.20"), "llm", lane="recovery")
    assert caught.value.lane == "recovery"

    core = reserve_spend(Decimal("0.20"), "llm", lane="core")
    release_spend(core)


def test_source_monthly_budget(conn):
    source_id = _make_source(conn, monthly_budget_eur=0.05)
    conn.commit()
    record_spend(0.05, "llm", source_id=source_id)
    with pytest.raises(BudgetExceeded, match="monthly budget"):
        check_budget(conn, source_id=source_id)
    check_budget(conn)


def test_source_spend_does_not_hit_other_sources(conn):
    exhausted = _make_source(conn, monthly_budget_eur=0.01)
    fresh = _make_source(conn, monthly_budget_eur=0.01)
    conn.commit()
    record_spend(0.01, "llm", source_id=exhausted)
    check_budget(conn, source_id=fresh)


def test_source_reservations_are_admitted_atomically(conn):
    source_id = _make_source(conn, monthly_budget_eur=0.30)
    conn.commit()
    first = reserve_spend(Decimal("0.20"), "llm", source_id=source_id)
    with pytest.raises(BudgetExceeded, match="monthly budget reached"):
        reserve_spend(Decimal("0.20"), "llm", source_id=source_id)
    release_spend(first)


def test_llm_budgeted_create_reserves_each_ambiguous_retry(conn, monkeypatch):
    """Potentially billed lost responses consume allowance before retrying."""
    from eventindex import llm

    calls = {"n": 0}

    def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=10, cost=0.001,
        ))

    monkeypatch.setattr(llm, "_create", flaky_create)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    response = llm._budgeted_create(
        source_id=None,
        job_id=None,
        budget_lane=None,
        model=config.MODEL_MINI,
        messages=[],
    )
    assert response.usage.cost == 0.001
    assert calls["n"] == 3
    states = conn.execute(
        "SELECT state, count(*) AS n FROM budget_spend GROUP BY state"
    ).fetchall()
    assert {row["state"]: row["n"] for row in states} == {
        "settled": 1, "uncertain": 2,
    }


def test_structured_validation_retry_rechecks_allowance(conn, monkeypatch):
    from pydantic import BaseModel

    from eventindex import llm

    class Answer(BaseModel):
        ok: bool

    cap = Decimal(str(config.GLOBAL_DAILY_PAID_CAP_EUR))
    record_spend(cap - Decimal("0.38"), "llm")
    calls = {"n": 0}

    def invalid_response(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=38_000, completion_tokens=0, cost=None,
            ),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"no":true}'))],
        )

    monkeypatch.setattr(llm, "_create", invalid_response)
    with pytest.raises(DailyBudgetExceeded):
        llm.complete(conn, "answer", Answer)
    assert calls["n"] == 1


def test_llm_client_has_one_bounded_retry_layer(monkeypatch):
    from eventindex import llm

    seen = {}

    def fake_openai(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "OpenAI", fake_openai)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")

    llm._get_client()

    assert seen["timeout"] == llm.PROVIDER_TIMEOUT_SECONDS
    assert seen["max_retries"] == 0


def test_llm_request_rejects_provider_price_above_reviewed_max(conn, monkeypatch):
    from eventindex import llm

    seen = {}

    def success(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, cost=0.000001,
        ))

    monkeypatch.setattr(llm, "_create", success)
    llm._budgeted_create(
        source_id=None,
        job_id=None,
        budget_lane=None,
        model=config.MODEL_MINI,
        messages=[],
        extra_body={"usage": {"include": True}},
    )
    assert seen["extra_body"]["provider"]["max_price"] == {
        "prompt": 0.14,
        "completion": 0.28,
    }
    assert seen["extra_body"]["usage"] == {"include": True}


def test_llm_credit_outage_opens_global_circuit(conn, monkeypatch):
    import httpx
    from openai import APIStatusError

    from eventindex import llm

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(402, request=request)

    def no_credit(**kwargs):
        raise APIStatusError(
            "Payment required", response=response,
            body={"error": {"message": "Insufficient credits"}},
        )

    monkeypatch.setattr(llm, "_create", no_credit)
    with pytest.raises(ProviderUnavailable, match="daily key limit"):
        llm._budgeted_create(
            source_id=None,
            job_id=None,
            budget_lane=None,
            model=config.MODEL_MINI,
            messages=[],
        )
    assert conn.execute(
        "SELECT count(*) AS n FROM provider_circuit WHERE provider='openrouter'"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM budget_spend WHERE state='reserved'"
    ).fetchone()["n"] == 0


def test_daily_key_limit_error_opens_global_circuit(conn, monkeypatch):
    import httpx
    from openai import APIStatusError

    from eventindex import llm

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(400, request=request)

    def key_limit(**kwargs):
        raise APIStatusError(
            "Token budget reached",
            response=response,
            body={"error": {"code": "token_limit_exceeded"}},
        )

    monkeypatch.setattr(llm, "_create", key_limit)
    with pytest.raises(ProviderUnavailable):
        llm._budgeted_create(
            source_id=None,
            job_id=None,
            budget_lane=None,
            model=config.MODEL_MINI,
            messages=[],
        )
    assert conn.execute(
        "SELECT count(*) AS n FROM provider_circuit WHERE provider='openrouter'"
    ).fetchone()["n"] == 1


def test_retryable_server_error_keeps_uncertain_reservation(conn, monkeypatch):
    import httpx
    from openai import APIStatusError

    from eventindex import llm

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    failure = httpx.Response(502, request=request)
    calls = {"n": 0}

    def gateway_then_success(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIStatusError(
                "Bad gateway", response=failure, body={"error": {}},
            )
        return SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, cost=0.000001,
        ))

    monkeypatch.setattr(llm, "_create", gateway_then_success)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    llm._budgeted_create(
        source_id=None,
        job_id=None,
        budget_lane=None,
        model=config.MODEL_MINI,
        messages=[],
    )
    states = conn.execute(
        "SELECT state, count(*) AS n FROM budget_spend GROUP BY state"
    ).fetchall()
    assert {row["state"]: row["n"] for row in states} == {
        "settled": 1,
        "uncertain": 1,
    }


def test_missing_openrouter_key_fails_closed_before_paid_work(conn, monkeypatch):
    from eventindex import llm

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    with pytest.raises(ProviderUnavailable, match="key is missing"):
        llm.ensure_openrouter_available(probe_now=True)
    assert conn.execute(
        "SELECT count(*) AS n FROM provider_circuit WHERE provider='openrouter'"
    ).fetchone()["n"] == 1


def test_auth_rejection_opens_global_circuit(conn, monkeypatch):
    import httpx
    from openai import APIStatusError

    from eventindex import llm

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "revoked")
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(401, request=request)
    monkeypatch.setattr(
        llm,
        "_create",
        lambda **kwargs: (_ for _ in ()).throw(APIStatusError(
            "Unauthorized", response=response, body={"error": {}},
        )),
    )
    with pytest.raises(ProviderUnavailable, match="authentication rejected"):
        llm._budgeted_create(
            source_id=None,
            job_id=None,
            budget_lane=None,
            model=config.MODEL_MINI,
            messages=[],
        )
    assert conn.execute(
        "SELECT count(*) AS n FROM provider_circuit WHERE provider='openrouter'"
    ).fetchone()["n"] == 1


def test_missing_usage_keeps_full_reservation_as_uncertain(conn, monkeypatch):
    from eventindex import llm

    monkeypatch.setattr(
        llm,
        "_create",
        lambda **kwargs: SimpleNamespace(usage=None),
    )
    llm._budgeted_create(
        source_id=None,
        job_id=None,
        budget_lane=None,
        model=config.MODEL_MINI,
        messages=[],
    )
    row = conn.execute(
        "SELECT amount_eur, reserved_eur, state FROM budget_spend"
    ).fetchone()
    assert row == {
        "amount_eur": Decimal(str(config.LLM_RESERVATION_EUR_BY_MODEL[
            config.MODEL_MINI
        ])),
        "reserved_eur": Decimal("0"),
        "state": "uncertain",
    }


def test_expired_credit_circuit_probes_fail_closed(conn, monkeypatch):
    from eventindex import llm

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test")
    trip_provider_circuit("openrouter", "empty", seconds=0)
    monkeypatch.setattr(llm, "_credit_headroom_usd", lambda: 0.0)
    with pytest.raises(ProviderUnavailable):
        llm.ensure_openrouter_available()
    row = conn.execute(
        "SELECT blocked_until > now() AS blocked FROM provider_circuit "
        "WHERE provider='openrouter'"
    ).fetchone()
    assert row["blocked"]


def test_expired_credit_circuit_clears_after_topup(conn, monkeypatch):
    from eventindex import llm

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test")
    trip_provider_circuit("openrouter", "empty", seconds=0)
    monkeypatch.setattr(llm, "_credit_headroom_usd", lambda: 10.0)
    llm.ensure_openrouter_available()
    assert conn.execute(
        "SELECT count(*) AS n FROM provider_circuit"
    ).fetchone()["n"] == 0
