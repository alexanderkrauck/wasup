"""Precision + pipeline-loss audit (the recall red team's twin).

Section 1 - locality: events that exist ONLY on global aggregators with no
venue, no geo, and a foreign event URL - the "online event padded into the
Linz listing" signature (found live via Eventbrite, 2026-07-10). Plus any
event whose geo sits outside the Linz bbox.

Section 2 - aggregator->canon diff: recent aggregator claims that never
resolved into an event (silent pipeline loss).

Pure SQL, zero LLM, safe to run anytime:
uv run python scripts/index_audit.py
"""

from datetime import date

from eventindex import config, db

AGGREGATORS = r"linztermine|eventbrite|eventfinder|tips\.at Linz|meinbezirk"
BBOX = {"lat_min": 48.08, "lat_max": 48.53, "lon_min": 13.95, "lon_max": 14.63}


def placeless_aggregator_events(conn) -> list[dict]:
    return conn.execute(
        f"""
        SELECT e.id, e.title, e.url,
               array_agg(DISTINCT s.name) AS sources
        FROM event e
        JOIN identity i ON i.event_id = e.id
        JOIN event_claim c ON c.fingerprint = i.fingerprint
        JOIN source s ON s.id = c.source_id
        WHERE e.venue_id IS NULL AND e.geo IS NULL
          AND EXISTS (SELECT 1 FROM occurrence o WHERE o.event_id = e.id
                      AND o.starts_at > now())
        GROUP BY e.id
        HAVING bool_and(s.name ~* '{AGGREGATORS}')
        ORDER BY e.title
        """
    ).fetchall()


def out_of_bbox_events(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT e.id, e.title, ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon
        FROM event e
        WHERE e.geo IS NOT NULL AND NOT (
            ST_Y(e.geo) BETWEEN %(lat_min)s AND %(lat_max)s
            AND ST_X(e.geo) BETWEEN %(lon_min)s AND %(lon_max)s)
        """,
        BBOX,
    ).fetchall()


def unresolved_aggregator_claims(conn) -> list[dict]:
    return conn.execute(
        f"""
        SELECT s.name AS source, count(*) AS lost,
               min(c.payload->'title'->>'value') AS sample_title
        FROM event_claim c JOIN source s ON s.id = c.source_id
        WHERE s.name ~* '{AGGREGATORS}'
          AND c.extracted_at > now() - interval '7 days'
          AND NOT EXISTS (SELECT 1 FROM identity i
                          WHERE i.fingerprint = c.fingerprint)
        GROUP BY s.name ORDER BY lost DESC
        """
    ).fetchall()


def main() -> None:
    lines = [f"# Index audit - {date.today().isoformat()}", ""]
    with db.connect() as conn:
        junk = placeless_aggregator_events(conn)
        lines.append(f"## Aggregator-only events with zero locality evidence: {len(junk)}")
        lines.append("(candidates for a rebuild-side locality gate - see OPEN-QUESTIONS)")
        for r in junk:
            from urllib.parse import urlparse

            host = urlparse(r["url"] or "").netloc
            foreign = "" if host.endswith(".at") else " [foreign URL]"
            lines.append(f"- {r['title'][:70]}{foreign} <{r['url']}> ({', '.join(r['sources'])})")
        lines.append("")

        outside = out_of_bbox_events(conn)
        lines.append(f"## Events with geo OUTSIDE the Linz bbox: {len(outside)}")
        for r in outside:
            lines.append(f"- {r['title'][:70]} ({r['lat']:.3f}, {r['lon']:.3f})")
        lines.append("")

        lost = unresolved_aggregator_claims(conn)
        lines.append("## Aggregator claims (7d) that never resolved into canon")
        if lost:
            for r in lost:
                lines.append(f"- {r['source']}: {r['lost']} claims "
                             f"(e.g. {r['sample_title']!r})")
        else:
            lines.append("- none: every recent aggregator claim reached canon")

    out = config.ROOT / "var" / "review" / f"index-audit-{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
