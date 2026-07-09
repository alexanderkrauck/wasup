"""Generate the coverage chart for the Innovationshauptplatz pitch
(private/GO-TO-MARKET.md phase 0: "this chart IS the meeting").

Queries the live DB and writes private/coverage-chart.svg: stat tiles
(index size, linztermine overlap, horizon) plus upcoming events per week
split into "nur im Event-Index" vs "auch im linztermine-Feed", with the
7-day feed cliff annotated. Refresh before every meeting.

Run: uv run python scripts/coverage_chart.py
"""

from datetime import date, timedelta

from eventindex import config, db

OUT = config.ROOT / "private" / "coverage-chart.svg"

WEEKS = 16
W, H = 1160, 760
PLOT = dict(left=72, right=1112, top=330, bottom=640)

# reference dataviz palette (validated: CVD dE 73.6; aqua < 3:1 -> direct labels)
BLUE = "#2a78d6"  # nur im Event-Index
AQUA = "#1baf7a"  # auch im linztermine-Feed
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE = "#e1e0d9", "#c3c2b7"

MONTHS = ["Jän", "Feb", "Mär", "Apr", "Mai", "Jun",
          "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def de_num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def de_date(d: date) -> str:
    return f"{d.day}. {MONTHS[d.month - 1]}"


def fetch():
    with db.connect() as conn:
        lt_sources = [r["id"] for r in conn.execute(
            "SELECT id FROM source WHERE url ILIKE '%%linztermine%%'"
        )]
        lt_events = (
            "SELECT DISTINCT i.event_id FROM identity i "
            "JOIN event_claim c ON c.fingerprint = i.fingerprint "
            "WHERE c.source_id = ANY(%(lt)s)"
        )
        one = lambda sql: next(iter(conn.execute(sql, {"lt": lt_sources}).fetchone().values()))
        total = one("SELECT count(*) FROM event")
        overlap = one(f"SELECT count(*) FROM ({lt_events}) x")
        beyond7 = one(
            "SELECT count(DISTINCT o.event_id) FROM occurrence o "
            "WHERE o.starts_at > now() + interval '7 days' "
            f"AND o.status != 'cancelled' AND o.event_id NOT IN ({lt_events})"
        )
        sources = one("SELECT count(*) FROM source WHERE status = 'active'")
        horizon_days = one(
            "SELECT floor(extract(epoch FROM max(starts_at) - now()) / 86400) "
            "FROM occurrence WHERE status != 'cancelled'"
        )
        weekly = conn.execute(
            "SELECT floor(extract(epoch FROM o.starts_at - now()) / 604800)::int AS wk, "
            f" count(DISTINCT o.event_id) FILTER (WHERE o.event_id IN ({lt_events})) AS lt, "
            f" count(DISTINCT o.event_id) FILTER (WHERE o.event_id NOT IN ({lt_events})) AS us "
            "FROM occurrence o "
            "WHERE o.starts_at BETWEEN now() AND now() + interval '%(weeks)s weeks' "
            "AND o.status != 'cancelled' GROUP BY 1 ORDER BY 1",
            {"lt": lt_sources, "weeks": WEEKS},
        ).fetchall()
    by_week = {r["wk"]: (r["lt"], r["us"]) for r in weekly}
    weeks = [by_week.get(i, (0, 0)) for i in range(WEEKS)]
    return dict(total=total, overlap=overlap, beyond7=beyond7, sources=sources,
                horizon_days=int(horizon_days), weeks=weeks)


def bar(x: float, y_base: float, h: float, width: float, fill: str,
        rounded_top: bool, title: str | None = None) -> str:
    r = min(4, h / 2) if rounded_top else 0
    y = y_base - h
    if r:
        d = (f"M{x},{y_base} V{y + r} Q{x},{y} {x + r},{y} H{x + width - r} "
             f"Q{x + width},{y} {x + width},{y + r} V{y_base} Z")
        shape = f'<path d="{d}" fill="{fill}"/>'
    else:
        shape = f'<rect x="{x}" y="{y}" width="{width}" height="{h}" fill="{fill}"/>'
    return shape if title is None else shape.replace("/>", f"><title>{title}</title></{'path' if r else 'rect'}>", 1)


def render(d: dict) -> str:
    today = date.today()
    pct = round(100 * (d["total"] - d["overlap"]) / d["total"])
    horizon_end = today + timedelta(days=d["horizon_days"])

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'viewBox="0 0 {W} {H}" font-family="system-ui, -apple-system, \'Segoe UI\', sans-serif">',
         f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>']
    t = lambda x, y, txt, size, fill, weight="normal", anchor="start": s.append(
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{txt}</text>')

    # header
    t(48, 64, "Linz Event-Index — Abdeckung im Vergleich zu linztermine.at", 24, INK, "600")
    t(48, 92, f"{de_num(d['total'])} aktuelle Veranstaltungen im Index; "
              f"{de_num(d['total'] - d['overlap'])} davon ({pct} %) sind über den "
              f"linztermine-Open-Data-Feed nicht auffindbar.", 15, INK2)

    # stat tiles (hero first)
    tiles = [
        ("Events im Index", de_num(d["total"]), 48),
        ("nur im Event-Index", f"{de_num(d['total'] - d['overlap'])}", 28),
        ("kommende Events nach dem 7-Tage-Fenster", de_num(d["beyond7"]), 28),
        ("aktive Datenquellen", de_num(d["sources"]), 28),
    ]
    x = 48
    for label, value, size in tiles:
        t(x, 150, label, 13, MUTED)
        t(x, 150 + size + 14, value, size, INK, "600")
        x += 278

    # chart title + legend
    t(48, 292, f"Kommende Veranstaltungen pro Woche (nächste {WEEKS} Wochen)", 16, INK, "600")
    lx = PLOT["right"] - 420
    s.append(f'<rect x="{lx}" y="282" width="12" height="12" rx="3" fill="{BLUE}"/>')
    t(lx + 18, 293, "nur im Linz Event-Index", 13, INK2)
    s.append(f'<rect x="{lx + 200}" y="282" width="12" height="12" rx="3" fill="{AQUA}"/>')
    t(lx + 218, 293, "auch im linztermine-Feed", 13, INK2)

    # gridlines + y ticks
    y_max = max(lt + us for lt, us in d["weeks"])
    y_max = (y_max // 100 + 1) * 100
    px = lambda v: PLOT["bottom"] - v / y_max * (PLOT["bottom"] - PLOT["top"])
    for v in range(0, y_max + 1, 100):
        y = px(v)
        color = BASELINE if v == 0 else GRID
        s.append(f'<line x1="{PLOT["left"]}" y1="{y}" x2="{PLOT["right"]}" y2="{y}" '
                 f'stroke="{color}" stroke-width="1"/>')
        t(PLOT["left"] - 10, y + 4, de_num(v), 12, MUTED, anchor="end")

    # columns (24px, centered in slot; 2px surface gap between stack segments)
    slot = (PLOT["right"] - PLOT["left"]) / WEEKS
    bw = 24
    for i, (lt, us) in enumerate(d["weeks"]):
        cx = PLOT["left"] + i * slot + (slot - bw) / 2
        wk_start = today + timedelta(weeks=i)
        title = (f"Woche ab {de_date(wk_start)}: {lt + us} Events "
                 f"({lt} auch im linztermine-Feed)")
        lt_h = lt / y_max * (PLOT["bottom"] - PLOT["top"])
        us_h = us / y_max * (PLOT["bottom"] - PLOT["top"])
        y_base = PLOT["bottom"]
        if lt_h >= 1:
            s.append(bar(cx, y_base, lt_h, bw, AQUA, rounded_top=False, title=title))
            y_base -= lt_h + 2  # surface gap
        s.append(bar(cx, y_base, us_h, bw, BLUE, rounded_top=True, title=title))
        if i % 2 == 0:
            t(cx + bw / 2, PLOT["bottom"] + 22, de_date(wk_start), 12, MUTED, anchor="middle")
        if i == 0:  # direct labels: the week the whole story hangs on
            t(cx + bw / 2, y_base - us_h - 10, de_num(lt + us), 13, INK2, "600", "middle")
            t(cx + bw / 2, PLOT["bottom"] - lt_h / 2 + 5, de_num(lt), 12, INK, "600", "middle")

    # the 7-day cliff
    cliff_x = PLOT["left"] + slot
    s.append(f'<line x1="{cliff_x}" y1="{PLOT["top"]}" x2="{cliff_x}" y2="{PLOT["bottom"]}" '
             f'stroke="{BASELINE}" stroke-width="1"/>')
    t(cliff_x + 10, PLOT["top"] + 16, "linztermine-Open-Data-Feed endet nach 7 Tagen", 13, INK2, "600")
    t(cliff_x + 10, PLOT["top"] + 34, "der Event-Index reicht weiter — bis "
      + f"{de_date(horizon_end)} {horizon_end.year}", 13, MUTED)

    # footer
    t(48, H - 28, f"Stand: {today.isoformat()} · ohne abgesagte Termine · "
      "linztermine-Zuordnung: Events, die (auch) über den linztermine-Open-Data-Feed "
      "erfasst wurden · Quelle: Linz Event-Index", 12, MUTED)

    s.append("</svg>")
    return "\n".join(s)


def main() -> None:
    data = fetch()
    OUT.write_text(render(data))
    print(f"wrote {OUT}")
    print(f"  total={data['total']} overlap={data['overlap']} "
          f"beyond7={data['beyond7']} sources={data['sources']} "
          f"horizon={data['horizon_days']}d")


if __name__ == "__main__":
    main()
