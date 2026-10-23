"""Markets view: world exchanges (sessions, lunch breaks, holidays), the sun, US/CME/FX state."""
from datetime import date, datetime, timezone

import pytest

from massive_collate.gui import world
from massive_collate.gui.markets import markets, symbol_sessions, us_equities
from massive_collate.sessions import ET, MarketCalendar

pytest.importorskip("exchange_calendars")
CAL = MarketCalendar()


def _status(code, when):
    e = next(x for x in world.EXCHANGES if x.code == code)
    return world.status(e, when)


def test_tokyo_lunch_break_and_close():
    # Friday 2026-10-23 12:00 JST = 03:00 UTC: Tokyo is at lunch (11:30-12:30).
    s = _status("XTKS", datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc))
    assert s["state"] == "break" and s["event"] == "reopens"
    assert s["today"]["break"] == "11:30-12:30" and s["today"]["close"] == "15:30"
    s = _status("XTKS", datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc))
    assert s["state"] == "open" and s["event"] == "lunch break"


def test_kospi_open_and_weekend():
    assert _status("XKRX", datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc))["state"] == "open"
    sat = _status("XKRX", datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc))
    assert sat["state"] == "closed" and sat["event"] == "opens" and sat["today"] is None


def test_shenzhen_uses_shanghais_calendar():
    a = _status("XSHG", datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc))
    b = _status("XSHE", datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc))
    assert a["state"] == b["state"] == "open" and a["today"] == b["today"]


def test_saudi_weekend_is_friday_and_saturday():
    s = _status("XSAU", datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc))  # a Friday
    assert s["state"] == "closed" and s["weekend"] == [4, 5]
    assert all(date.fromisoformat(d).weekday() not in (4, 5) for d in s["holidays"])


def test_every_exchange_reports_a_state():
    out = world.world_markets(datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc))
    assert len(out) == len(world.EXCHANGES)
    assert all(m["state"] in ("open", "break", "closed") for m in out), [m for m in out if m["state"] not in ("open", "break", "closed")]


def test_subsolar_point():
    noon_greenwich = world.subsolar(datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc))
    assert abs(noon_greenwich["lon"]) < 2.5 and 23 < noon_greenwich["lat"] < 23.6  # June solstice
    assert abs(world.subsolar(datetime(2026, 3, 20, 14, 46, tzinfo=timezone.utc))["lat"]) < 0.5  # the equinox moment
    assert abs(abs(world.subsolar(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))["lon"]) - 180) < 2


def test_us_equities_phases():
    pre = us_equities(CAL, datetime(2026, 10, 22, 8, 0, tzinfo=ET))
    assert pre["state"] == "pre-market" and pre["next"]["state"] == "open"
    assert us_equities(CAL, datetime(2026, 10, 22, 10, 0, tzinfo=ET))["state"] == "open"
    assert us_equities(CAL, datetime(2026, 10, 22, 17, 0, tzinfo=ET))["state"] == "after-hours"
    assert us_equities(CAL, datetime(2026, 11, 26, 10, 0, tzinfo=ET))["state"] == "closed"  # Thanksgiving


def test_markets_lists_watched_groups():
    import pandas as pd

    watch = [{"ticker": "NQ1!", "asset": "future", "source_ticker": "NQ"}, {"ticker": "USDEUR", "asset": "fx", "source_ticker": "C:EURUSD"},
             {"ticker": "AAPL", "asset": "stock", "source_ticker": "AAPL"}]
    out = markets(CAL, pd.DataFrame(), watch, datetime(2026, 10, 22, 21, 0, tzinfo=ET))
    ids = [m["id"] for m in out["markets"]]
    assert ids == ["us-equities", "us-indices", "cme-equity", "fx"]
    cme = out["markets"][2]
    assert cme["state"] == "open" and cme["tradeDate"] == "2026-10-23"


def test_symbol_sessions_for_annotations():
    rows = symbol_sessions(CAL, "stock", None, datetime(2026, 11, 25, tzinfo=ET), datetime(2026, 11, 28, tzinfo=ET))
    assert [r["day"] for r in rows] == ["2026-11-25", "2026-11-27"]  # Thanksgiving skipped
    friday = rows[1]
    assert friday["regularClose"] - friday["regularOpen"] == 3.5 * 3600 * 1000  # 09:30-13:00


def test_scan_only_and_fetch_endpoint(tmp_path):
    from massive_collate.collector import scan
    from massive_collate.config import TIERS
    from massive_collate.gui.server import ChartApp
    from massive_collate.storage import Store
    from massive_collate.symbols import resolve

    with Store(tmp_path / "m.db", check_same_thread=False) as store:
        store.watch_spec("massive", resolve("AAPL"))
        store.watch_spec("massive", resolve("MSFT"))
        result = scan(None, store, TIERS["free"], source_name="massive", only={"AAPL"}, log=lambda m: None)
        assert [r["ticker"] for r in result.items] == ["AAPL"]
        with pytest.raises(LookupError):
            ChartApp(store).start_scan("NOPE")  # only watched symbols can be fetched


def test_proxy_moves_colour_the_exchanges(tmp_path):
    from datetime import date

    from massive_collate.storage import Store

    with Store(tmp_path / "m.db") as store:
        store.save_proxy_closes("massive", date(2026, 10, 21), {"EWJ": 100.0, "EWY": 50.0, "SPY": 600.0})
        store.save_proxy_closes("massive", date(2026, 10, 22), {"EWJ": 103.0, "EWY": 48.5, "SPY": 600.0})
        moves = store.proxy_moves()
    assert round(moves["EWJ"]["changePct"], 6) == 3.0 and round(moves["EWY"]["changePct"], 6) == -3.0
    out = {m["code"]: m for m in world.world_markets(datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc), moves=moves)}
    assert out["XTKS"]["performance"] == {"proxy": "EWJ", "day": "2026-10-22", "close": 103.0, "changePct": moves["EWJ"]["changePct"]}
    assert out["XKRX"]["performance"]["changePct"] < 0 and out["XNYS"]["performance"]["changePct"] == 0
    assert "performance" not in out["XPRA"]  # no US-listed proxy for Prague
