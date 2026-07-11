"""Incompleteness red team: per-vertical recall against independent web
ground truth.

For each vertical, one web-search call (Exa via the OpenRouter plugin,
budget-ledgered like everything else) hunts real events in the next 30
days; each found event is fuzzy-matched against canon. Misses become the
recall metric AND probe jobs for their source domains - the probe
chokepoint still decides what registers. Report: var/review/recall-<date>.md

Run: uv run python scripts/recall_redteam.py [max_verticals]
Cost: ~EUR 0.04/vertical; a full run stays under EUR 1.
"""

import re
import sys
from datetime import date, datetime, timedelta

from eventindex import config, db, llm
from eventindex.discovery.probe import domain_of, is_known, known_domains
from eventindex.discovery.sweep import _PORTAL_NOISE
from eventindex.jobs.worker import enqueue

VERTICALS = [
    "Startup, Pitch- und Gründer-Events",
    "Tech- und AI-Meetups",
    "Salsa/Bachata und Tanzabende",
    "Lauftreffs und Volksläufe",
    "Poetry Slams und Lesungen",
    "Englischsprachige / Expat-Events",
    "Senioren-Veranstaltungen",
    "Kinder-Workshops und Ferienprogramm",
    "eSports- und Gaming-Turniere",
    "Brettspiel- und Pen&Paper-Treffs",
    "Flohmärkte und Bauernmärkte",
    "Konzerte kleiner Bühnen und Open Mics",
    "Kabarett und Comedy",
    "Yoga-, Meditations- und Achtsamkeitskurse",
    "Kletter- und Boulder-Events",
    "LGBTQ+-Veranstaltungen",
    "Repair Cafés und Ehrenamt",
    "Podiumsdiskussionen und Vorträge",
    "Weinverkostungen und Kulinarik-Events",
    "Volksfeste und Vereinsfeste",
]

_LINE = re.compile(r"^\s*[-*]?\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(https?://\S+)\s*$")
_SKIP_DOMAINS = _PORTAL_NOISE + ("wasup.goedly.com",)


def _tokens(title: str) -> set[str]:
    return {t for t in re.sub(r"[^\wäöüß ]", " ", title.lower()).split() if len(t) > 2}


def _matches(title: str, canon_titles: list[str]) -> bool:
    want = _tokens(title)
    if not want:
        return False
    for have in canon_titles:
        got = _tokens(have)
        if got and len(want & got) / min(len(want), len(got)) >= 0.5:
            return True
    return False


def hunt(tx, vertical: str) -> list[tuple[str, date, str]]:
    """One web-search call -> [(title, date, url)]; malformed lines dropped."""
    today = date.today().isoformat()
    msg = llm.chat(
        tx,
        [{"role": "user", "content": (
            f"Suche im Web: {vertical} in Linz und Umgebung (25km). "
            f"Heute ist {today}. Liste NUR konkrete öffentliche Veranstaltungen "
            f"der nächsten 30 Tage mit bekanntem Datum, eine pro Zeile, Format:\n"
            f"Titel | YYYY-MM-DD | Quell-URL\n"
            f"Keine Serien ohne Datum, keine vergangenen Events, keine Erklärungen."
        )}],
        plugins=[{"id": "web", "engine": "exa", "max_results": 10}],
    )
    found = []
    for line in (msg.content or "").splitlines():
        if m := _LINE.match(line):
            try:
                d = datetime.fromisoformat(m.group(2)).date()
            except ValueError:
                continue
            if date.today() <= d <= date.today() + timedelta(days=35):
                found.append((m.group(1), d, m.group(3)))
    return found


def canon_titles_around(tx, d: date) -> list[str]:
    rows = tx.execute(
        "SELECT DISTINCT e.title FROM occurrence o JOIN event e ON e.id = o.event_id "
        "WHERE o.starts_at::date BETWEEN %s AND %s",
        (d - timedelta(days=1), d + timedelta(days=1)),
    ).fetchall()
    return [r["title"] for r in rows]


def main() -> None:
    max_verticals = int(sys.argv[1]) if len(sys.argv) > 1 else len(VERTICALS)
    lines = [f"# Recall red team - {date.today().isoformat()}", ""]
    total_found = total_hit = probes = 0
    with db.connect() as conn:
        known = known_domains(conn)
        for vertical in VERTICALS[:max_verticals]:
            try:
                found = hunt(conn, vertical)
            except Exception as e:  # budget/network: report and keep going
                lines.append(f"## {vertical}: HUNT FAILED ({e})")
                continue
            hits, misses = [], []
            for title, d, url in found:
                (hits if _matches(title, canon_titles_around(conn, d))
                 else misses).append((title, d, url))
            total_found += len(found)
            total_hit += len(hits)
            recall = f"{len(hits)}/{len(found)}" if found else "n/a (web found none)"
            lines.append(f"## {vertical}: recall {recall}")
            for title, d, url in misses:
                domain = domain_of(url)
                probed = ""
                if domain and not is_known(domain, known) \
                        and not any(n in url for n in _SKIP_DOMAINS):
                    enqueue(conn, "probe",
                            {"url": url, "discovered_via": "recall_redteam"})
                    known.add(domain)  # one probe per domain per run
                    probes += 1
                    probed = " -> probe queued"
                lines.append(f"- MISS {d} {title} <{url}>{probed}")
            lines.append("")
        conn.commit()

    lines.append(f"TOTAL: {total_hit}/{total_found} matched, {probes} probes queued")
    out = config.ROOT / "var" / "review" / f"recall-{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print("\n".join(lines[-3:]))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
