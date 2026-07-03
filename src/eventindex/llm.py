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
from eventindex.budget import check_budget, record_spend

S = TypeVar("S", bound=BaseModel)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        _client = OpenAI(
            base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY
        )
    return _client


def _cost_eur(usage) -> tuple[float, int, int]:
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    cost_usd = getattr(usage, "cost", None)  # OpenRouter credits, USD
    if cost_usd is not None:
        return float(cost_usd) * config.USD_TO_EUR, tokens_in, tokens_out
    est = (tokens_in + tokens_out) / 1000 * config.FALLBACK_EUR_PER_1K_TOKENS
    return est, tokens_in, tokens_out


def complete(
    tx,
    prompt: str,
    schema: type[S],
    *,
    model: str = config.MODEL_MINI,
    system: str | None = None,
    source_id: UUID | None = None,
    job_id: UUID | None = None,
) -> S:
    """One structured LLM call: budget-checked, schema-validated, ledgered.

    Retries once with the validation error appended, then raises.
    """
    check_budget(tx, source_id=source_id)

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: ValidationError | None = None
    for _ in range(2):
        response = _get_client().chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            extra_body={"usage": {"include": True}},
        )
        cost, tokens_in, tokens_out = _cost_eur(response.usage)
        record_spend(
            cost,
            "llm",
            source_id=source_id,
            job_id=job_id,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
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
