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
    sync(src, store, FREE, NOW + timedelta(days=1), log=_log)
    assert src.instrument_calls == 1  # reference details only, at most weekly; no listing/identity check
    assert store.details("fake", "IXIC") is not None
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


def test_inverted_pair_is_named_as_the_inverse(store):
    from massive_collate.models import Instrument

    store.watch_spec("fake", resolve("USDEUR"))
    src = FakeSource(entitled=("minute",), listing=Instrument("C:EURUSD", name="Euro - United States dollar",
                                                             details={"name": "Euro - United States dollar"}))
    sync(src, store, FREE, NOW, log=_log)
    name = "Euro - United States dollar, inverted (USD priced in EUR)"
    assert store.details("fake", "USDEUR")["name"] == name and store.watchlist("fake")[0]["name"] == name


def _nq_source():
    d1, d2 = date(2026, 9, 17), date(2026, 9, 18)
    day = lambda d, v: {"t": _ms(d, 0), "o": 1, "h": 1, "l": 1, "c": 1, "v": v}  # noqa: E731
    src = FakeSource(entitled=("minute",))
    src.bars = {
        ("NQU6", "day"): [day(d1, 900), day(d2, 100)],
        ("NQZ6", "day"): [day(d1, 100), day(d2, 800), day(NOW.date() - timedelta(days=1), 700)],
        ("NQU6", "minute"): [{"t": _ms(d1), "o": 1, "h": 1, "l": 1, "c": 1}],
        ("NQZ6", "minute"): [{"t": _ms(d2), "o": 2, "h": 2, "l": 2, "c": 2}],
    }
    return src, d1, d2


def test_continuous_future_rolls_and_fetches_held_contracts(store):
    store.watch_spec("fake", resolve("NQ1!"))
    src, d1, d2 = _nq_source()
    sync(src, store, FREE, NOW, log=_log)
    rolls = store.rolls("fake", "NQ")
    assert rolls[:2] == [(d1, "NQU6"), (d2, "NQZ6")] and {t for _, t in rolls} == {"NQU6", "NQZ6"}
    minute_calls = [c for c in src.bar_calls if c[3] == "minute"]
    assert {c[0] for c in minute_calls} == {"NQU6", "NQZ6"} and all(c[4] == "future" for c in minute_calls)
    w = store.watchlist("fake")[0]
    assert w["resolution"] == "minute" and w["kind"] == "future"
    states = {c["ticker"]: c["state"] for c in store.contracts("fake", "NQ")}
    assert states["NQU6"] == "ended" and states["NQZ6"] == "live" and states["NQH6"] == "missing"


def test_known_contracts_are_not_requested_again(store):
    store.watch_spec("fake", resolve("NQ1!"))
    src, _, _ = _nq_source()
    sync(src, store, FREE, NOW, log=_log)
    src.bar_calls = []
    sync(src, store, FREE, NOW, log=_log)  # same day: missing/ended skipped, live already current
    assert [c for c in src.bar_calls if c[3] == "day"] == []
    later = NOW + timedelta(days=2)
    src.bar_calls = []
    sync(src, store, FREE, later, log=_log)
    day_calls = {c[0] for c in src.bar_calls if c[3] == "day"}
    assert "NQZ6" in day_calls and "NQU6" not in day_calls and "NQH6" not in day_calls


def test_two_digit_year_contracts_are_found_and_remembered(store):
    store.watch_spec("fake", resolve("NG1!"))
    d = NOW.date() - timedelta(days=1)
    day = {"t": _ms(d, 0), "o": 3, "h": 3, "l": 3, "c": 3, "v": 500}
    src = FakeSource(entitled=("minute",))
    src.bars = {("NGX26", "day"): [day], ("NGX26", "minute"): [{"t": _ms(d), "o": 3, "h": 3, "l": 3, "c": 3}]}
    sync(src, store, FREE, NOW, log=_log)
    states = {c["ticker"]: c["state"] for c in store.contracts("fake", "NG")}
    assert states["NGX26"] == "live" and states["NGX6"] == "missing"
    assert store.rolls("fake", "NG")[-1] == (d, "NGX26")
    src.bar_calls = []
    sync(src, store, FREE, NOW + timedelta(days=2), log=_log)
    asked = {c[0] for c in src.bar_calls if c[3] == "day"}
    assert "NGX26" in asked and "NGX6" not in asked  # the working name is remembered


def test_untried_naming_is_tried_even_if_the_other_is_pending(store):
    store.watch_spec("fake", resolve("NG1!"))
    store.set_contract("fake", "NG", "NGX6", date(2026, 11, 1), "pending", None, NOW.date())  # earlier run
    d = NOW.date() - timedelta(days=1)
    src = FakeSource(entitled=("minute",))
    src.bars = {("NGX26", "day"): [{"t": _ms(d, 0), "o": 3, "h": 3, "l": 3, "c": 3, "v": 500}]}
    sync(src, store, FREE, NOW, log=_log)
    assert {c["ticker"]: c["state"] for c in store.contracts("fake", "NG")}["NGX26"] == "live"


def test_entitlements_are_probed_once_per_asset_class(store):
    store.watch_spec("fake", resolve("AAA"))
    store.watch_spec("fake", resolve("BBB"))
    src = FakeSource(entitled=("minute",))
    sync(src, store, FREE, NOW, log=_log)
    stock_probes = [p for p in src.probed if p[2] == "stock"]
    assert [r for r, _, _ in stock_probes] == ["trades", "second", "minute"]  # once, not per ticker
    assert {w["resolution"] for w in store.watchlist("fake")} == {"minute"}


def test_recently_synced_symbols_wait_for_the_refresh_interval(store):
    store.watch_spec("fake", resolve("IXIC"))
    src = FakeSource(entitled=("minute",), bars={"minute": [{"t": _ms(date(2026, 10, 20)), "o": 1, "h": 1, "l": 1, "c": 1}]})
    first = sync(src, store, FREE, NOW, log=_log, refresh_hours=12)
    assert first.skipped == 0 and store.watchlist("fake")[0]["synced_at"] is not None
    src.bar_calls = []
    soon = sync(src, store, FREE, NOW + timedelta(hours=2), log=_log, refresh_hours=12)
    assert soon.skipped == 1 and src.bar_calls == []
    later = sync(src, store, FREE, NOW + timedelta(hours=13), log=_log, refresh_hours=12)
    assert later.skipped == 0 and src.bar_calls


def test_unfinished_backfill_is_not_marked_synced(store):
    store.watch_spec("fake", resolve("IXIC"))
    src = FakeSource(entitled=("minute",), budget=2)  # runs out mid-way
    result = sync(src, store, FREE, NOW, log=_log, refresh_hours=12)
    assert result.stopped and store.watchlist("fake")[0]["synced_at"] is None


def test_refresh_hours_default_by_plan():
    from massive_collate.config import load_settings

    assert load_settings(env={}, env_file=None).refresh_hours == 12
    assert load_settings(env={"MASSIVE_TIER": "starter"}, env_file=None).refresh_hours == 0
    assert load_settings(env={"MASSIVE_REFRESH_HOURS": "3"}, env_file=None).refresh_hours == 3


def test_futures_never_request_before_floor(store):
    store.watch_spec("fake", resolve("CL1!"))
    src = FakeSource(entitled=("minute",))
    sync(src, store, FREE, NOW, log=_log)
    floor = history_floor(FREE, NOW.date())
    assert src.bar_calls and all(c[1] >= floor for c in src.bar_calls)
