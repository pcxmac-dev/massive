"""Storage and derived data, from a bug sweep: Massive's condition ids, fingerprints that see
corrections, bins left without base bars, odd-lot minutes at a window's start, a feed created
just after a miss, requests counted on their own day, one derivation record per method, the asset
class and sessions of series no feed watches under that name, wall-clock hours on DST days."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from massive_collate import derive, provenance
from massive_collate.storage import Store
from massive_collate.ticks import build_bars

ET = "America/New_York"


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _bar(t: str, price: float, volume: float = 1.0, tz: str = ET) -> dict:
    return {"t": _ms(t, tz), "o": price, "h": price, "l": price, "c": price, "v": volume, "vw": price, "n": 1}


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["ts"], df["time"] = df["t"], pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
    return df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap",
                              "n": "transactions"})


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


def test_massive_condition_ids_apply_their_bar_rules(store):
    store.save_conditions("massive", [{"id": 37, "name": "Odd Lot Trade", "cta": "I", "utp": "I",
                                       "high_low": False, "open_close": False, "volume": True}])
    rules = store.condition_rules()
    assert rules[("utp", "37")] == (False, False, True) and rules[("utp", "I")] == (False, False, True)
    ns = _ms("2026-10-20 10:00") * 1_000_000
    ticks = pd.DataFrame({"ts_ns": [ns, ns + 1_000_000_000], "price": [10.0, 55.0], "size": [100.0, 3.0],
                          "exchange": [4, 4], "conditions": ["", "37"], "tape": [3, 3]})
    (bar,), unknown = build_bars(ticks, 60_000_000_000, rules)
    assert (bar["h"], bar["c"], bar["v"]) == (10.0, 10.0, 103.0) and unknown == set()


def test_the_fingerprint_changes_when_a_bar_is_corrected_in_place(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, [_bar("2026-10-20 10:00", 10.0)])
    before = store.series_fingerprint("massive", "SPCX")
    store.upsert_bars("massive", "SPCX", "minute", 1, [_bar("2026-10-20 10:00", 99.0)])  # the same time, corrected
    assert store.series_fingerprint("massive", "SPCX") != before


def test_a_rebuild_drops_derived_bins_whose_base_bars_went(store):
    store.upsert_built_bars("iex", "AAPL", "minute", 1, [_bar("2026-10-20 10:00", 10.0, 100), _bar("2026-10-20 10:07", 99.0, 100)], 1)
    derive.build_chain(store, "iex", "AAPL", "minute", "stock")
    five = store._bars("iex").execute("SELECT ts FROM built_bars WHERE timespan='minute' AND multiplier=5").fetchall()
    assert len(five) == 2
    store.drop_built_between("iex", "AAPL", ("minute",), _ms("2026-10-20 10:07"), _ms("2026-10-20 10:08"))  # withdrawn
    store.upsert_built_bars("iex", "AAPL", "minute", 1, [_bar("2026-10-20 10:00", 10.0, 100)], 1)  # rebuilt minute
    derive.build_chain(store, "iex", "AAPL", "minute", "stock")
    conn = store._bars("iex")
    assert conn.execute("SELECT COUNT(*) FROM built_bars WHERE timespan='minute' AND multiplier=5").fetchone()[0] == 1
    assert conn.execute("SELECT high, volume FROM built_bars WHERE timespan='day'").fetchone() == (10.0, 100.0)


def test_an_odd_lot_only_first_minute_carries_the_close_before_the_window():
    ns = _ms("2026-10-20 14:01") * 1_000_000
    ticks = pd.DataFrame({"ts_ns": [ns], "price": [231.0], "size": [5.0], "exchange": [86], "conditions": ["I"],
                          "tape": [3]})
    rules = {("utp", "I"): (False, False, True)}
    assert build_bars(ticks, 60_000_000_000, rules)[0] == []  # without the close before: dropped
    (bar,), _ = build_bars(ticks, 60_000_000_000, rules, prior_close=230.0)
    assert (bar["o"], bar["c"], bar["h"], bar["v"]) == (230.0, 230.0, 230.0, 5.0)


def test_a_feed_written_right_after_a_miss_is_listed(tmp_path):
    with Store(tmp_path / "m.db") as a, Store(tmp_path / "m.db") as b:
        assert a._feed("bitstamp") is None  # a miss, remembered for a few seconds
        b.upsert_bars("bitstamp", "BTCUSD", "minute", 1, [_bar("2026-10-20 10:00", 1.0)])  # another process
        assert ("bitstamp", "BTCUSD", "minute") in a.stored_series()


def test_requests_count_on_the_day_they_are_made(store):
    rid = store.start_run("massive", "sync")
    store.conn.execute("UPDATE runs SET started_at='2026-10-20T23:50:00+00:00' WHERE id=?", (rid,))
    store.finish_run(rid, 100, 0, "ok")
    store.count_request("massive", 60, now=datetime(2026, 10, 20, 23, 55, tzinfo=timezone.utc))
    store.count_request("massive", 40, now=datetime(2026, 10, 21, 0, 5, tzinfo=timezone.utc))  # after midnight
    assert store.requests_today("massive", now=datetime(2026, 10, 21, 1, tzinfo=timezone.utc)) == 40
    assert store.requests_today("massive", now=datetime(2026, 10, 20, 23, 59, tzinfo=timezone.utc)) == 100


def test_the_same_derivation_reuses_its_record_and_widens_its_span(store):
    a = provenance.record(store, "bars-from-bars", "iex:AAPL", {"from": "minute"}, {"bars": "iex:AAPL:minute"}, (100, 200))
    b = provenance.record(store, "bars-from-bars", "iex:AAPL", {"from": "minute"}, {"bars": "iex:AAPL:minute"}, (50, 150))
    assert a == b and store.derivation(a)["span_lo"] == 50 and store.derivation(a)["span_hi"] == 200
    c = provenance.record(store, "bars-from-bars", "iex:AAPL", {"from": "second"}, {"bars": "iex:AAPL:second"}, (0, 1))
    assert c != a


def test_a_series_no_feed_watches_under_that_name_takes_its_symbols_class(store):
    store.watch("massive", ["BTCUSD"], "minute", asset="crypto")  # watched on Massive only
    day = [_bar(f"2026-10-20 {h:02d}:00", 1.0, 60) for h in range(24)]
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, [{**b, "t": _ms(f"2026-10-20 {h:02d}:00", "UTC")}
                                                         for h, b in enumerate(day)])
    derive.run(store, sources={"coinbase"}, log=lambda *_: None)
    (ts, volume), = store._bars("coinbase").execute(
        "SELECT ts, volume FROM built_bars WHERE timespan='day'").fetchall()
    assert ts == _ms("2026-10-20 00:00", "UTC") and volume == 24 * 60  # a whole UTC day, not New York's session


def test_futures_days_are_cme_sessions_dated_by_the_day_they_end():
    rows = [_bar("2026-10-18 18:00", 1.0, 10), _bar("2026-10-18 23:59", 1.0, 10), _bar("2026-10-19 16:59", 1.0, 10)]
    (day,) = derive.aggregate(_frame(rows), "day", 1, "future")
    assert day["t"] == _ms("2026-10-19 00:00") and day["v"] == 30  # Sunday evening belongs to Monday


def test_multi_hour_bins_keep_the_wall_clock_on_the_day_the_clocks_change():
    rows = [_bar("2026-03-08 17:00", 1.0), _bar("2026-03-08 21:30", 1.0)]  # Sunday evening, after spring forward
    bins = derive.aggregate(_frame(rows), "hour", 4, "future")
    assert [pd.Timestamp(b["t"], unit="ms", tz="UTC").tz_convert(ET).strftime("%H:%M") for b in bins] == ["16:00", "20:00"]


def test_the_tick_range_is_the_oldest_and_newest(store):
    store.upsert_trades("alpaca", "AAPL", [{"sip_timestamp": t, "sequence_number": t, "price": 1.0, "size": 1}
                                           for t in (300, 100, 200)])
    assert store.trade_range("alpaca", "AAPL") == (100, 300)
    assert store.trade_range("alpaca", "MSFT") is None
