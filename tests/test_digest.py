import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from eventindex.jobs.digest import gather_stats, render

NOW = datetime(2026, 7, 3, 22, 0, tzinfo=timezone.utc)


def _stats(last_success):
    return {
        "crawls": [{"status": "ok", "n": 3, "events": 7}],
        "spend": [{"category": "llm", "eur": 0.1234, "n": 5}],
        "failed_jobs": [],
        "last_success": last_success,
    }


def test_no_dead_man_when_fresh():
    text = render(_stats(NOW - timedelta(hours=2)), NOW)
    assert "DEAD MAN" not in text
    assert "ok: 3 (events found: 7)" in text
    assert "llm: €0.1234 over 5 calls" in text
    assert "paid-provider budget today: €0.0000 actual + €0.0000 reserved / €2.40" in text


def test_paid_budget_lanes_render():
    stats = _stats(NOW) | {
        "paid_budget": [
            {"lane": "core", "actual_eur": 0.7, "reserved_eur": 0.2},
            {"lane": "recovery", "actual_eur": 0.4, "reserved_eur": 0},
        ]
    }
    text = render(stats, NOW)
    assert "€1.1000 actual + €0.2000 reserved / €2.40" in text
    assert "recovery: €0.4000 actual + €0.0000 reserved" in text


def test_dead_man_when_stale():
    text = render(_stats(NOW - timedelta(hours=49)), NOW)
    assert "DEAD MAN'S SWITCH" in text


def test_dead_man_when_never_crawled():
    text = render(_stats(None), NOW)
    assert "DEAD MAN'S SWITCH" in text
    assert "last: never" in text


def test_qa_section_renders_check_results():
    stats = _stats(NOW) | {
        "qa": [{"detail": "qa: checked=20 confirmed=18 cancelled=1 not_found=1"}]
    }
    text = render(stats, NOW)
    assert "qa: checked=20 confirmed=18" in text


def test_qa_section_flags_silence():
    assert "QA loop did not run" in render(_stats(NOW), NOW)


def test_limit_warning_screams_when_productive_source_truncated():
    stats = _stats(NOW - timedelta(hours=2))
    stats["limits_hit"] = [{"name": "linztermine (site, deep)",
                            "events_found": 426,
                            "detail": "method=recipe v2 LIMIT-TRUNCATED: state cap 100 hit"}]
    stats["budget_parked"] = [{"name": "big portal", "yield_ema": 300.0,
                               "last_error": "source monthly budget - waiting"}]
    text = render(stats, NOW)
    assert "EVENTS ARE BEING MISSED" in text
    assert "linztermine (site, deep)" in text
    assert "big portal" in text


def test_no_limit_warning_without_hits():
    text = render(_stats(NOW - timedelta(hours=2)), NOW)
    assert "EVENTS ARE BEING MISSED" not in text


def test_day_curve_anomaly_flags_capped_feed_signature():
    from datetime import date

    from eventindex.jobs.digest import day_curve_anomalies

    # three Wednesdays at ~20 events, the fourth collapses to 3: the
    # signature of a feed horizon ending mid-window
    curve = [{"day": date(2026, 7, 15), "n": 20},
             {"day": date(2026, 7, 22), "n": 21},
             {"day": date(2026, 7, 29), "n": 19},
             {"day": date(2026, 8, 5), "n": 3}]
    flags = day_curve_anomalies(curve)
    assert len(flags) == 1 and "2026-08-05" in flags[0]
    # low-volume weekdays never alert (median gate)
    quiet = [{"day": date(2026, 7, 13), "n": 2}, {"day": date(2026, 7, 20), "n": 0}]
    assert day_curve_anomalies(quiet) == []


def test_credit_outage_and_low_balance_scream():
    stats = _stats(NOW) | {
        "credit_parked": {"n": 41, "resume": NOW + timedelta(hours=1)},
        "openrouter_balance_usd": 2.5,
    }
    text = render(stats, NOW)
    assert "LLM CREDITS EMPTY: 41 jobs paused" in text
    assert "OPENROUTER BALANCE LOW: $2.50" in text


def test_healthy_balance_stays_quiet():
    stats = _stats(NOW) | {
        "credit_parked": {"n": 0, "resume": None},
        "openrouter_balance_usd": 80.0,
    }
    text = render(stats, NOW)
    assert "CREDITS EMPTY" not in text
    assert "BALANCE LOW" not in text


def test_mcp_usage_renders_partial_user_attribution():
    stats = _stats(NOW) | {
        "mcp_usage": {
            "days": 7,
            "since": NOW.date() - timedelta(days=6),
            "through": NOW.date(),
            "hashing_configured": True,
            "totals": {
                "calls": 8,
                "failures": 1,
                "observed_users": 2,
                "observed_sessions": 3,
            },
            "clients": [
                {
                    "client_family": "chatgpt",
                    "calls": 6,
                    "failures": 1,
                    "observed_users": 2,
                    "observed_sessions": 3,
                    "subject_attributed_calls": 5,
                },
                {
                    "client_family": "claude",
                    "calls": 2,
                    "failures": 0,
                    "observed_users": 0,
                    "observed_sessions": 0,
                    "subject_attributed_calls": 0,
                },
            ],
            "tools": [
                {"tool_name": "search_events", "calls": 8, "failures": 1}
            ],
        }
    }

    text = render(stats, NOW)

    assert "observed pseudonymous users=2" in text
    assert "chatgpt: calls=6" in text
    assert "subject coverage=83%" in text
    assert "claude: calls=2" in text
    assert "users unavailable" in text


