"""Sun, Moon and planet positions checked against well-known events."""
from datetime import datetime, timezone

from massive_collate.gui import astro, world


def _body(sky, name):
    return next(b for b in sky["bodies"] if b["id"] == name)


def _sep(sky, a, b):
    x, y = _body(sky, a), _body(sky, b)
    return astro.separation(x["ra"], x["dec"], y["ra"], y["dec"])


def test_total_solar_eclipse_2024_04_08():
    s = astro.sky(datetime(2024, 4, 8, 18, 18, tzinfo=timezone.utc))  # greatest eclipse
    assert _sep(s, "sun", "moon") < 1.2  # geocentric; topocentric parallax brings them together
    m = _body(s, "moon")
    assert m["illuminated"] < 0.01 and m["phaseName"] == "New moon"


def test_total_lunar_eclipse_2025_09_07():
    s = astro.sky(datetime(2025, 9, 7, 18, 12, tzinfo=timezone.utc))  # greatest eclipse
    m = _body(s, "moon")
    assert m["elongation"] > 179.0 and m["phaseName"] == "Full moon"


def test_great_conjunction_2020_12_21():
    s = astro.sky(datetime(2020, 12, 21, 18, 0, tzinfo=timezone.utc))
    assert _sep(s, "jupiter", "saturn") < 0.2  # 6 arcminutes apart


def test_mars_opposition_2025_01_16():
    s = astro.sky(datetime(2025, 1, 16, 2, 0, tzinfo=timezone.utc))
    # Opposite the Sun in ecliptic longitude; Mars was ~4 degrees north of the ecliptic, so the
    # angle between them was ~175.7, not 180.
    assert 175 < _body(s, "mars")["elongation"] < 176.5


def test_venus_greatest_eastern_elongation_2025_01_10():
    s = astro.sky(datetime(2025, 1, 10, 4, 0, tzinfo=timezone.utc))
    assert abs(_body(s, "venus")["elongation"] - 47.2) < 0.5


def test_mercury_never_far_from_the_sun():
    for day in range(0, 365, 7):
        t = datetime.fromordinal(datetime(2026, 1, 1).toordinal() + day).replace(tzinfo=timezone.utc)
        assert _body(astro.sky(t), "mercury")["elongation"] < 28.5


def test_sun_subpoint_agrees_with_the_noon_formula():
    for t in (datetime(2026, 6, 21, 12, tzinfo=timezone.utc), datetime(2026, 10, 23, 2, 30, tzinfo=timezone.utc)):
        s = _body(astro.sky(t), "sun")
        lon, lat = astro.subpoint(s["ra"], s["dec"], t)
        noon = world.subsolar(t)
        assert abs(lat - noon["lat"]) < 0.5 and abs(((lon - noon["lon"]) + 540) % 360 - 180) < 0.5


def test_moon_distance_in_range():
    for day in range(0, 60, 3):
        t = datetime.fromordinal(datetime(2026, 1, 1).toordinal() + day).replace(tzinfo=timezone.utc)
        km = _body(astro.sky(t), "moon")["distanceKm"]
        assert 356_000 < km < 407_000
