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
             {"ticker": "AAPL", "asset": "stock", "source_ticker": "AAPL"},
             {"ticker": "BTCUSD", "asset": "crypto", "source_ticker": "X:BTCUSD"}]
    out = markets(CAL, pd.DataFrame(), watch, datetime(2026, 10, 22, 21, 0, tzinfo=ET))
    ids = [m["id"] for m in out["markets"]]
    assert ids == ["us-equities", "us-indices", "cme-equity", "fx", "crypto"]
    crypto = out["markets"][-1]
    assert crypto["state"] == "open" and crypto["until"] is None and crypto["symbols"] == ["BTCUSD"]
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
    perf = out["XTKS"]["performance"]
    assert {k: perf[k] for k in ("proxy", "day", "close", "changePct")} == {
        "proxy": "EWJ", "day": "2026-10-22", "close": 103.0, "changePct": moves["EWJ"]["changePct"]}
    assert perf["basis"].startswith("EWJ, a US-listed fund")
    assert out["XKRX"]["performance"]["changePct"] < 0 and out["XNYS"]["performance"]["changePct"] == 0
    assert "performance" not in out["XPRA"]  # no US-listed proxy for Prague


def test_market_moves_skip_an_unpublished_day(tmp_path):
    from datetime import date

    from massive_collate.collector import ET, fetch_market_moves
    from massive_collate.storage import Store

    class Src:
        name = "massive"
        calls = []

        def market_daily(self, day):
            self.calls.append(day)
            if day == date(2026, 10, 22):
                return []  # not published yet
            return [{"T": "EWJ", "c": 100.0 + day.day}, {"T": "AAPL", "c": 1.0}]

    src = Src()
    with Store(tmp_path / "m.db") as store:
        now = datetime(2026, 10, 22, 23, 0, tzinfo=ET)
        used = fetch_market_moves(src, store, now, ["EWJ"], log=lambda m: None)
        assert used == 3 and store.proxy_days("massive") == [date(2026, 10, 20), date(2026, 10, 21)]
        assert round(store.proxy_moves()["EWJ"]["changePct"], 4) == round((121 / 120 - 1) * 100, 4)
        src.calls.clear()
        assert fetch_market_moves(src, store, now, ["EWJ"], log=lambda m: None) == 0  # nothing re-asked


def test_market_moves_treat_before_end_of_day_as_not_published(tmp_path):
    from datetime import date

    from massive_collate.collector import ET, fetch_market_moves
    from massive_collate.errors import NotEntitled
    from massive_collate.storage import Store

    class Src:
        name = "massive"

        def market_daily(self, day):
            if day == date(2026, 10, 22):
                raise NotEntitled(403, "Attempted to request today's data before end of day.")
            return [{"T": "EWJ", "c": 100.0 + day.day}]

    with Store(tmp_path / "m.db") as store:
        fetch_market_moves(Src(), store, datetime(2026, 10, 22, 23, tzinfo=ET), ["EWJ"], log=lambda m: None)
        assert store.proxy_days("massive") == [date(2026, 10, 20), date(2026, 10, 21)]


# -- foreign exchange centres and crypto exchanges --------------------------------------------

def _e(code):
    return next(x for x in world.EXCHANGES if x.code == code)


def test_fx_centre_follows_business_hours_inside_the_fx_week():
    # Thursday 2026-10-22 10:00 London (BST): London's dealing centre is open until 17:00 local
    s = world.fx_status(_e("FX-LON"), datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))
    assert s["state"] == "open" and s["until"] == "2026-10-22T16:00:00+00:00" and s["kind"] == "fx"
    # Saturday: closed until Monday 08:00 London
    s = world.fx_status(_e("FX-LON"), datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))
    assert s["state"] == "closed" and s["until"] == "2026-10-26T08:00:00+00:00"  # GMT again after 25 Oct


