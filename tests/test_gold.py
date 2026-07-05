"""Gold-set regression (H2.3): precision@merge >= 0.98, falling precision =
blocked merge. Skips until db/gold/gold_set.csv has labels (Alexander's
labeling session; template: scripts/dump_gold_candidates.py).
"""

import csv
from pathlib import Path

import pytest

GOLD = Path(__file__).parents[1] / "db" / "gold" / "gold_set.csv"


def _labeled_rows():
    if not GOLD.exists():
        pytest.skip("gold set not labeled yet (db/gold/gold_set.csv missing)")
    rows = [r for r in csv.DictReader(GOLD.open()) if r["label"].strip() in ("s", "d")]
    if len(rows) < 20:
        pytest.skip(f"gold set has only {len(rows)} labeled rows")
    return rows


def test_precision_at_merge():
    rows = _labeled_rows()
    merged = [
        r for r in rows
        if r["system_verdict"] == "merge" or r["system_verdict"] == "adjudicate:same"
    ]
    assert merged, "no system merges in gold set"
    correct = sum(1 for r in merged if r["label"].strip() == "s")
    precision = correct / len(merged)
    wrong = [r["title_a"] for r in merged if r["label"].strip() != "s"]
    assert precision >= 0.98, (
        f"precision@merge {precision:.3f} < 0.98 - BLOCKED MERGE. "
        f"false merges: {wrong[:5]}"
    )


def test_auto_merge_never_contradicts_a_different_label():
    rows = _labeled_rows()
    false_auto = [
        r for r in rows if r["system_verdict"] == "merge" and r["label"].strip() == "d"
    ]
    assert not false_auto, f"auto-merged pairs labeled different: {false_auto[:3]}"
