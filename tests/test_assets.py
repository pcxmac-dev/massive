"""Collection for each asset class: index, forex (incl. inverted pairs) and continuous futures."""
from datetime import date, datetime, timedelta

import pytest

from massive_collate.collector import ET, history_floor, invert_bars, sync
from massive_collate.config import TIERS
from massive_collate.symbols import resolve

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def _log(_):
    pass


def _ms(d: date, hour: int = 10) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=ET).timestamp() * 1000)


def test_invert_bars_is_exact_and_swaps_high_low():
    out = invert_bars([{"t": 1, "o": 1.25, "h": 2.0, "l": 1.0, "c": 1.6, "v": 5, "vw": 1.3}])[0]
    assert (out["o"], out["h"], out["l"], out["c"]) == (0.8, 1.0, 0.5, 0.625)
    assert out["vw"] is None and out["v"] == 5


def test_index_is_fetched_by_source_ticker_without_listing_check(store):
    store.watch_spec("fake", resolve("IXIC"))
    src = FakeSource(entitled=("minute",), bars={"minute": [{"t": _ms(date(2026, 10, 20)), "o": 1, "h": 1, "l": 1, "c": 1}]})
    sync(src, store, FREE, NOW, log=_log)
    assert src.instrument_calls == 0
    assert all(call[0] == "I:COMP" and call[4] == "index" for call in src.bar_calls)
    assert ("second", "I:COMP", "index") in src.probed
    assert store.bar_range("fake", "IXIC", "minute") is not None


def test_inverted_fx_pair_is_stored_inverted(store):
    store.watch_spec("fake", resolve("USDEUR"))
    bar = {"t": _ms(date(2026, 10, 20)), "o": 1.25, "h": 1.25, "l": 1.25, "c": 1.25}
    src = FakeSource(entitled=("minute",), bars={"minute": [bar], "day": [bar]})
    sync(src, store, FREE, NOW, log=_log)
    assert all(call[0] == "C:EURUSD" for call in src.bar_calls)
    df = store.load_bars(["USDEUR"], "minute", source="fake")
    assert df["close"].iloc[0] == pytest.approx(0.8)


def test_continuous_future_rolls_and_fetches_held_contracts(store):
    store.watch_spec("fake", resolve("NQ1!"))
    d1, d2 = date(2026, 9, 17), date(2026, 9, 18)
    src = FakeSource(entitled=("minute",))
    src.contracts = [
        {"ticker": "NQU6", "first_trade_date": "2025-06-20", "last_trade_date": "2026-09-18"},
        {"ticker": "NQZ6", "first_trade_date": "2025-09-19", "last_trade_date": "2026-12-18"},
        {"ticker": "NQZ5", "first_trade_date": "2024-09-20", "last_trade_date": "2025-12-19"},  # out of range? no: kept
    ]
    day = lambda d, v: {"t": _ms(d, 0), "o": 1, "h": 1, "l": 1, "c": 1, "v": v}  # noqa: E731
    src.bars = {
        ("NQU6", "day"): [day(d1, 900), day(d2, 100)],
        ("NQZ6", "day"): [day(d1, 100), day(d2, 800)],
        ("NQZ5", "day"): [],
        ("NQU6", "minute"): [{"t": _ms(d1), "o": 1, "h": 1, "l": 1, "c": 1}],
        ("NQZ6", "minute"): [{"t": _ms(d2), "o": 2, "h": 2, "l": 2, "c": 2}],
    }
    sync(src, store, FREE, NOW, log=_log)
    assert store.rolls("fake", "NQ") == [(d1, "NQU6"), (d2, "NQZ6")]
    minute_calls = [c for c in src.bar_calls if c[3] == "minute"]
    assert {c[0] for c in minute_calls} == {"NQU6", "NQZ6"} and all(c[4] == "future" for c in minute_calls)
    assert store.watchlist("fake")[0]["resolution"] == "minute"
    assert store.watchlist("fake")[0]["kind"] == "future"


def test_futures_never_request_before_floor(store):
    store.watch_spec("fake", resolve("CL1!"))
    src = FakeSource(entitled=("minute",))
    src.contracts = [{"ticker": "CLZ6", "first_trade_date": "2020-01-01", "last_trade_date": "2026-11-19"}]
    src.bars = {("CLZ6", "day"): []}
    sync(src, store, FREE, NOW, log=_log)
    floor = history_floor(FREE, NOW.date())
    assert all(c[1] >= floor - timedelta(days=1) for c in src.bar_calls)
