from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from mcp import types

from eventindex import config
from eventindex.api.mcp_usage import (
    purge_old_usage,
    record_mcp_call,
    render_usage_report,
    usage_dimensions,
    usage_report,
)

VIENNA = ZoneInfo("Europe/Vienna")
NOW = datetime(2026, 8, 27, 10, 30, tzinfo=VIENNA)


def _context(meta=None, *, client_name=None, user_agent=None):
    meta_model = types.RequestParams.Meta.model_validate(meta or {})
    headers = {"user-agent": user_agent} if user_agent else {}
    request_context = SimpleNamespace(
        meta=meta_model,
        request=SimpleNamespace(headers=headers),
    )
    params = None
    if client_name:
        params = SimpleNamespace(
            clientInfo=SimpleNamespace(name=client_name, version="test")
        )
    return SimpleNamespace(
        request_context=request_context,
        session=SimpleNamespace(client_params=params),
    )


def test_dimensions_hash_only_documented_openai_ids(monkeypatch):
    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "k" * 32)
    dimensions = usage_dimensions(_context({
        "openai/userAgent": "ChatGPT/1.0",
        "openai/subject": "raw-user-id",
        "openai/session": "raw-session-id",
        "openai/organization": "ignored-org",
        "openai/userLocation": {"city": "Linz"},
    }))

    assert dimensions.client_family == "chatgpt"
    assert len(dimensions.subject_digest) == 64
    assert len(dimensions.session_digest) == 64
    assert "raw-user-id" not in dimensions.subject_digest
    assert dimensions.subject_digest != dimensions.session_digest
    assert not hasattr(dimensions, "organization_digest")


def test_openai_actor_outweighs_generic_user_agent(monkeypatch):
    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "k" * 32)
    dimensions = usage_dimensions(_context({
        "openai/userAgent": "Apps SDK Connector/2026",
        "openai/subject": "anonymous-user",
    }))

    assert dimensions.client_family == "chatgpt"
    assert len(dimensions.subject_digest) == 64


def test_dimensions_support_current_mcp_and_legacy_client_hints(monkeypatch):
    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "")

    current = usage_dimensions(_context({
        "io.modelcontextprotocol/clientInfo": {
            "name": "Claude Code",
            "version": "1.2.3",
        }
    }))
    legacy = usage_dimensions(_context(client_name="Codex CLI"))
    header_fallback = usage_dimensions(_context(user_agent="Anthropic-MCP/1"))

    assert current.client_family == "claude"
    assert legacy.client_family == "codex"
    assert header_fallback.client_family == "claude"
    assert current.subject_digest == current.session_digest == ""


def test_missing_or_short_hash_key_never_falls_back_to_raw(monkeypatch):
    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "too-short")
    dimensions = usage_dimensions(_context({
        "openai/subject": "subject-stays-out-of-storage",
        "openai/session": "session-stays-out-of-storage",
    }))

    assert dimensions.client_family == "chatgpt"
    assert dimensions.subject_digest == ""
    assert dimensions.session_digest == ""


def test_daily_upsert_report_and_retention(conn, monkeypatch):
    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "secret-key-" * 4)
    chat = _context({
        "openai/subject": "chatgpt-user-a",
        "openai/session": "conversation-a",
    })
    second_session = _context({
        "openai/subject": "chatgpt-user-a",
        "openai/session": "conversation-b",
    })
    claude = _context({
        "io.modelcontextprotocol/clientInfo": {
            "name": "Claude Desktop",
            "version": "0.1",
        }
    })

    record_mcp_call(chat, "search_events", failed=False, now=NOW)
    record_mcp_call(chat, "search_events", failed=True, now=NOW)
    record_mcp_call(
        second_session, "get_event", failed=False, now=NOW
    )
    record_mcp_call(claude, "search_events", failed=False, now=NOW)

    rows = conn.execute(
        "SELECT * FROM mcp_usage_daily ORDER BY client_family, tool_name, "
        "session_digest"
    ).fetchall()
    assert len(rows) == 3
    merged = next(
        row for row in rows
        if row["client_family"] == "chatgpt" and row["call_count"] == 2
    )
    assert merged["failure_count"] == 1
    assert all("chatgpt-user-a" not in str(row) for row in rows)
    assert all("conversation-a" not in str(row) for row in rows)

    report = usage_report(conn, days=7, today=NOW.date())
    assert report["totals"] == {
        "calls": 4,
        "failures": 1,
        "observed_users": 1,
        "observed_sessions": 2,
    }
    by_client = {row["client_family"]: row for row in report["clients"]}
    assert by_client["chatgpt"]["calls"] == 3
    assert by_client["chatgpt"]["observed_users"] == 1
    assert by_client["claude"]["calls"] == 1
    assert by_client["claude"]["observed_users"] == 0
    rendered = render_usage_report(report)
    assert "chatgpt: calls=3" in rendered
    assert "claude: calls=1" in rendered
    assert "users unavailable" in rendered
    assert "chatgpt-user-a" not in rendered

    conn.execute(
        "INSERT INTO mcp_usage_daily "
        "(usage_date, client_family, tool_name, call_count) "
        "VALUES (%s, 'unknown', 'search', 1)",
        (NOW.date() - timedelta(days=30),),
    )
    assert purge_old_usage(conn, today=NOW.date()) == 1


def test_recording_failure_never_breaks_tool(monkeypatch):
    class BrokenDB:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("db unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(config, "MCP_USAGE_HMAC_KEY", "k" * 32)
    monkeypatch.setattr("eventindex.api.mcp_usage.db.connect", BrokenDB)

    record_mcp_call(
        _context({"openai/subject": "safe"}),
        "search_events",
        failed=False,
        now=NOW,
    )

    def broken_dimensions(_context):
        raise RuntimeError("unexpected metadata shape")

    monkeypatch.setattr(
        "eventindex.api.mcp_usage.usage_dimensions", broken_dimensions
    )
    record_mcp_call(
        _context({"openai/subject": "safe"}),
        "search_events",
        failed=False,
        now=NOW,
    )
