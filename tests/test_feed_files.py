"""Each feed's data in files of its own, and the one-off move from the single-file layout."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from massive_collate import splitfeeds
from massive_collate.storage import OldLayout, Store


def _bar(t: int, c: float = 1.0) -> dict:
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 2.0, "vw": c, "n": 3}


def _trade(ns: int, seq: int = 1) -> dict:
    return {"sip_timestamp": ns, "sequence_number": seq, "price": 10.0, "size": 5, "exchange": 4, "conditions": [12]}


def _tables(path) -> set[str]:
    with closing(sqlite3.connect(path)) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_each_feed_has_its_own_files(tmp_path):
    with Store(tmp_path / "m.db") as s:
        s.upsert_bars("massive", "AAPL", "minute", 1, [_bar(60_000), _bar(120_000)])
        s.upsert_bars("alpaca", "AAPL", "minute", 1, [_bar(180_000)])
        s.upsert_trades("alpaca", "AAPL", [_trade(1), _trade(2)])
        df = s.load_bars(["AAPL"], "minute")
        assert list(df["source"]) == ["massive", "massive", "alpaca"]  # all feeds, in time order
        assert sorted(s.stored_series()) == [("alpaca", "AAPL", "minute"), ("alpaca", "AAPL", "trades"),
                                             ("massive", "AAPL", "minute")]
        assert s.feed_names() == ["alpaca", "massive"]
    feeds = tmp_path / "feeds"
    assert sorted(f.name for f in feeds.glob("*.db")) == ["alpaca-ticks.db", "alpaca.db", "massive.db"]
    assert "bars" not in _tables(tmp_path / "m.db")  # the main file keeps only what feeds share
    with closing(sqlite3.connect(feeds / "massive.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 2
        assert "source" not in {r[1] for r in c.execute("PRAGMA table_info(bars)")}  # the file is the feed


def test_a_feed_written_by_another_process_is_seen(tmp_path):
    reader = Store(tmp_path / "m.db")  # e.g. the chart app, open before the feed existed
    assert reader.stored_series() == []
    writer = Store(tmp_path / "m.db")  # the collector
    writer.upsert_bars("tiingo", "SPY", "day", 1, [_bar(86_400_000)])
    assert ("tiingo", "SPY", "day") in reader.stored_series()
    assert len(reader.bars_window(["tiingo"], "SPY", "day", 0, 10**13)) == 1
    writer.upsert_trades("tiingo", "SPY", [_trade(5)])  # its first ticks: a new file
    assert reader.trade_edges("tiingo", "SPY") == (5, 5)
    reader.close()
    writer.close()


def test_unknown_feed_reads_as_empty(store):
    assert store.bar_edges("nobody", "X") is None and store.bars_window(["nobody", "*"], "X", "minute", 0, 1).empty
    assert store.load_trades("X", "nobody").empty and store.composite_stats("BTCUSD", "minute").empty
    with pytest.raises(ValueError):
        store.upsert_bars("../escape", "X", "minute", 1, [_bar(1)])


def _old_database(tmp_path):
    """The single-file layout: bars of every feed and the composite stats in the main file, ticks in ticks.db."""
    path = tmp_path / "massive.db"
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("CREATE TABLE bars (source TEXT NOT NULL, ticker TEXT NOT NULL, timespan TEXT NOT NULL, "
                  "multiplier INTEGER NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, "
                  "volume REAL, vwap REAL, transactions INTEGER, PRIMARY KEY (source, ticker, timespan, multiplier, ts)) "
                  "WITHOUT ROWID")
        c.executemany("INSERT INTO bars VALUES (?, ?, 'minute', 1, ?, ?, ?, ?, ?, 1.5, NULL, 7)",
                      [(src, t, ts, ts / 1e5, ts / 1e5 + 1, ts / 1e5 - 1, ts / 1e5 + 0.25)
                       for src in ("massive", "alpaca", "composite") for t in ("AAPL", "BTCUSD")
                       for ts in range(60_000, 60_000 * 50, 60_000)])
        c.execute("CREATE TABLE composite_stats (ticker TEXT NOT NULL, timespan TEXT NOT NULL, ts INTEGER NOT NULL, "
                  "venues INTEGER, sd_bps REAL, range_bps REAL, env_h REAL, env_l REAL, members TEXT, outliers TEXT, "
                  "PRIMARY KEY (ticker, timespan, ts)) WITHOUT ROWID")
        c.executemany("INSERT INTO composite_stats VALUES ('BTCUSD', 'minute', ?, 3, 0.5, 1.0, 2.0, 0.5, 'a,b,c', '')",
                      [(ts,) for ts in range(60_000, 60_000 * 50, 60_000)])
        c.execute("CREATE TABLE watchlist (source TEXT NOT NULL, ticker TEXT NOT NULL, resolution TEXT NOT NULL "
                  "DEFAULT 'auto', backfilled_to TEXT, added_at TEXT NOT NULL, PRIMARY KEY (source, ticker))")
        c.execute("INSERT INTO watchlist VALUES ('massive', 'AAPL', 'auto', NULL, 'x')")
    with closing(sqlite3.connect(tmp_path / "ticks.db")) as c, c:
        c.execute("CREATE TABLE trades (source TEXT NOT NULL, ticker TEXT NOT NULL, ts_ns INTEGER NOT NULL, "
                  "seq INTEGER NOT NULL, price REAL NOT NULL, size REAL NOT NULL, exchange INTEGER, conditions TEXT, "
                  "trade_id TEXT, tape INTEGER, PRIMARY KEY (source, ticker, ts_ns, seq)) WITHOUT ROWID")
        c.executemany("INSERT INTO trades VALUES ('alpaca', 'BTCUSD', ?, ?, 100.5, 0.25, 1, '12,37', 'id', NULL)",
                      [(ns * 1000, ns % 3) for ns in range(1, 400)])
    return path


def test_old_layout_is_refused_until_split(tmp_path):
    path = _old_database(tmp_path)
    with pytest.raises(OldLayout, match="split-feeds"):
        Store(path)
    lines = []
    moved = splitfeeds.split(path, log=lines.append)
    assert moved == {"alpaca.db bars": 98, "composite.db bars": 98, "massive.db bars": 98,
                     "composite.db composite_stats": 49, "alpaca-ticks.db trades": 399}
    backups = sorted(f.name for f in (tmp_path / "backups").iterdir())
    assert len(backups) == 2 and backups[0].endswith("-ticks.db")  # both old files backed up first
    assert not (tmp_path / "ticks.db").exists() and not (tmp_path / "feeds.partial").exists()
    assert not {"bars", "composite_stats"} & _tables(path)
    with Store(path) as s:
        bars = s.load_bars(["AAPL"], "minute", source="massive")
        assert len(bars) == 49 and bars["close"].iloc[0] == pytest.approx(0.85) and bars["transactions"].iloc[0] == 7
        assert len(s.composite_stats("BTCUSD", "minute")) == 49
        trades = s.load_trades("BTCUSD", "alpaca")
        assert len(trades) == 399 and trades["conditions"].iloc[0] == "12,37"
        assert s.watchlist()[0]["ticker"] == "AAPL"  # shared tables stay in the main file
    assert splitfeeds.split(path, log=lines.append) == {}  # done: nothing left to move


def test_a_copy_that_differs_leaves_the_old_layout_whole(tmp_path, monkeypatch):
    path = _old_database(tmp_path)
    monkeypatch.setattr(splitfeeds, "_same", lambda a, b: False)
    with pytest.raises(splitfeeds.SplitError, match="nothing removed"):
        splitfeeds.split(path, log=lambda *_: None)
    assert "bars" in _tables(path) and (tmp_path / "ticks.db").exists() and not (tmp_path / "feeds").exists()
    monkeypatch.undo()
    assert len(splitfeeds.split(path, log=lambda *_: None)) == 5  # run again: redone from the start
    with Store(path) as s:
        assert len(s.load_bars(["BTCUSD"], "minute")) == 3 * 49
