"""Processes behaving badly: locks that outlive or are taken from their owners, recorder threads
and flushes that fail, coverage for time nobody was listening, runs cut short."""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from massive_collate import cli, live, proc
from massive_collate.config import ConfigError, load_settings
from massive_collate.storage import Store


# -- locks ------------------------------------------------------------------------------------

def test_a_live_owner_keeps_its_lock_however_old(tmp_path):
    lock = tmp_path / "m.lock"
    assert proc.acquire(lock)
    old = time.time() - 7 * 3600  # a laptop that slept through the night mid-sync
    os.utime(lock, (old, old))
    assert proc.holder(lock) == os.getpid() and not proc.acquire(lock)


def test_a_lock_naming_a_reused_process_id_is_taken_over(tmp_path):
    lock = tmp_path / "m.lock"
    lock.write_text(f"{os.getpid()} 1")  # this process's id, but another process's start
    assert proc.holder(lock) is None and proc.acquire(lock)
    assert lock.read_text() == proc.stamp()


def test_releasing_leaves_a_lock_another_process_took(tmp_path):
    lock = tmp_path / "m.lock"
    assert proc.acquire(lock)
    lock.write_text("4242 7")  # taken over meanwhile
    proc.release(lock)
    assert lock.exists()


def test_the_run_lock_refuses_while_another_live_process_holds_it(tmp_path):
    db = tmp_path / "m.db"
    with cli.run_lock(db):
        assert proc.owns(db.with_suffix(".lock"))
    assert not db.with_suffix(".lock").exists()
    other = db.with_suffix(".lock")
    other.write_text(f"{os.getppid()} {proc.started(os.getppid()) or 0}")  # this test's parent: alive
    with pytest.raises(SystemExit):
        with cli.run_lock(db):
            pass
    assert other.exists()  # not removed by the run that was refused


def test_an_interrupted_run_is_not_recorded_ok(tmp_path):
    settings = load_settings(env={"MASSIVE_API_KEY": "k", "MASSIVE_DB": str(tmp_path / "m.db")}, env_file=None)

    class Args:
        source = "massive"

    with pytest.raises(KeyboardInterrupt):
        with cli.api_run(Args(), settings, "sync") as (source, store, log):
            raise KeyboardInterrupt
    with Store(tmp_path / "m.db") as store:
        assert store.runs(1)[0]["status"] == "interrupted"


# -- the live recorder --------------------------------------------------------------------------

class _Dies:
    def __init__(self, error):
        self.error = error

    def send(self, m):
        pass

    def recv(self):
        raise self.error

    def close(self):
        pass


def test_a_stream_thread_that_meets_an_unexpected_error_is_marked_down_and_retried(tmp_path):
    logged = []
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=logged.append)
    calls, states = [], []

    def connect(url):
        calls.append(url)
        states.append((dict(rec._status["coinbase"]), "coinbase" in rec._since))  # as the last connection left it
        if len(calls) >= 2:
            rec.stop.set()
        return _Dies(TypeError("int() argument must be a string, not 'NoneType'"))

    rec.connect = connect
    rec.stop.wait = lambda timeout=None: rec.stop.is_set()
    rec._run_stream(rec.streams[0])  # returns: nothing escaped the thread
    status, connected = states[1]  # after the first connection's error
    assert len(calls) == 2 and not connected and status["connected"] is False and "TypeError" in status["error"]
    assert any("disconnected (TypeError" in line for line in logged)  # tried again, said why


def test_a_failed_flush_keeps_the_trades_for_the_next(tmp_path, monkeypatch):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    t0 = 1_792_825_200_000
    rec._buffer[("coinbase", "BTCUSD")].append(live._trade(t0 * 1_000_000, 1, 81000, 0.1, 1))
    with Store(tmp_path / "m.db") as store:
        real = store.upsert_trades

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "upsert_trades", locked)
        with pytest.raises(sqlite3.OperationalError):
            rec.flush(store, now_ms=t0 + 5_000)
        rec._buffer[("coinbase", "BTCUSD")].append(live._trade((t0 + 1000) * 1_000_000, 2, 81001, 0.1, 2))
        monkeypatch.setattr(store, "upsert_trades", real)
        assert rec.flush(store, now_ms=t0 + 6_000) == 2  # the first trade too
        assert store.trade_edges("coinbase", "BTCUSD")[0] == t0 * 1_000_000


