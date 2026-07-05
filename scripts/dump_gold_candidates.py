"""Dump candidate claim pairs for gold-set labeling (H2.3).

Recomputes the resolver's blocking + pairwise scores and writes
db/gold/candidates.csv. Alexander fills the `label` column with s (same) /
d (different) / ? (unclear); the labeled file becomes db/gold/gold_set.csv
and runs as a regression test. Venue writes are rolled back - this script
has no side effects.

Run: uv run python scripts/dump_gold_candidates.py
"""

import csv
import hashlib
from collections import defaultdict

from eventindex import config, db
from eventindex.resolve import match
from eventindex.resolve.rebuild import _load_claims, _resolve_venues, _venue_key
from eventindex.resolve.fingerprint import VIENNA

OUT = config.ROOT / "db" / "gold" / "candidates.csv"
MIN_SCORE = 0.40  # include sub-threshold pairs so the set has real negatives


def main() -> None:
    rows = []
    with db.connect() as conn:
        with conn.transaction(force_rollback=True):
            claims = _load_claims(conn)
            _resolve_venues(conn, claims)
            verdicts = {
                r["pair_key"]: r["same_event"]
                for r in conn.execute(
                    "SELECT pair_key, same_event FROM adjudication WHERE decided_by='llm'"
                )
            }
            sources = {
                r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM source")
            }

        blocks = defaultdict(dict)
        for c in claims:
            key = (c.starts_at.astimezone(VIENNA).date(), _venue_key(c))
            blocks[key].setdefault(c.fingerprint, c)  # one representative per fp

        for block in blocks.values():
            reps = sorted(block.values(), key=lambda c: c.fingerprint)
            for i in range(len(reps)):
                for j in range(i + 1, len(reps)):
                    a, b = reps[i], reps[j]
                    if a.source_id == b.source_id:
                        continue  # same-source pairs are fingerprint business
                    score = match.pair_score(a.candidate(), b.candidate())
                    if score < MIN_SCORE:
                        continue
                    verdict = match.classify(score)
                    if verdict == match.ADJUDICATE:
                        fp_a, fp_b = sorted((a.fingerprint, b.fingerprint))
                        pk = hashlib.md5(f"{fp_a}|{fp_b}".encode()).hexdigest()
                        llm = verdicts.get(pk)
                        verdict = f"adjudicate:{'same' if llm else 'different' if llm is not None else 'uncached'}"
                    rows.append({
                        "label": "",
                        "score": round(score, 3),
                        "system_verdict": verdict,
                        "title_a": a.title, "title_b": b.title,
                        "starts_a": a.starts_at.astimezone(VIENNA).isoformat(),
                        "starts_b": b.starts_at.astimezone(VIENNA).isoformat(),
                        "venue_a": a.value("venue_name") or "",
                        "venue_b": b.value("venue_name") or "",
                        "source_a": sources.get(a.source_id, ""),
                        "source_b": sources.get(b.source_id, ""),
                        "fingerprint_a": a.fingerprint,
                        "fingerprint_b": b.fingerprint,
                    })

    rows.sort(key=lambda r: -r["score"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} candidate pairs -> {OUT}")
    print("label column: s = same event, d = different, ? = unclear")


if __name__ == "__main__":
    main()
