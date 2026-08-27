"""Privacy-preserving aggregate monitoring for MCP tool calls.

Only allowlisted protocol hints are inspected. Raw identifiers, client
headers, tool arguments, prompts, filters, results, IPs, and locations are
never persisted. ChatGPT's documented pseudonymous subject/session hints are
HMACed before storage; clients without such a hint remain unattributed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from eventindex import config, db

log = logging.getLogger("eventindex.mcp_usage")

ClientFamily = Literal["chatgpt", "claude", "codex", "other", "unknown"]
KNOWN_TOOLS = {
    "search_events",
    "get_event",
    "get_calendar_link",
    "search",
    "fetch",
}
UNKNOWN_TOOL = "unknown_tool"
_EMPTY_DIGEST = ""
_MAX_HINT_CHARS = 1024
_VIENNA = ZoneInfo(config.TIMEZONE)


@dataclass(frozen=True)
class UsageDimensions:
    client_family: ClientFamily
    subject_digest: str = _EMPTY_DIGEST
    session_digest: str = _EMPTY_DIGEST


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        result = dumped(by_alias=True, exclude_none=True)
        return result if isinstance(result, dict) else {}
    return {}


def _bounded_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_HINT_CHARS:
        return None
    return value


def _client_family(value: Any) -> ClientFamily:
    hint = _bounded_string(value)
    if hint is None:
        return "unknown"
    lowered = hint.casefold()
    if "codex" in lowered:
        return "codex"
    if "chatgpt" in lowered or "openai" in lowered:
        return "chatgpt"
    if "claude" in lowered or "anthropic" in lowered:
        return "claude"
    return "other"


def _request_meta(context: Any) -> tuple[Any | None, dict[str, Any]]:
    try:
        request_context = context.request_context
    except (AttributeError, ValueError):
        return None, {}
    meta = getattr(request_context, "meta", None)
    if meta is None:
        return request_context, {}
    extra = getattr(meta, "model_extra", None)
    if not isinstance(extra, dict):
        extra = _mapping(meta)
    return request_context, extra


def _legacy_client_name(context: Any) -> str | None:
    """Legacy MCP exposes clientInfo on a stateful initialized session."""
    try:
        params = context.session.client_params
    except (AttributeError, ValueError):
        return None
    info = getattr(params, "clientInfo", None) if params is not None else None
    if info is None and params is not None:
        info = getattr(params, "client_info", None)
    if info is None:
        return None
    if isinstance(info, dict):
        return _bounded_string(info.get("name"))
    return _bounded_string(getattr(info, "name", None))


def _http_client_name(request_context: Any) -> str | None:
    """Best-effort software family only; the raw HTTP User-Agent is discarded."""
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        return _bounded_string(headers.get("user-agent"))
    except (AttributeError, TypeError):
        return None


def _digest(kind: str, value: Any) -> str:
    raw = _bounded_string(value)
    key = config.MCP_USAGE_HMAC_KEY
    if raw is None or len(key.encode("utf-8")) < 32:
        return _EMPTY_DIGEST
    message = f"mcp-usage-v1:{kind}\0{raw}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def usage_dimensions(context: Any) -> UsageDimensions | None:
    """Extract only normalized/hashed, documented metadata from one call."""
    request_context, meta = _request_meta(context)
    if request_context is None:
        return None

    openai_agent = meta.get("openai/userAgent")
    modern_info = _mapping(meta.get("io.modelcontextprotocol/clientInfo"))
    modern_name = modern_info.get("name")
    legacy_name = _legacy_client_name(context)
    http_name = _http_client_name(request_context)
    openai_family = _client_family(openai_agent)
    has_openai_actor = (
        _bounded_string(meta.get("openai/subject")) is not None
        or _bounded_string(meta.get("openai/session")) is not None
    )

    if openai_family in {"chatgpt", "codex"}:
        family = openai_family
    elif has_openai_actor:
        # Documented OpenAI actor metadata is more authoritative than a
        # generic or changed user-agent label.
        family = "chatgpt"
    elif _bounded_string(modern_name) is not None:
        family = _client_family(modern_name)
    elif legacy_name is not None:
        family = _client_family(legacy_name)
    elif http_name is not None:
        # A header is an unverified software hint, never a user identity.
        family = _client_family(http_name)
    else:
        family = "unknown"

    return UsageDimensions(
        client_family=family,
        subject_digest=_digest("openai-subject", meta.get("openai/subject")),
        session_digest=_digest("openai-session", meta.get("openai/session")),
    )


def _metric_tool_name(tool_name: str) -> str:
    return tool_name if tool_name in KNOWN_TOOLS else UNKNOWN_TOOL


def purge_old_usage(conn, *, today=None) -> int:
    today = today or datetime.now(_VIENNA).date()
    cutoff = today - timedelta(days=config.MCP_USAGE_RETENTION_DAYS - 1)
    cursor = conn.execute(
        "DELETE FROM mcp_usage_daily WHERE usage_date < %s", (cutoff,)
    )
    return cursor.rowcount


def record_mcp_call(
    context: Any,
    tool_name: str,
    *,
    failed: bool,
    now: datetime | None = None,
) -> None:
    """Aggregate one call. Monitoring failure must never fail the MCP tool."""
    try:
        dimensions = usage_dimensions(context)
        if dimensions is None:
            return
        at = (now or datetime.now(_VIENNA)).astimezone(_VIENNA)
        # Telemetry is secondary to serving the tool. Bound connection setup,
        # statement execution, and lock waiting; the caller also runs this off
        # the MCP event loop.
        with db.connect(
            connect_timeout=1,
            options="-c statement_timeout=1000 -c lock_timeout=500",
        ) as conn:
            purge_old_usage(conn, today=at.date())
            conn.execute(
                """
                INSERT INTO mcp_usage_daily (
                    usage_date, client_family, tool_name,
                    subject_digest, session_digest,
                    call_count, failure_count
                ) VALUES (%s, %s, %s, %s, %s, 1, %s)
                ON CONFLICT (
                    usage_date, client_family, tool_name,
                    subject_digest, session_digest
                ) DO UPDATE SET
                    call_count = mcp_usage_daily.call_count + 1,
                    failure_count = mcp_usage_daily.failure_count
                        + EXCLUDED.failure_count
                """,
                (
                    at.date(),
                    dimensions.client_family,
                    _metric_tool_name(tool_name),
                    dimensions.subject_digest,
                    dimensions.session_digest,
                    int(failed),
                ),
            )
    except Exception as exc:  # monitoring is deliberately fail-open
        log.warning("MCP usage monitoring unavailable (%s)", type(exc).__name__)


def usage_report(conn, *, days: int = 7, today=None) -> dict[str, Any]:
    if not 1 <= days <= config.MCP_USAGE_RETENTION_DAYS:
        raise ValueError(
            f"days must be between 1 and {config.MCP_USAGE_RETENTION_DAYS}"
        )
    today = today or datetime.now(_VIENNA).date()
    since = today - timedelta(days=days - 1)
    params = (since, today)
    clients = conn.execute(
        """
        SELECT client_family,
               sum(call_count)::bigint AS calls,
               sum(failure_count)::bigint AS failures,
               count(DISTINCT nullif(subject_digest, ''))::bigint
                   AS observed_users,
               count(DISTINCT nullif(session_digest, ''))::bigint
                   AS observed_sessions,
               coalesce(sum(call_count) FILTER (
                   WHERE subject_digest <> ''
               ), 0)::bigint AS subject_attributed_calls
        FROM mcp_usage_daily
        WHERE usage_date BETWEEN %s AND %s
        GROUP BY client_family
        ORDER BY calls DESC, client_family
        """,
        params,
    ).fetchall()
    tools = conn.execute(
        """
        SELECT tool_name,
               sum(call_count)::bigint AS calls,
               sum(failure_count)::bigint AS failures
        FROM mcp_usage_daily
        WHERE usage_date BETWEEN %s AND %s
        GROUP BY tool_name
        ORDER BY calls DESC, tool_name
        """,
        params,
    ).fetchall()
    totals = conn.execute(
        """
        SELECT coalesce(sum(call_count), 0)::bigint AS calls,
               coalesce(sum(failure_count), 0)::bigint AS failures,
               count(DISTINCT nullif(subject_digest, ''))::bigint
                   AS observed_users,
               count(DISTINCT nullif(session_digest, ''))::bigint
                   AS observed_sessions
        FROM mcp_usage_daily
        WHERE usage_date BETWEEN %s AND %s
        """,
        params,
    ).fetchone()
    return {
        "days": days,
        "since": since,
        "through": today,
        "hashing_configured": len(config.MCP_USAGE_HMAC_KEY.encode("utf-8")) >= 32,
        "totals": totals,
        "clients": clients,
        "tools": tools,
    }


def render_usage_report(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        f"MCP usage — {report['since']} through {report['through']} "
        f"({report['days']} Vienna days)",
        f"calls={totals['calls']}, failures={totals['failures']}, "
        f"observed pseudonymous users={totals['observed_users']}, "
        f"observed sessions={totals['observed_sessions']}",
    ]
    if not report["hashing_configured"]:
        lines.append(
            "! user/session attribution disabled: set MCP_USAGE_HMAC_KEY "
            "to at least 32 secret bytes"
        )
    lines.append("clients (self-reported/best-effort; never authentication):")
    if not report["clients"]:
        lines.append("  no MCP tool calls")
    for row in report["clients"]:
        calls = int(row["calls"] or 0)
        attributed = int(row["subject_attributed_calls"] or 0)
        users = int(row["observed_users"] or 0)
        sessions = int(row["observed_sessions"] or 0)
        user_text = (
            f"observed users={users}, subject coverage={attributed / calls:.0%}"
            if attributed
            else "users unavailable"
        )
        lines.append(
            f"  {row['client_family']}: calls={calls}, failures="
            f"{int(row['failures'] or 0)}, {user_text}, sessions={sessions}"
        )
    lines.append("tools:")
    if not report["tools"]:
        lines.append("  no MCP tool calls")
    for row in report["tools"]:
        lines.append(
            f"  {row['tool_name']}: calls={int(row['calls'] or 0)}, "
            f"failures={int(row['failures'] or 0)}"
        )
    return "\n".join(lines) + "\n"
