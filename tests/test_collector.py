from __future__ import annotations

from datetime import date, datetime, timedelta

from massive_collate import collector
from massive_collate.config import TIERS
from massive_collate.collector import ET, history_floor, sync

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def _trade(day: date, second: int, price: float = 10.0, exchange: int = 4) -> dict:
    ns = int(datetime(day.year, day.month, day.day, 10, 0, second, tzinfo=ET).timestamp()) * 10**9
    return {"sip_timestamp": ns, "sequence_number": second, "price": price, "size": 100, "exchange": exchange}


def _trades(n_days: int = 3, per_day: int = 3) -> list[dict]:
    return [_trade(NOW.date() - timedelta(days=d), s, 10 + s) for d in range(n_days) for s in range(per_day)]


def test_history_floor_respects_plan():
    assert history_floor(FREE, date(2026, 10, 21)) == date(2024, 10, 22)
    assert history_floor(FREE, date(2028, 2, 29)) == date(2026, 3, 1)


def test_auto_picks_finest_resolution(store):
    src = FakeSource(entitled=("trades", "second", "minute"), trades=_trades())
    store.watch("fake", ["SPCX"])
    sync(src, store, FREE, NOW, log=lambda m: None)
    assert store.watchlist("fake")[0]["resolution"] == "trades"
    assert store.trade_range("fake", "SPCX") is not None


def test_falls_back_when_ticks_not_on_plan(store):
    bar = {"t": 1_760_000_000_000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}
    src = FakeSource(entitled=("minute",), bars={"minute": [bar]})
    store.watch("fake", ["SPCX"])
    sync(src, store, FREE, NOW, log=lambda m: None)
    assert store.watchlist("fake")[0]["resolution"] == "minute"
    assert store.bar_range("fake", "SPCX", "minute") == (bar["t"], bar["t"])


def test_never_requests_before_plan_floor(store):
    src = FakeSource(trades=_trades())
    store.watch("fake", ["SPCX"], "trades")
    sync(src, store, FREE, NOW, log=lambda m: None)
    floor_ns = collector._date_ns(history_floor(FREE, NOW.date()))
    assert all(c["gte"] is None or c["gte"] >= floor_ns for c in src.trade_calls)


def test_stopped_run_resumes_without_gaps(store):
    all_trades = _trades(n_days=4, per_day=3)  # 12 trades, 2 per page
    store.watch("fake", ["SPCX"], "trades")

    first = FakeSource(trades=all_trades, budget=3)
    result = sync(first, store, FREE, NOW, log=lambda m: None)
    assert result.stopped and first.requests_made == 3
    assert store.watchlist("fake")[0]["backfilled_to"] is None

    second = FakeSource(trades=all_trades)
    sync(second, store, FREE, NOW, log=lambda m: None)
    got = store.load_trades("SPCX", "fake")
    assert len(got) == len(all_trades)
    assert store.watchlist("fake")[0]["backfilled_to"] == history_floor(FREE, NOW.date())


def test_backfilled_series_only_fetches_new_data(store):
    src = FakeSource(trades=_trades())
    store.watch("fake", ["SPCX"], "trades")
    sync(src, store, FREE, NOW, log=lambda m: None)
    src.trade_calls.clear()
    sync(src, store, FREE, NOW, log=lambda m: None)
    assert len(src.trade_calls) == 1 and src.trade_calls[0]["desc"] is False


def test_exchanges_saved_once(store):
    src = FakeSource(trades=_trades())
    sync(src, store, FREE, NOW, log=lambda m: None)
    sync(src, store, FREE, NOW, log=lambda m: None)
    ex = store.exchanges("fake")
    assert list(ex["name"]) == ["Nasdaq", "NYSE Arca"] and ex["country"].iloc[0] == "US"


def test_trades_joined_with_exchange_names(store):
    src = FakeSource(trades=_trades())
    store.watch("fake", ["SPCX"], "trades")
    sync(src, store, FREE, NOW, log=lambda m: None)
    df = store.load_trades("SPCX", "fake")
    assert set(df["exchange_name"]) == {"Nasdaq"}
    assert str(df["time"].dt.tz) == "America/New_York"
