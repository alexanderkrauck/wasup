"""Static release gate for the MCP calling-agent evaluation corpus."""

import json

from eventindex import config


def test_mcp_tool_use_gold_is_broad_and_machine_gradable():
    cases = json.loads(
        (config.ROOT / "db/gold/mcp_tool_use.json").read_text()
    )
    assert len(cases) >= 30
    assert {
        case["expected_tool"] for case in cases
    } >= {"search_events", "search", "get_event", "get_calendar_link", "none"}
    for case in cases:
        assert set(case) == {
            "task", "expected_tool", "critical_args",
            "forbidden_tools", "max_calls",
        }
        assert case["task"] and isinstance(case["critical_args"], dict)
        assert isinstance(case["forbidden_tools"], list)
        assert isinstance(case["max_calls"], int) and case["max_calls"] >= 0


def test_gold_covers_joint_tags_hard_soft_price_scale_and_calendar():
    text = (config.ROOT / "db/gold/mcp_tool_use.json").read_text().lower()
    for required in (
        '"dance","elegant"',
        "preferred_max_price",
        '"max_price"',
        "participant_count_min",
        "required_attributes",
        "get_calendar_link",
        "get_event only after selection",
    ):
        assert required in text