def test_fetch_blocked_suspects_render():
    stats = _stats(NOW) | {"fetch_blocked": [{"name": "Stadionwelt"}]}
    text = render(stats, NOW)
    assert "FETCH-BLOCKED SUSPECTS" in text
    assert "Stadionwelt" in text


def test_field_completeness_and_hydration_render():
    stats = _stats(NOW) | {
        "field_completeness": {
            "future_events": 100,
            "stated_price": 25,
            "any_price": 90,
            "booking_without_stated_price": 7,
            "event_scale": 95,
            "identity_evidence": 40,
        },
        "hydration": {
            "unresolved": 8,
            "oldest_unresolved": NOW - timedelta(hours=6),
            "failed_24h": 2,
        },
        "verification": {
            "unresolved": 3,
            "oldest_unresolved": NOW - timedelta(hours=2),
            "supported_24h": 4,
            "contradicted_24h": 1,
            "unverified_24h": 2,
        },
    }
    text = render(stats, NOW)
    assert "stated price: 25/100 (25.0%)" in text
    assert "any price (stated or estimated): 90/100 (90.0%)" in text
    assert "event scale estimate: 95/100 (95.0%)" in text
    assert "evidence-backed identity: 40/100 (40.0%)" in text
    assert "booking URL without stated price: 7" in text
    assert "hydration jobs: 8 unresolved, oldest 6:00:00 ago" in text
    assert "risk verification: 3 unresolved, oldest 2:00:00 ago" in text
    assert "supported=4, contradicted=1, unverified=2" in text


def test_audience_readiness_violations_are_loud():
    stats = _stats(NOW) | {
        "audience_readiness": {
            "pending_events": 4,
            "oldest_pending": NOW - timedelta(hours=3),
            "orphan_pending": 1,
            "scheduled_violations": 2,
            "gender_violations": 1,
            "energy_violations": 2,
            "solo_violations": 1,
            "failed_24h": 3,
        }
    }

    text = render(stats, NOW)

    assert "AUDIENCE READINESS ALERT" in text
    assert "pending (not public): 4; oldest 3:00:00 ago" in text
    assert "pending without active estimate_audience job: 1" in text
    assert "SCHEDULED INVARIANT VIOLATIONS: 2" in text
    assert "gender=1, energy=2, solo=1" in text
    assert "failed estimate_audience jobs (24h): 3" in text


def test_healthy_audience_readiness_stays_concise():
    stats = _stats(NOW) | {
        "audience_readiness": {
            "pending_events": 0,
            "oldest_pending": None,
            "orphan_pending": 0,
            "scheduled_violations": 0,
            "gender_violations": 0,
            "energy_violations": 0,
            "solo_violations": 0,
            "failed_24h": 0,
        }
    }

    text = render(stats, NOW)

    assert "AUDIENCE READINESS ALERT" not in text
    assert "audience publication readiness: healthy" in text


def test_gather_stats_audits_batch_jobs_and_all_required_facets(conn, monkeypatch):
    monkeypatch.setattr(
        "eventindex.jobs.digest.openrouter_balance", lambda: None
    )
    queued_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    invalid_scheduled_id = uuid.uuid4()
    valid_scheduled_id = uuid.uuid4()
    valid_inferred = {
        "energy": "medium",
        "_audience_essentials": {
            "energy": {
                "value": "medium",
                "confidence": 0.35,
                "evidence": None,
            }
        },
        "solo_friendly": {
            "value": True,
            "confidence": 0.35,
            "evidence": None,
        },
    }
    invalid_inferred = {
        "energy": "high",
        "_audience_essentials": {
            "energy": {"value": "high", "confidence": "invalid"}
        },
        "solo_friendly": {"value": False, "confidence": 0},
    }

    for event_id, title, inferred, gender, gender_conf, age in (
        (queued_id, "Queued", {}, None, None, "1 hour"),
        (orphan_id, "Orphan", {}, None, None, "5 hours"),
        (
            invalid_scheduled_id,
            "Invalid scheduled",
            invalid_inferred,
            0.5,
            0,
            "2 hours",
        ),
        (
            valid_scheduled_id,
            "Valid scheduled",
            valid_inferred,
            0.5,
            0.35,
            "2 hours",
        ),
    ):
        conn.execute(
            "INSERT INTO event "
            "(id, kind, title, inferred, expected_gender_split, "
            "expected_gender_split_confidence, updated_at) "
            "VALUES (%s, 'one_off', %s, %s, %s, %s, "
            "now() - %s::interval)",
            (event_id, title, Jsonb(inferred), gender, gender_conf, age),
        )

    for event_id, status in (
        (queued_id, "pending_enrichment"),
        (orphan_id, "pending_enrichment"),
        (invalid_scheduled_id, "scheduled"),
        (invalid_scheduled_id, "scheduled"),
        (valid_scheduled_id, "scheduled"),
    ):
        conn.execute(
            "INSERT INTO occurrence (event_id, starts_at, status) "
            "VALUES (%s, now() + interval '1 day', %s)",
            (event_id, status),
        )
    conn.execute(
        "INSERT INTO jobs (kind, payload, status) "
        "VALUES ('estimate_audience', %s, 'pending')",
        (Jsonb({"event_ids": [str(queued_id)]}),),
    )
    conn.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at) "
        "VALUES ('estimate_audience', %s, 'failed', now())",
        (Jsonb({"event_ids": [str(orphan_id)]}),),
    )

    audience = gather_stats(conn)["audience_readiness"]

    assert audience["pending_events"] == 2
    assert audience["oldest_pending"] is not None
    assert audience["orphan_pending"] == 1
    assert audience["scheduled_violations"] == 1
    assert audience["gender_violations"] == 1
    assert audience["energy_violations"] == 1
    assert audience["solo_violations"] == 1
    assert audience["failed_24h"] == 1
