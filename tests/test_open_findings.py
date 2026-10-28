"""The open findings of the 2026-10-28 audit (docs/AUDIT.md), fixed: trades the exchange withdrew
are archived, never deleted, and found by more than an id unique within a day; a pruned bar hides
only the version pruned, so a correction or a repair shows."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from massive_collate import live
from massive_collate.storage import Store

ET = "America/New_York"
DAY_NS = 86_400_000_000_000


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


def _trade(ns: int, trade_id: int, price: float, size: float = 100) -> dict:
    return {"sip_timestamp": ns, "sequence_number": (trade_id << 6) | (ord("V") & 63), "price": price, "size": size,
            "exchange": ord("V"), "conditions": [], "id": str(trade_id), "tape": 3}


def test_a_withdrawn_trade_is_archived_and_an_id_alone_never_takes_yesterdays(store):
    ns = _ms("2026-10-23 10:00") * 1_000_000
    yesterday, today = _trade(ns - DAY_NS + 5, 7, 231.0), _trade(ns, 7, 231.0)  # the exchange's ids start again daily
    store.upsert_trades("iex", "AAPL", [yesterday, today])
    conn = store._ticks("iex")
    assert store.withdraw_trades("iex", "AAPL", [{**_trade(ns, 7, 230.0), "withdrawn": True}], ns - DAY_NS) == []
    assert store.withdraw_trades("iex", "AAPL", [{**today, "withdrawn": True}], ns - DAY_NS) == [ns]  # the newest
    assert conn.execute("SELECT ts_ns FROM ticks.trades").fetchall() == [(ns - DAY_NS + 5,)]
    assert conn.execute("SELECT ts_ns, price, reason FROM ticks.archived_trades").fetchall() == [(ns, 231.0, "withdrawn")]
    # canceled in the batch that brought it: never in the series, archived as it came
    late = _trade(ns + 1, 8, 232.0)
    assert store.withdraw_trades("iex", "AAPL", [{**late, "withdrawn": True}], ns, unstored=[late]) == []
    assert conn.execute("SELECT ts_ns FROM ticks.archived_trades ORDER BY ts_ns").fetchall() == [(ns,), (ns + 1,)]
    assert store.archived_rows("iex", "AAPL") == 2
    assert store.withdraw_trades("nofeed", "AAPL", [{**late, "withdrawn": True}], ns) == []  # no files made for it


def test_a_batch_hands_over_the_trades_it_withdrew():
    t = _trade(1, 5, 1.0)
    fix = {**_trade(1, 5, 1.5)}
    standing, gone, taken = live._settle([t, {**t, "withdrawn": True}, fix])
    assert standing == [fix] and gone == [{**t, "withdrawn": True}] and taken == [t]


def test_a_bar_the_stream_joined_partway_through_is_never_claimed_whole(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=(), stock_keys=("PKTESTKEY", "test-secret"),
                        log=lambda *_: None, connect=lambda url: None)
    rec.streams[0].confirmed = frozenset({"AAPL"})
    heard = _ms("2026-10-23 10:00:30.5")
    rec._since["iex"], rec._subs["iex"] = heard, {"AAPL": heard}
    rec._buffer[("iex", "AAPL")].append(_trade(_ms("2026-10-23 10:00:40") * 1_000_000, 1, 230.0))
    now = _ms("2026-10-23 10:01:30")
    rec._heard["iex"] = now
    with Store(tmp_path / "m.db") as store:
        assert rec.flush(store, now_ms=now) == 1
        assert store.coverage("AAPL", "trades")["iex"] == [(heard, now)]
        assert store.coverage("AAPL", "second")["iex"] == [(_ms("2026-10-23 10:00:31"), now)]
        assert store.coverage("AAPL", "minute")["iex"] == [(_ms("2026-10-23 10:01"), now)]  # 10:00 was half heard
        assert len(store.bars_window(["iex"], "AAPL", "minute", now - 90_000, now)) == 1  # built and shown all the same


def test_a_late_change_mid_history_reaches_the_composite(store):
    from datetime import datetime, timezone

    from massive_collate import composite
    from massive_collate.coverage import FeedProfile
    from massive_collate.config import TIERS
    from massive_collate.symbols import resolve

    mk = lambda n, v: FeedProfile(n, TIERS["free"], frozenset({"crypto"}), venues={"crypto": v})  # noqa: E731
    feeds = {"coinbase": mk("coinbase", "coinbase"), "alpaca": mk("alpaca", "kraken")}
    t0 = int(datetime(2026, 10, 1, 15, tzinfo=timezone.utc).timestamp() * 1000)
    old = t0 - 20 * 86_400_000
    bar = lambda t, c=1.0: {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1.0}  # noqa: E731
    for feed in feeds:
        store.watch_spec(feed, resolve("BTCUSD"))
        store.upsert_bars(feed, "BTCUSD", "minute", 1, [bar(old), bar(t0)])
    now = datetime.fromtimestamp((t0 + 3_600_000) / 1000, timezone.utc)
    composite.refresh(store, feeds, now, log=lambda m: None)
    close = lambda: store.bars_window(["composite"], "BTCUSD", "minute", old, old + 60_000)["close"].tolist()  # noqa: E731
    assert close() == [1.0]
    time.sleep(0.005)
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, [bar(old, 3.0)])  # a correction, twenty days back
    composite.refresh(store, feeds, now, log=lambda m: None)
    assert close() == [2.0]  # the mean of 3 and 1 (equal volume and quality)


def test_tiingos_symbols_a_month_are_the_keys_on_the_machine_its_forex_included(tmp_path):
    from datetime import datetime, timezone

    from massive_collate import collector
    from massive_collate.config import load_settings
    from massive_collate.ratelimit import MachineLedger
    from massive_collate.sources import providers

    ledger, now = MachineLedger("tiingo:symbols", "k"), datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Store(tmp_path / "a.db") as a, Store(tmp_path / "b.db") as b:
        assert collector._monthly_symbol(a, "tiingo", "AAPL", now, 2, ledger)
        assert collector._monthly_symbol(b, "tiingofx", "EURUSD", now, 2, ledger)  # another database, its forex
        assert not collector._monthly_symbol(a, "tiingo", "MSFT", now, 2, ledger)  # the key's two places are taken
        assert collector._monthly_symbol(b, "tiingo", "AAPL", now, 2, ledger)  # one already asked: still may
        assert collector._monthly_symbol(a, "tiingo", "MSFT", datetime(2026, 11, 1, tzinfo=timezone.utc), 2, ledger)
    settings = load_settings(env={"TIINGO_API_KEY": "k"}, env_file=None)
    stocks, fx = providers()["tiingo"].build(settings, 1), providers()["tiingofx"].build(settings, 1)
    assert stocks.symbol_ledger.name == fx.symbol_ledger.name and fx.symbols_per_month == stocks.symbols_per_month == 450


def test_a_symbol_taken_off_the_watchlist_during_a_run_is_skipped_not_the_run_ended(store):
    from massive_collate import collector

    said = []
    store.watch("massive", ["AAPL"], "minute")
    assert collector._watch_row(store, "massive", "AAPL", said.append)["ticker"] == "AAPL"
    store.unwatch("massive", ["AAPL"])  # the chart's list changed while the sync ran
    assert collector._watch_row(store, "massive", "AAPL", said.append) is None and "taken off" in said[0]


def test_a_pruned_bar_hides_only_the_version_pruned(store):
    t = _ms("2026-10-20 10:00")
    bar = {"t": t, "o": 1, "h": 9, "l": 1, "c": 1, "v": 1}
    window = lambda: store.bars_window(["massive"], "SPCX", "minute", t, t + 60_000)  # noqa: E731
    loaded = lambda: store.load_bars(["SPCX"], "minute", source="massive")  # noqa: E731
    store.upsert_bars("massive", "SPCX", "minute", 1, [bar])
    rows = store.series_rows("massive", "SPCX", "minute", t, t + 60_000)
    assert store.prune("massive", "SPCX", "minute", 1, rows, "spike", True, code="test") == 1
    assert window().empty and loaded().empty
    time.sleep(0.005)
    store.upsert_bars("massive", "SPCX", "minute", 1, [bar])  # the same bar again: not stored anew, still left out
    assert window().empty
    store.upsert_bars("massive", "SPCX", "minute", 1, [{**bar, "h": 1.5}])  # the feed's correction
    assert window()["high"].tolist() == [1.5] and loaded()["high"].tolist() == [1.5]
    # the new version is judged as itself: listed again, and left out again
    assert store.prune("massive", "SPCX", "minute", 1, store.series_rows("massive", "SPCX", "minute", t, t + 60_000),
                       "spike", True, code="test") == 1
    assert window().empty
    # a caller without the version prunes the one stored
    store.upsert_bars("massive", "SPCX", "minute", 1, [{**bar, "t": t + 60_000}])
    assert store.prune("massive", "SPCX", "minute", 1, pd.DataFrame([{"ts": t + 60_000, "high": 9.0}]), "spike", True,
                       code="test") == 1
    assert store.bars_window(["massive"], "SPCX", "minute", t + 60_000, t + 120_000).empty


def test_a_pruned_built_bar_shows_again_once_a_repair_builds_it_anew(store):
    t = _ms("2026-10-20 10:00")
    first = store.record_derivation("bars-from-ticks", "massive:SPCX", "test", {"v": 1}, {})
    store.upsert_built_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 1, "h": 9, "l": 1, "c": 1, "v": 1}], first)
    store.prune("massive", "SPCX", "minute", 1, store.series_rows("massive", "SPCX", "minute", t, t + 60_000),
                "spike", True, code="test")
    window = lambda: store.bars_window(["massive"], "SPCX", "minute", t, t + 60_000)  # noqa: E731
    assert window().empty
    store.upsert_built_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 1, "h": 9, "l": 1, "c": 1, "v": 1}], first)
    assert window().empty  # built again by the same derivation (the live recorder's next flush)
    repair = store.record_derivation("bars-from-ticks", "massive:SPCX", "test", {"v": 2}, {})
    assert repair != first
    store.upsert_built_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 1, "h": 1.2, "l": 1, "c": 1, "v": 1}], repair)
    assert window()["high"].tolist() == [1.2]
