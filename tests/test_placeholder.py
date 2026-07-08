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
