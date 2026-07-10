"""Recall red team: the deterministic core (line parser + fuzzy matcher).
The web hunt itself is LLM and runs only via the script."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from recall_redteam import _LINE, _matches  # noqa: E402


def test_line_parser_accepts_the_asked_format_only():
    m = _LINE.match("- Startup Pitch Night | 2026-07-20 | https://tech2b.at/x")
    assert m and m.group(2) == "2026-07-20"
    assert _LINE.match("Pitch Night | 2026-07-20 | https://a.at/x")  # no bullet ok
    assert _LINE.match("Es gibt viele Events in Linz.") is None
    assert _LINE.match("Titel | irgendwann | https://a.at") is None


def test_fuzzy_match_tolerates_rewording_but_not_different_events():
    canon = ["Global AI Hackathon Linz", "Salsa Night im OK Linz"]
    assert _matches("AI Hackathon (Global), Tabakfabrik", canon)
    assert _matches("Salsa Night", canon)
    assert not _matches("Bachata Workshop Anfänger", canon)
    assert not _matches("", canon)
    # short/stopword-only titles never match by accident
    assert not _matches("im am", canon)


def test_match_is_date_scoped_by_construction():
    # the matcher only sees canon titles from ±1 day (canon_titles_around);
    # this pins the contract that matching is title-level, not date-level
    assert _matches("AI Hackathon", ["Global AI Hackathon Linz"])
    assert not _matches("AI Hackathon", [])
