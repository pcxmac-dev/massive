"""Bars-only mode (BARS_ONLY, on by default): no process but the live recorder opens the feeds'
tick files. The chart app, the sync, audits and checks work from bars alone; reading the tick
files kept tens of gigabytes of them in Windows' file cache."""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing

import pandas as pd
import pytest

from massive_collate import cli, collector, live
from massive_collate.config import load_settings
from massive_collate.storage import Store, TicksBlocked, set_tick_access

MIN = 60_000


def _trade(ns: int, seq: int = 1) -> dict:
    return {"sip_timestamp": ns, "sequence_number": seq, "price": 10.0, "size": 5, "exchange": 4, "conditions": [12]}


def _bar(t: int, c: float = 1.0) -> dict:
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 2.0, "vw": c, "n": 3}


@pytest.fixture
def with_ticks(tmp_path):
    """A database whose feed has bars and a tick file (written with tick access)."""
    with Store(tmp_path / "m.db", ticks=True) as s:
        s.upsert_bars("alpaca", "AAPL", "minute", 1, [_bar(MIN)])
        s.upsert_trades("alpaca", "AAPL", [_trade(MIN * 1_000_000), _trade(MIN * 1_000_000 + 5, 2)])
    return tmp_path / "m.db"


def test_a_bars_only_store_never_opens_a_tick_file(with_ticks):
    with Store(with_ticks, ticks=False) as s:
        feed = s._feed("alpaca")
        assert not feed.has_ticks()
        assert [r[1] for r in feed.conn.execute("PRAGMA database_list")] == ["main"]  # never attached
        assert s.trades_between("alpaca", "AAPL", 0, 2**62).empty
        assert s.trade_edges("alpaca", "AAPL") is None and s.load_trades("AAPL").empty
        assert ("alpaca", "AAPL", "trades") not in s.stored_series()
        assert ("alpaca", "AAPL", "minute") in s.stored_series()  # its bars as ever
        with pytest.raises(TicksBlocked):
            s.upsert_trades("alpaca", "AAPL", [_trade(2 * MIN * 1_000_000)])
        reader = s.reader()
        try:
            assert not reader.ticks_allowed and not reader._feed("alpaca").has_ticks()
        finally:
            reader.close()
    with closing(sqlite3.connect(with_ticks.parent / "feeds" / "alpaca-ticks.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2  # nothing lost: just not read


def test_the_process_mode_is_what_a_store_takes_when_not_told(with_ticks):
    set_tick_access(False)
    with Store(with_ticks) as s:
        assert not s.ticks_allowed and not s._feed("alpaca").has_ticks()
    set_tick_access(True)
    with Store(with_ticks) as s:
        assert s._feed("alpaca").has_ticks()


def test_bars_only_is_the_default_setting(monkeypatch):
    monkeypatch.delenv("BARS_ONLY", raising=False)
    assert load_settings(env={}).bars_only is True
    assert load_settings(env={"BARS_ONLY": "off"}).bars_only is False


def test_every_command_takes_the_mode_and_the_recorder_alone_opens_ticks():
    assert "set_tick_access(not settings.bars_only)" in inspect.getsource(cli.main)
    assert "Store(self.db_path, ticks=True)" in inspect.getsource(live.Recorder.run)


def test_the_sync_builds_nothing_from_ticks_it_does_not_read(with_ticks):
    with Store(with_ticks, ticks=False) as s:
        s.add_coverage("alpaca", "AAPL", "trades", 0, 10 * MIN)
        assert collector.build_from_ticks(s, "alpaca", "AAPL", 0, 10 * MIN, rules={}, log=lambda *_: None) == 0
        assert "alpaca" not in s.coverage("AAPL", "second")  # no span claimed as built from ticks


def test_rebuilding_from_ticks_is_refused_in_bars_only_mode(with_ticks):
    from massive_collate import baddata

    with Store(with_ticks, ticks=False) as s, pytest.raises(TicksBlocked):
        baddata.repair_from_ticks(s, "alpaca", log=lambda *_: None)


def test_a_bars_volume_profile_without_trades_is_spread_from_its_seconds(tmp_path):
    from massive_collate.gui.bars import BarEngine
    from massive_collate.gui.window import WindowReader

    hour = int(pd.Timestamp("2026-10-12 10:00", tz="America/New_York").timestamp() * 1000)
    with Store(tmp_path / "m.db", check_same_thread=False, ticks=False) as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(hour + i * MIN, 10.0 + i / 100) for i in range(120)])
        s.watch("massive", ["SPCX"], "minute")
        reader = WindowReader(BarEngine(s))
        assert reader.profile("massive", "SPCX", "hour", 1, "extended", at_ms=hour)["basis"] == "30 1-minute bars"  # 9:30-10:30
        s.upsert_bars("massive", "SPCX", "second", 1, [_bar(hour + i * 1000, 10.0) for i in range(90)])
        got = reader.profile("massive", "SPCX", "hour", 1, "extended", at_ms=hour)
        assert got["basis"] == "90 1-second bars" and "estimated from 1-second bars" in got["derived"]
        later = reader.profile("massive", "SPCX", "hour", 1, "extended", at_ms=hour + 60 * MIN)
        assert later["basis"] == "60 1-minute bars"  # 10:30-11:30, without seconds: its minutes
