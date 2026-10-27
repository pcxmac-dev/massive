"""CME Group alongside NYSE and Nasdaq (the user, 2026-10-27): every futures product of CME, NYMEX,
CBOT and COMEX watched as its continuous chart, the best-known first, with the futures given a
capped share of each run so the stocks are not held behind their rate."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from massive_collate import collector, initializer
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings
from massive_collate.storage import Store
from massive_collate.symbols import resolve

from .conftest import FakeSource

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=ET)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


def _directory(store):
    stocks = [{"ticker": t, "name": t, "exchange": mic, "type": "CS", "kind": "stock"} for t, mic in
              (("AAPL", "XNAS"), ("KO", "XNYS"))]
    futures = [{"ticker": t, "name": n, "exchange": mic, "type": ty, "kind": "future"} for t, n, mic, ty in (
        ("ZZZ", "A sports contract", "XCME", "sports_index"), ("CL", "Crude oil", "XNYM", "crude_oil"),
        ("GC", "Gold", "XCEC", "precious"), ("ES", "E-mini S&P 500", "XCME", "us_index"),
        ("ZN", "10-year note", "XCBT", "long_term_gov"), ("0SN10", "Event swap 10:00", "XCME", "sports_index"),
        ("LBR", "Lumber", "XCME", "forestry"), ("ICE1", "Elsewhere", "IFUS", "softs"))]
    store.update_directory("massive", "stock", stocks, TODAY)
    store.update_directory("massive", "future", futures, TODAY)
    store.save_market_snapshot("massive", date(2026, 10, 26), [
        {"T": "AAPL", "c": 250.0, "v": 50e6, "vw": 250.0}, {"T": "KO", "c": 70.0, "v": 10e6, "vw": 70.0},
        {"T": "CL", "c": 60.0, "v": 9e9, "vw": 60.0}])  # Colgate-Palmolive's shares: not crude oil's volume


def test_the_best_known_futures_go_first_then_by_sector_and_events_last():
    rank = lambda code, sector: initializer.futures_rank({"ticker": code, "type": sector})  # noqa: E731
    assert rank("ES", "us_index") < rank("CL", "crude_oil") < rank("ZN", "long_term_gov")  # all MAJOR_FUTURES
    assert rank("ZN", "long_term_gov") < rank("XAE", "sel_sector_index") < rank("LBR", "forestry")
    assert rank("LBR", "forestry") < rank("EVT", "sports_index")
    assert initializer.EXCHANGES == ("XNYS", "XNAS", "XCME", "XNYM", "XCBT", "XCEC")


def test_cme_groups_products_are_watched_as_continuous_futures(store):
    _directory(store)
    store.watch_spec("massive", resolve("GC1!"))  # already watched: kept as it is
    dry = initializer.initialize(store, ["massive"], initializer.CME_GROUP, dry_run=True, futures_feeds=["massive"])
    f = dry["futures"]
    assert f["listed"] == 7 and f["byExchange"] == {"XCME": 4, "XNYM": 1, "XCEC": 1, "XCBT": 1}  # not IFUS
    assert f["added"] == {"massive": 5} and f["unreadable"] == ["0SN10"]  # GC1! watched already
    assert f["top"] == ["ES1!", "CL1!", "GC1!", "ZN1!", "LBR1!", "ZZZ1!"]
    assert {w["ticker"] for w in store.watchlist()} == {"GC1!"} and not store.kv_get(initializer.ORDER_KEY)

    initializer.initialize(store, ["massive"], initializer.STOCK_EXCHANGES)  # the stocks first ...
    initializer.initialize(store, ["massive"], initializer.CME_GROUP, futures_feeds=["massive"])  # ... then CME Group
    rows = {w["ticker"]: w for w in store.watchlist("massive")}
    assert rows["ES1!"]["asset"] == "future" and rows["ES1!"]["source_ticker"] == "ES" and rows["ES1!"]["rank"] == 1
    assert rows["ES1!"]["resolution"] == "minute"
    order = initializer.load_order(store)
    assert [t for t, _ in sorted(order.items(), key=lambda kv: kv[1])] == [
        "AAPL", "KO", "ES1!", "CL1!", "GC1!", "ZN1!", "LBR1!", "ZZZ1!"]  # the futures after the stocks
    initializer.initialize(store, ["massive"], initializer.STOCK_EXCHANGES)  # stocks ranked again: futures keep theirs
    again = initializer.load_order(store)
    assert again["AAPL"] == 0 and again["ES1!"] > again["KO"] and again["ES1!"] < again["ZZZ1!"]


def test_the_cli_takes_cme_for_all_four_exchanges(tmp_path, monkeypatch, capsys):
    from massive_collate import cli

    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    with Store(tmp_path / "m.db") as s:
        _directory(s)
    assert cli.main(["initialize", "--exchanges", "CME", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "7 futures products on XCME 4" in out and "would be added" in out and "0SN10" in out


class Futures(FakeSource):
    """Each futures contract asked for costs a request and has no bars."""
    frames: tuple = ()


def test_futures_take_a_capped_share_of_each_run_and_go_first(store):
    store.watch("fake", ["AAA"], "minute")
    for t in ("ES1!", "CL1!", "GC1!"):
        store.watch_spec("fake", resolve(t), "minute")
    store.kv_set(initializer.ORDER_KEY, json.dumps({"AAA": 0, "ES1!": 1, "CL1!": 2, "GC1!": 3}))
    src, seen, lines = Futures(entitled=("minute", "day")), [], []
    sync(src, store, TIERS["advanced"], NOW, log=lines.append, on_item=seen.append, futures_per_run=1)
    assert seen[0] == "ES1!" and "AAA" in seen  # futures first; the stocks still get the run
    assert "CL1!" not in seen and "GC1!" not in seen  # the futures' share was spent on ES1!
    assert any("futures left for the next runs" in m for m in lines)


def test_less_traded_futures_are_refreshed_weekly(store, monkeypatch):
    monkeypatch.setattr(collector, "FUTURES_DAILY", 1)
    for t in ("ES1!", "LBR1!"):
        store.watch_spec("fake", resolve(t), "minute")
        store.set_synced("fake", t, NOW - timedelta(days=2))
    store.kv_set(initializer.ORDER_KEY, json.dumps({"ES1!": 0, "LBR1!": 1}))
    assert collector._futures_kept_current(store.watchlist("fake"), initializer.load_order(store), store, NOW) == {"ES1!"}
    src, seen = Futures(entitled=("minute", "day")), []
    sync(src, store, TIERS["advanced"], NOW, log=lambda *_: None, on_item=seen.append, futures_per_run=1000)
    assert "LBR1!" not in seen  # synced two days ago: not due before a week
    store.set_synced("fake", "LBR1!", NOW - timedelta(days=8))
    seen.clear()
    sync(src, store, TIERS["advanced"], NOW, log=lambda *_: None, on_item=seen.append, futures_per_run=1000)
    assert "LBR1!" in seen


def test_official_schedules_are_asked_for_the_picked_and_the_first_futures(store, monkeypatch):
    from massive_collate.live import WATCHLISTS_KEY

    monkeypatch.setattr(collector, "FUTURES_SCHEDULES", 2)
    for t in ("ES1!", "CL1!", "GC1!", "ZN1!"):
        store.watch_spec("massive", resolve(t), "minute")
    store.kv_set(initializer.ORDER_KEY, json.dumps({"ES1!": 0, "CL1!": 1, "GC1!": 2, "ZN1!": 3}))
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "mine", "tickers": ["ZN1!"]}]))
    assert collector.futures_for_schedules(store, "massive") == ["CL", "ES", "ZN"]


def test_futures_per_run_is_a_setting():
    assert load_settings(env={}, env_file=None).futures_per_run == 100
    assert load_settings(env={"FUTURES_PER_RUN": "40"}, env_file=None).futures_per_run == 40
