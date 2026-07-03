from datetime import datetime
from zoneinfo import ZoneInfo

from eventindex.resolve.fingerprint import fingerprint, geo_cell, normalize_title

VIENNA = ZoneInfo("Europe/Vienna")
DT = datetime(2026, 7, 10, 20, 0, tzinfo=VIENNA)


def test_normalize_folds_umlauts_dates_stopwords():
    assert normalize_title("Konzert im Brucknerhaus am 10.07.2026") == (
        "konzert brucknerhaus"
    )
    assert normalize_title("GRÜNMARKT Urfahr") == "gruenmarkt urfahr"


def test_same_event_same_fingerprint_despite_formatting():
    a = fingerprint("Jazz-Abend: Die Nacht", DT, lat=48.31, lon=14.29)
    b = fingerprint("JAZZ ABEND — die NACHT!", DT, lat=48.3101, lon=14.2899)
    assert a == b


def test_different_day_different_fingerprint():
    other_day = DT.replace(day=11)
    assert fingerprint("Jazz Abend", DT) != fingerprint("Jazz Abend", other_day)


def test_utc_datetime_buckets_to_vienna_day():
    # 2026-07-10 23:00 UTC is already July 11 in Vienna
    utc_late = datetime(2026, 7, 10, 23, 0, tzinfo=ZoneInfo("UTC"))
    assert "2026-07-11" in fingerprint("x", utc_late)


def test_geo_cell_distinguishes_across_town_not_next_door():
    linz_center = geo_cell(48.3069, 14.2858)
    next_door = geo_cell(48.3071, 14.2861)
    across_town = geo_cell(48.33, 14.32)
    assert linz_center == next_door
    assert linz_center != across_town
    assert geo_cell(None, None) == ""


def test_venue_id_beats_geo_cell():
    fp = fingerprint("Jazz Abend", DT, lat=48.31, lon=14.29, venue_id="abc-123")
    assert fp.endswith("|abc-123")