def test_wellington_waits_for_the_fx_week_to_open():
    # Monday 08:00 NZDT is Sunday 14:00 New York (EST), before spot FX opens at 17:00 New York:
    # the centre opens when the market does (Monday 11:00 NZDT). 2026-11-02 is an ordinary Monday.
    sunday = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    s = world.fx_status(_e("FX-WLG"), sunday)
    assert s["state"] == "closed" and s["until"] == "2026-11-01T22:00:00+00:00"


def test_fx_centre_holidays_come_from_the_local_exchange():
    # New Zealand's Labour Day (Monday 2026-10-26): Wellington stays closed until Tuesday
    s = world.fx_status(_e("FX-WLG"), datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))
    assert "2026-10-26" in s["holidays"] and s["until"] == "2026-10-26T19:00:00+00:00"  # Tue 08:00 NZDT


def test_fx_windows_cross_the_new_york_day_boundary_without_a_gap():
    # Wellington 08:00-17:00 NZDT spans 17:00 New York, where one FX session ends and the next begins
    w = world.fx_windows(_e("FX-WLG"), datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc), days=2)
    assert any((b - a).total_seconds() == 9 * 3600 for a, b in w)


def test_crypto_exchanges_never_close():
    for when in (datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc), datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)):
        s = world.crypto_status(_e("CB"), when)
        assert s["state"] == "open" and s["aroundTheClock"] and s["holidays"] == [] and s["kind"] == "crypto"


def test_stored_series_colour_fx_centres_and_crypto_exchanges(tmp_path):
    from massive_collate.storage import Store

    day = lambda d: int(datetime(2026, 10, d, tzinfo=timezone.utc).timestamp() * 1000)  # noqa: E731
    with Store(tmp_path / "m.db") as store:
        store.upsert_bars("massive", "BTCUSD", "day", 1, [{"t": day(21), "c": 100000.0}, {"t": day(22), "c": 107000.0}])
        store.upsert_bars("massive", "USDJPY", "day", 1, [{"t": day(21), "c": 150.0}, {"t": day(22), "c": 160.0}])
        store.save_proxy_closes("massive", date(2026, 10, 21), {"IBIT": 50.0, "FXY": 60.0})
        store.save_proxy_closes("massive", date(2026, 10, 22), {"IBIT": 40.0, "FXY": 66.0})
        moves = world.market_moves(store)
    out = {m["code"]: m for m in world.world_markets(datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc), moves=moves)}
    # the stored series wins over the proxy fund
    assert out["UPBIT"]["performance"]["proxy"] == "BTCUSD" and round(out["UPBIT"]["performance"]["changePct"], 9) == 7.0
    # USDJPY up 150 -> 160 means the yen fell: 150/160 - 1
    tyo = out["FX-TYO"]["performance"]
    assert tyo["proxy"] == "JPY/USD" and round(tyo["changePct"], 9) == round((150 / 160 - 1) * 100, 9)
    assert "inverted" in tyo["basis"] and tyo["day"] == "2026-10-22"
    assert "performance" not in out["FX-HKG"]  # pegged currency, no series or fund


def test_big_moves_use_thresholds_by_market_type():
    from massive_collate.gui.announcements import exchange_events

    now = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)
    rows = world.world_markets(now, moves={"BTCUSD": {"day": "2026-10-22", "close": 1.0, "prevClose": 1.0, "changePct": 5.0},
                                           "GBPUSD": {"day": "2026-10-22", "close": 1.0, "prevClose": 1.0, "changePct": 1.2}})
    texts = [e["text"] for e in exchange_events(now, rows)]
    assert not any("Bitcoin" in t for t in texts)  # 5% is an ordinary Bitcoin day
    assert sum("GBP against the dollar up 1.2%" in t for t in texts) == 1
    rows = world.world_markets(now, moves={"BTCUSD": {"day": "2026-10-22", "close": 1.0, "prevClose": 1.0, "changePct": -8.0}})
    bitcoin = [e for e in exchange_events(now, rows) if "Bitcoin" in e["text"]]
    assert len(bitcoin) == 1 and bitcoin[0]["source"] == "BTCUSD"  # one item for all crypto exchanges
