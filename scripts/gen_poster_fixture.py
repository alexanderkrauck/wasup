"""Regenerate tests/fixtures/poster.png: a legible synthetic event poster
(the vision gold-set class). Run: uv run python scripts/gen_poster_fixture.py"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LINES = [
    ("SOMMER IM HOF 2030", 64),
    ("", 30),
    ("Fr 07.08.2030  19:30", 44),
    ("Sommerkonzert der Stadtkapelle", 40),
    ("", 24),
    ("Sa 15.08.2030  20:00", 44),
    ("Open-Air-Kino: Casablanca", 40),
    ("", 24),
    ("So 30.08.2030  10:00", 44),
    ("Familienbrunch mit Flohmarkt", 40),
    ("", 30),
    ("Innenhof Museumstrasse 12, Linz", 32),
    ("Eintritt frei", 32),
]


def main() -> None:
    img = Image.new("RGB", (1000, 1200), "#f5efe0")
    draw = ImageDraw.Draw(img)
    y = 80
    for text, size in LINES:
        if text:
            font = ImageFont.load_default(size=size)
            w = draw.textlength(text, font=font)
            draw.text(((1000 - w) / 2, y), text, fill="#1a1a2e", font=font)
        y += size + 18
    out = Path(__file__).parents[1] / "tests" / "fixtures" / "poster.png"
    img.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
