from eventindex.extract import is_placeholder_title


def test_venue_name_plus_generic_is_placeholder():
    assert is_placeholder_title("Sandburg Events", "Sandburg Linz")
    assert is_placeholder_title("Veranstaltungen im Smaragd", "CulturCafé Smaragd")
    assert is_placeholder_title("Termine", "Posthof")
    assert is_placeholder_title("", "X")


def test_real_titles_survive():
    assert not is_placeholder_title("Sommerfest im Smaragd", "CulturCafé Smaragd")
    assert not is_placeholder_title("Queen'z Garden LIVE", "Sandburg Linz")
    assert not is_placeholder_title("Klassik am Dom: Tom Jones", "Klassik am Dom")
    assert not is_placeholder_title("Flohmarkt der Stadtbibliothek", "Stadtbibliothek Linz")


def test_sanity_filter_gates_all_extraction_paths():
    """Recipe selectors bypass the cascade, so the gate must be callable on
    raw payloads - past events and placeholders never become claims."""
    from eventindex.extract import sanity_filter

    past = {"title": {"value": "Konzert", "confidence": 0.9},
            "starts_at": {"value": "2020-01-01", "confidence": 0.9}}
    placeholder = {"title": {"value": "Events", "confidence": 0.9},
                   "starts_at": {"value": "2099-01-01", "confidence": 0.9}}
    good = {"title": {"value": "Sommerkonzert", "confidence": 0.9},
            "starts_at": {"value": "2099-07-20T19:00:00+02:00", "confidence": 0.9}}
    assert sanity_filter([past, placeholder, good], {"name": "X"}) == [good]


# ------------------------------- audit 2026-07-12: claim hygiene (Block 4)

from eventindex.extract import clean_text, is_non_event, normalize_claim


def _p(**fields):
    return {k: {"value": v, "confidence": 0.9} for k, v in fields.items()}


def test_clean_text_unescapes_double_encoded_entities():
    assert clean_text("Grill &amp;amp; Chill") == "Grill & Chill"
    assert clean_text("Freibad &quot;Fest&quot;") == 'Freibad "Fest"'
    assert clean_text("WEB-C@fé &#8211; Stammtisch") == "WEB-C@fé – Stammtisch"


def test_title_loses_decor_clickbait_and_venue_suffix():
    p = normalize_claim(_p(
        title="LINZ - MAMMA MIA PARTY 💛 - FAST AUSVERKAUFT",
    ))
    assert p["title"]["value"] == "LINZ - MAMMA MIA PARTY"
    p = normalize_claim(_p(title="KinderUni Linz Linz Innenstadt",
                           venue_name="Linz Innenstadt"))
    assert p["title"]["value"] == "KinderUni Linz"
    p = normalize_claim(_p(title="Steaming Satellites - Posthof Linz",
                           venue_name="Posthof"))
    assert p["title"]["value"] == "Steaming Satellites"


def test_year_like_and_absurd_prices_are_dropped():
    p = normalize_claim(_p(
        title="Charity-Verkauf Straßenzeitung seit 1840",
        price_min=1840.0, price_max=1840.0,
    ))
    assert "price_min" not in p and "price_max" not in p
    p = normalize_claim(_p(title="Konzert", price_min=25.0, price_max=32.0))
    assert p["price_min"]["value"] == 25.0
    p = normalize_claim(_p(title="Lehrgang", price_min=830.0))
    assert "price_min" not in p  # > 500 cap


def test_non_events_are_gated():
    assert is_non_event("Sommerferien")
    assert is_non_event("schulfrei = turnfrei (Hl. Florian)")
    assert is_non_event("Anton Bruckner Universität (Hinweis auf Programm)")
    assert is_non_event("Öffnungszeiten Zoo Linz")
    assert not is_non_event("Sommerkonzert im Ferienprogramm")