def test_coverage_ends_at_the_last_time_the_stream_was_heard(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    t0 = 1_792_825_200_000
    rec._since["coinbase"], rec._heard["coinbase"] = t0, t0 + 10_000  # then nothing (asleep, or a dead link)
    with Store(tmp_path / "m.db") as store:
        rec.flush(store, now_ms=t0 + 8 * 3_600_000)
        assert store.coverage("BTCUSD", "trades")["coinbase"] == [(t0, t0 + 10_000 + live.HEARD_SLACK_MS)]


def test_a_trade_stamped_after_the_flush_clock_reaches_its_minute_bar(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    m = 1_792_825_200_000  # a whole minute
    rec._buffer[("coinbase", "BTCUSD")] += [live._trade((m - 30_000) * 1_000_000, 1, 100, 1, 1),
                                            live._trade((m - 200) * 1_000_000, 2, 105, 1, 2)]
    with Store(tmp_path / "m.db") as store:
        rec.flush(store, now_ms=m - 500)  # this machine's clock a little behind the exchange's
        rec._buffer[("coinbase", "BTCUSD")].append(live._trade((m + 1000) * 1_000_000, 3, 107, 1, 3))
        rec.flush(store, now_ms=m + 2_000)
        bar = store._bars("coinbase").execute(
            "SELECT close, volume FROM built_bars WHERE timespan='minute' AND ts=?", (m - 60_000,)).fetchone()
    assert bar == (105.0, 2.0)


def test_a_recorder_that_lost_its_lock_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LOCK_CHECK_SECONDS", 0)
    monkeypatch.setattr(live, "FLUSH_SECONDS", 0.01)
    locks = live.take_locks(tmp_path / "m.db")
    rec = live.Recorder(tmp_path / "m.db", coins=(), sources=(), log=lambda *_: None, connect=lambda url: None,
                        locks=locks)
    live.machine_lock().write_text("4242 7")  # another recorder took over
    rec.run()  # returns on its own
    assert rec.stop.is_set()
    live.release_locks(locks)


def test_unknown_live_coins_are_refused():
    with pytest.raises(ConfigError, match="MATIC"):
        load_settings(env={"LIVE_CRYPTO": "BTC,MATIC"}, env_file=None)
    assert load_settings(env={"LIVE_CRYPTO": "btc, eth"}, env_file=None).live_coins == ("BTC", "ETH")


def test_two_exchanges_streaming_one_pair_are_not_a_double_download(tmp_path):
    from massive_collate.deps import coverage_overlaps

    with Store(tmp_path / "m.db") as store:
        store.add_coverage("coinbase", "BTCUSD", "trades", 0, 100)
        store.add_coverage("bitstamp", "BTCUSD", "trades", 0, 100)
        store.add_coverage("massive", "AAPL", "minute", 0, 100)
        store.add_coverage("alpaca", "AAPL", "minute", 50, 150)
        markets = {"coinbase": "coinbase", "bitstamp": "bitstamp", "massive": "sip", "alpaca": "sip"}
        overlaps = coverage_overlaps(store, lambda a, b, t: markets[a] == markets[b])
    assert len(overlaps) == 1 and overlaps[0].startswith("AAPL minute massive/alpaca")


def test_a_class_plan_change_finds_resolutions_again(tmp_path):
    log = []
    with Store(tmp_path / "m.db") as store:
        cli._note_tier(store, load_settings(env={"MASSIVE_TIER": "advanced"}, env_file=None), log.append)
        store.watch("massive", ["BTCUSD"], "minute", asset="crypto")
        cli._note_tier(store, load_settings(env={"MASSIVE_TIER": "advanced", "MASSIVE_TIER_CRYPTO": "starter"},
                                            env_file=None), log.append)
        assert log and "crypto=starter" in log[0]
        assert store.watchlist("massive", "BTCUSD")[0]["resolution"] == "auto"
