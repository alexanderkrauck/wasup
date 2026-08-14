"""The one LLM client (DECISIONS.md): OpenRouter behind the OpenAI SDK.

complete() is the only entry point. It requires a DB transaction because the
budget check and the spend ledger are part of the call - an LLM call outside a
budget context is structurally impossible.

Output is always validated against a pydantic schema; an unvalidated LLM
output reaching the DB is a bug by definition (CLAUDE.md).
"""

from typing import TypeVar
from uuid import UUID

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from eventindex import config
from eventindex.budget import (
    ProviderUnavailable,
    claim_provider_probe,
    clear_provider_circuit,
    mark_spend_uncertain,
    release_spend,
    reserve_spend,
    settle_spend,
    trip_provider_circuit,
)

S = TypeVar("S", bound=BaseModel)

_client: OpenAI | None = None
PROVIDER_TIMEOUT_SECONDS = 90.0


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        _client = OpenAI(
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            timeout=PROVIDER_TIMEOUT_SECONDS,
            # _create owns the retry policy. The SDK defaults to two hidden
            # retries with a ten-minute read timeout, which multiplied one
            # slow OpenRouter response into tens of minutes.
            max_retries=0,
        )
    return _client


def _create(**kwargs):
    """One raw SDK call; _budgeted_create owns retries and reservations."""
    return _get_client().chat.completions.create(**kwargs)


def _credit_headroom_usd() -> float:
    """Free provider probe: account balance constrained by any key limit."""
    import httpx

    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    credits = httpx.get(
        "https://openrouter.ai/api/v1/credits", headers=headers, timeout=10
    )
    credits.raise_for_status()
    data = credits.json()["data"]
    headroom = float(data["total_credits"]) - float(data["total_usage"])

    key = httpx.get(
        "https://openrouter.ai/api/v1/key", headers=headers, timeout=10
    )
    key.raise_for_status()
    key_data = key.json().get("data", {})
    if key_data.get("limit_remaining") is not None:
        headroom = min(headroom, float(key_data["limit_remaining"]))
    return headroom


def ensure_openrouter_available(*, probe_now: bool = False) -> None:
    """Honor the breaker; optionally verify headroom before another paid API."""
    if not config.OPENROUTER_API_KEY:
        blocked_until = trip_provider_circuit(
            "openrouter", "OPENROUTER_API_KEY is missing", seconds=24 * 3600
        )
        raise ProviderUnavailable(
            "OpenRouter API key is missing; failing closed",
            provider="openrouter",
            blocked_until=blocked_until,
        )
    should_probe = claim_provider_probe("openrouter")
    if not should_probe and not probe_now:
        return
    try:
        headroom = _credit_headroom_usd()
    except Exception as exc:
        blocked_until = trip_provider_circuit(
            "openrouter", f"credit probe failed: {type(exc).__name__}"
        )
        raise ProviderUnavailable(
            "OpenRouter credit state unavailable; failing closed",
            provider="openrouter",
            blocked_until=blocked_until,
        ) from exc
    if headroom < config.OPENROUTER_RESUME_MIN_USD:
        blocked_until = trip_provider_circuit(
            "openrouter", f"credits empty (${headroom:.4f} available)"
        )
        raise ProviderUnavailable(
            f"OpenRouter credits empty (${headroom:.4f} available)",
            provider="openrouter",
            blocked_until=blocked_until,
        )
    clear_provider_circuit("openrouter")


def _budgeted_create(
    *,
    source_id: UUID | None,
    job_id: UUID | None,
    budget_lane: str | None,
    model: str,
    reservation_eur: float | None = None,
    **kwargs,
):
    """Provider call with one atomic reservation per physical attempt."""
    import json
    import time

    from openai import APIConnectionError, APIStatusError

    ensure_openrouter_available()
    reviewed_max_eur = config.LLM_RESERVATION_EUR_BY_MODEL.get(
        model, config.LLM_UNKNOWN_MODEL_RESERVATION_EUR
    )
    max_eur = reviewed_max_eur if reservation_eur is None else reservation_eur
    if max_eur <= 0 or max_eur > reviewed_max_eur:
        raise ValueError(
            "request reservation must be positive and no greater than the "
            f"reviewed {model} maximum of EUR {reviewed_max_eur}"
        )
    max_price = config.LLM_MAX_PRICE_USD_PER_M_BY_MODEL.get(model)
    if max_price is not None:
        extra_body = dict(kwargs.get("extra_body") or {})
        provider = dict(extra_body.get("provider") or {})
        provider["max_price"] = max_price
        extra_body["provider"] = provider
        kwargs["extra_body"] = extra_body
    last: Exception | None = None
    for attempt in range(3):
        reservation = reserve_spend(
            max_eur,
            "llm",
            source_id=source_id,
            job_id=job_id,
            lane=budget_lane,
            provider="openrouter",
            detail=f"maximum reservation for {model}",
        )
        try:
            response = _create(model=model, **kwargs)
        except (json.JSONDecodeError, APIConnectionError) as e:
            mark_spend_uncertain(
                reservation,
                f"ambiguous OpenRouter attempt {attempt + 1}: {type(e).__name__}",
            )
            last = e
        except APIStatusError as e:
            body = getattr(e, "body", None) or {}
            error_body = body.get("error", body) if isinstance(body, dict) else {}
            provider_code = (
                error_body.get("code") if isinstance(error_body, dict) else None
            )
            exhausted = (
                e.status_code == 402
                or provider_code == "token_limit_exceeded"
                or "insufficient credit" in str(e).lower()
            )
            # A timeout or gateway/server failure may arrive after prompt
            # processing and therefore may still be billed. Capacity is more
            # valuable than optimistic accounting: retain the full amount.
            if e.status_code in (408, 500, 502, 503, 504):
                mark_spend_uncertain(
                    reservation,
                    f"ambiguous OpenRouter HTTP {e.status_code} attempt "
                    f"{attempt + 1}",
                )
                last = e
                time.sleep(5 * (attempt + 1))
                continue
            # Authentication, validation, credit and rate-limit responses are
            # definite pre-generation rejections.
            release_spend(reservation)
            if exhausted:
                blocked_until = trip_provider_circuit(
                    "openrouter", "credits or daily key limit exhausted"
                )
                raise ProviderUnavailable(
                    "OpenRouter credits empty or daily key limit reached",
                    provider="openrouter",
                    blocked_until=blocked_until,
                ) from e
            if e.status_code in (401, 403):
                blocked_until = trip_provider_circuit(
                    "openrouter", f"authentication rejected (HTTP {e.status_code})"
                )
                raise ProviderUnavailable(
                    "OpenRouter authentication rejected; failing closed",
                    provider="openrouter",
                    blocked_until=blocked_until,
                ) from e
            if e.status_code != 429:
                raise
            last = e
        except Exception as e:
            mark_spend_uncertain(
                reservation,
                f"ambiguous OpenRouter failure: {type(e).__name__}",
            )
            raise
        else:
            cost_info = _cost_eur(getattr(response, "usage", None))
            if cost_info is None:
                mark_spend_uncertain(
                    reservation,
                    "OpenRouter response omitted cost and token usage",
                )
                return response
            cost, tokens_in, tokens_out = cost_info
            overrun = settle_spend(
                reservation,
                cost,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            if overrun:
                # The response is usable and paid for; retain it, but stop any
                # further calls until pricing/reservation bounds are reviewed.
                trip_provider_circuit(
                    "openrouter",
                    f"provider cost exceeded €{max_eur:.2f} reservation for {model}",
                    seconds=24 * 3600,
                )
            return response
        time.sleep(5 * (attempt + 1))
    raise last


def _cost_eur(usage) -> tuple[float, int, int] | None:
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    cost_usd = getattr(usage, "cost", None)  # OpenRouter credits, USD
    if cost_usd is not None:
        return float(cost_usd) * config.USD_TO_EUR, tokens_in, tokens_out
    if tokens_in == 0 and tokens_out == 0:
        return None
    est = (tokens_in + tokens_out) / 1000 * config.FALLBACK_EUR_PER_1K_TOKENS
    return est, tokens_in, tokens_out


def chat(
    tx,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    model: str = config.MODEL_MINI,
    source_id: UUID | None = None,
    job_id: UUID | None = None,
    plugins: list[dict] | None = None,
    budget_lane: str | None = None,
):
    """One raw chat turn (optionally with tools / OpenRouter plugins):
    budget-checked, ledgered. Returns the assistant message. The agent loop
    and the search fan-out build on this - like complete(), it cannot bypass
    the budget."""
    kwargs: dict = {}
    if tools:
        kwargs["tools"] = tools
    extra_body: dict = {"usage": {"include": True}}
    if plugins:
        extra_body["plugins"] = plugins
    response = _budgeted_create(
        source_id=source_id,
        job_id=job_id,
        budget_lane=budget_lane,
        model=model,
        messages=messages,
        max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
        extra_body=extra_body,
        **kwargs,
    )
    return response.choices[0].message


def complete(
    tx,
    prompt: str,
    schema: type[S],
    *,
    model: str = config.MODEL_MINI,
    system: str | None = None,
    source_id: UUID | None = None,
    job_id: UUID | None = None,
    images: list[str] | None = None,
    budget_lane: str | None = None,
    max_tokens: int | None = None,
    reservation_eur: float | None = None,
    reasoning_effort: str | None = None,
) -> S:
    """One structured LLM call: budget-checked, schema-validated, ledgered.

    images: data URLs attached to the user turn (vision path, fence fired
    2026-07-20) - the model must be multimodal (config.MODEL_VISION).
    Retries once with the validation error appended, then raises.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if images:
        messages.append({"role": "user", "content": (
            [{"type": "text", "text": prompt}]
            + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        )})
    else:
        messages.append({"role": "user", "content": prompt})

    last_error: ValidationError | None = None
    for _ in range(2):
        extra_body: dict = {"usage": {"include": True}}
        if reasoning_effort is not None:
            if reasoning_effort not in {
                "none", "minimal", "low", "medium", "high", "xhigh", "max",
            }:
                raise ValueError(f"unsupported reasoning effort {reasoning_effort!r}")
            extra_body["reasoning"] = {"effort": reasoning_effort}
        response = _budgeted_create(
            source_id=source_id,
            job_id=job_id,
            budget_lane=budget_lane,
            model=model,
            messages=messages,
            max_tokens=max_tokens or config.LLM_MAX_OUTPUT_TOKENS,
            reservation_eur=reservation_eur,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            extra_body=extra_body,
        )
        content = response.choices[0].message.content or ""
        try:
            return schema.model_validate_json(content)
        except ValidationError as e:
            last_error = e
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": f"Your output failed validation:\n{e}\n"
                    "Return corrected JSON matching the schema exactly.",
                }
            )
    raise last_error
