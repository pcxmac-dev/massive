"""Fixes from the code review (CODE_REVIEW.md) in the sources and live feeds: a refused key, server
waits and redirects, the live streams' failures, the shared request window, closed archives and
the continuous futures roll."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from massive_collate import cli, killswitch, live
from massive_collate.errors import NotEntitled, Paused, RateLimited, SourceError, Unauthorized
from massive_collate.sources.alpaca import AlpacaClient
from massive_collate.sources.massive import RETRY_WAIT_MAX, MassiveClient, MassiveSource, SameHostSession, _retry_delay
from massive_collate.storage import Store

from .conftest import FakeResponse, FakeSession


def _client(responses, **kw):
    session = FakeSession(responses)
    kw.setdefault("sleep", lambda s: None)
    return MassiveClient("KEY", session=session, **kw), session


# -- a refused key (finding 2) ---------------------------------------------------------------------

def test_a_401_is_the_key_refused_not_a_plan_and_nothing_more_is_sent():
    client, session = _client([FakeResponse(401, {"status": "ERROR", "error": "Unknown API Key"})] * 3)
    with pytest.raises(Unauthorized) as first:
        client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
    assert not isinstance(first.value, NotEntitled) and killswitch.is_refusal(first.value, first.value.status)
    with pytest.raises(Unauthorized, match="nothing sent"):
        client.get("/v2/aggs/ticker/MSFT/range/1/minute/2026-10-01/2026-10-02")
    assert len(session.calls) == client.requests_made == 1  # not retried, and not asked again


def test_a_probe_with_a_refused_key_is_not_an_answer_about_the_plan():
    src = MassiveSource(_client([FakeResponse(401, {"error": "Unknown API Key"})])[0])
    with pytest.raises(Unauthorized):  # "not on your plan" would be remembered for a week
        src.is_entitled("second", "AAPL")
    src = MassiveSource(_client([FakeResponse(403, {"message": "NOT_AUTHORIZED"})])[0])
    assert src.is_entitled("second", "AAPL") is False  # a 403 is the plan


def test_a_revoked_key_stops_the_sync_after_one_request_and_kills_the_feed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "data" / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "FAKE")
    sent = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        sent.append(url)
        return FakeResponse(401, {"status": "ERROR", "error": "Unknown API Key"})

    monkeypatch.setattr(requests.Session, "get", get)
    for ticker in ("AAPL", "MSFT", "NVDA", "AMZN"):
        assert cli.main(["watch", "add", ticker]) == 0
    cli.main(["sync"])
    assert len([u for u in sent if "api.massive.com" in u]) == 1  # not one 401 per symbol until the cap
    with Store(tmp_path / "data" / "m.db") as s:
        k = killswitch.killed(s, "massive", "fetch")
        assert k and "401" in k["why"]


def test_alpaca_keys_the_trading_api_refuses_lose_the_reference_record_only():
    session = FakeSession([FakeResponse(401, {"message": "unauthorized."}), FakeResponse(200, {"bars": {}}),
                           FakeResponse(401, {"message": "unauthorized."})])
    client = AlpacaClient("KID", "SECRET", session=session, sleep=lambda s: None)
    with pytest.raises(NotEntitled, match="ALPACA_PAPER"):  # paper keys on the live API, or the reverse
        client.get(f"{client.trading_url}/v2/assets/AAPL")
    with pytest.raises(NotEntitled, match="nothing sent"):
        client.get(f"{client.trading_url}/v2/assets/MSFT")
    assert client.get("/v2/stocks/bars") == {"bars": {}}  # the data API still answers
    with pytest.raises(Unauthorized):  # the data API refusing them is the keys refused
        client.get("/v2/stocks/bars")
    assert len(session.calls) == 3


# -- server waits and redirects (low) --------------------------------------------------------------

def test_retry_after_as_an_http_date_and_past_the_longest_wait():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    when = FakeResponse(503, headers={"Retry-After": "Mon, 26 Oct 2026 12:00:45 GMT"})
    assert _retry_delay(when, 0, now=now) == 45.0
    assert _retry_delay(FakeResponse(503, headers={"Retry-After": "nan"}), 1) == 10.0  # unreadable: own backoff
    assert _retry_delay(FakeResponse(503, headers={"Retry-After": "Mon, 26 Oct 2026 11:00:00 GMT"}), 0, now=now) == 0.0

    slept = []
    client, session = _client([FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": str(RETRY_WAIT_MAX + 1)}),
                               FakeResponse(200, {})], sleep=slept.append)
    with pytest.raises(RateLimited):  # not waited out for hours, not asked again sooner than told
        client.get("/x")
    assert len(session.calls) == 1 and slept == []


def test_a_pause_ends_a_long_wait_for_the_server():
    slept = []
    client, session = _client([FakeResponse(503, {}, headers={"Retry-After": "120"}), FakeResponse(200, {})],
                              sleep=slept.append)
    client.should_stop = lambda: "fetching paused by you" if len(slept) >= 3 else None
    with pytest.raises(Paused):
        client.get("/x")
    assert len(session.calls) == 1 and sum(slept) < 120  # stopped part way, in slices


def _redirect(location: str, url: str = "https://data.alpaca.markets/v2/stocks/bars") -> requests.Response:
    r = requests.Response()
    r.status_code, r.url = 302, url
    r.headers["Location"] = location
    return r


def test_a_redirect_is_followed_on_the_same_host_only():
    s = SameHostSession()
    assert s.get_redirect_target(_redirect("/v2/stocks/bars?page=2")) == "/v2/stocks/bars?page=2"
    assert s.get_redirect_target(_redirect("https://evil.test/collect")) is None  # the key headers stay home
    assert s.get_redirect_target(_redirect("http://data.alpaca.markets/v2/x")) is None  # nor in clear text
    client, _ = _client([FakeResponse(302, {}, headers={"Location": "https://evil.test/collect"})])
    with pytest.raises(SourceError, match="redirected to evil.test; not followed"):
        client.get("/x")
    assert isinstance(MassiveClient("K")._session, SameHostSession)
    assert isinstance(AlpacaClient("KID", "SECRET")._session, SameHostSession)


def test_each_asset_classs_window_keeps_the_margin_of_a_few_a_minute():
    from massive_collate.config import load_settings
    from massive_collate.sources import build_source

    free = build_source("massive", load_settings(env={"MASSIVE_API_KEY": "FAKE"}, env_file=None)).client
    assert free._limiter.period == 62.0
    # Advanced stocks (unlimited), the other classes on the free plan: 5 a minute each, the same margin
    client = build_source("massive", load_settings(env={"MASSIVE_API_KEY": "FAKE", "MASSIVE_TIER": "advanced"},
                                                   env_file=None)).client
    assert client.class_limiters["crypto"].max_calls == 5
    assert all(lim.period == 62.0 for lim in client.class_limiters.values())


def test_the_futures_day_window_ends_with_the_last_days_session():
    client, session = _client([FakeResponse(200, {"results": []})])
    list(MassiveSource(client).bar_pages("ESZ6", date(2026, 10, 19), date(2026, 10, 23), "day", asset="future"))
    params = session.calls[0][1]
    # the session settling on the 19th opened on the 18th's evening; the one opening on the 23rd's
    # evening settles on the 26th: not asked for
    assert params["window_start.gte"] == "2026-10-18" and params["window_start.lt"] == "2026-10-23"


# -- the live streams (findings 3 and 25; lows) ------------------------------------------------------

class _WS:
    def __init__(self, msgs=()):
        self.msgs = list(msgs)

    def send(self, m):
        pass

    def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        raise ConnectionError("closed by the server (1000)")

    def close(self):
        pass


def _recorder(tmp_path, connect, until):
    """A coinbase recorder whose waits are recorded, not slept; it stops once `until(waits)`."""
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=connect)
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if until(waits) or rec._status["coinbase"].get("killed"):
            rec.stop.set()
        return rec.stop.is_set()

    rec.stop.wait = wait
    return rec, waits


def test_a_network_that_is_down_does_not_kill_the_stream(tmp_path):
    def connect(url):
        raise OSError("[Errno 11001] getaddrinfo failed")  # no connection made at all

    rec, waits = _recorder(tmp_path, connect, until=lambda w: len(w) >= 2 * killswitch.FLAP_LIMIT)
    rec._run_stream(rec.streams[0])
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0]  # backoff only, on its way to MAX_BACKOFF
    with Store(tmp_path / "m.db") as s:
        assert killswitch.killed(s, "coinbase", "live") is None and s.feed_events("coinbase") == []
    assert rec._status["coinbase"]["failures"] == len(waits)


def test_a_server_that_takes_the_connection_and_drops_it_still_flaps(tmp_path):
    calls = []

    def connect(url):
        calls.append(url)
        return _WS()  # dropped as soon as it connects

    rec, _ = _recorder(tmp_path, connect, until=lambda w: False)
    rec._run_stream(rec.streams[0])
    assert len(calls) == killswitch.FLAP_LIMIT
    with Store(tmp_path / "m.db") as s:
        assert "dropped within" in killswitch.killed(s, "coinbase", "live")["why"]


def test_a_database_error_in_the_reconnect_handler_does_not_end_the_stream(tmp_path, monkeypatch):
    def locked(self, feed, kind, outcome, *a, **k):
        if outcome != "connected":  # connected, then the drop cannot be written
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "feed_event", locked)
    calls = []
    rec, waits = _recorder(tmp_path, lambda url: calls.append(url) or _WS(), until=lambda w: len(w) >= 2)
    rec._run_stream(rec.streams[0])
    assert len(calls) == 2 and waits == [1.0, 2.0]  # tried again: the thread went on
    assert rec._status["coinbase"]["connected"] is False


def test_a_kill_that_cannot_be_written_still_keeps_the_stream_off(tmp_path, monkeypatch):
    def unwritable(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(live.killswitch, "strike", unwritable)
    calls = []
    refusal = json.dumps({"type": "error", "message": "Failed to subscribe", "reason": "subscription limit reached"})
    rec, waits = _recorder(tmp_path, lambda url: calls.append(url) or _WS([refusal]), until=lambda w: True)
    rec._run_stream(rec.streams[0])
    assert len(calls) == 1 and waits == [killswitch.KILL_MINUTES[0] * 60]  # off as long as a kill, not a second
    assert "kill not recorded" in rec._status["coinbase"]["error"]


class _Thread:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_a_stream_thread_that_ended_is_started_again_with_growing_waits(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    started = []
    rec._start_stream = lambda s: started.append(s.source) or _Thread(False)  # each ends at once
    threads, restarts = {"coinbase": _Thread(False)}, {}
    for now in (0.0, 4.0, 5.0, 6.0, 15.0, 16.0, 17.0, 36.0):
        rec._supervise(threads, restarts, now)
    # found ended at 0: started at 5; found ended at 6: started at 16 (10 s); at 17: again at 37 (20 s)
    assert started == ["coinbase"] * 2 and restarts["coinbase"]["at"] == 37.0
    threads["coinbase"] = _Thread(True)
    rec._supervise(threads, restarts, 40.0)
    assert len(started) == 2  # alive: left alone
    threads["coinbase"] = _Thread(False)
    restarts["coinbase"].update(at=None, started=40.0)
    rec._supervise(threads, restarts, 40.0 + live.STABLE_SECONDS)  # it had run a while: the short wait again
    assert restarts["coinbase"]["wait"] == live.RESTART_SECONDS


def test_an_unexpected_error_out_of_the_stream_loop_marks_it_down(tmp_path, monkeypatch):
    logged = []
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=logged.append,
                        connect=lambda url: None)
    rec._status["coinbase"]["connected"] = True

    def escaped(stream, store):
        raise RuntimeError("escaped")

    monkeypatch.setattr(rec, "_stream_loop", escaped)
    rec._run_stream(rec.streams[0])  # returns: the supervisor starts it again
    assert rec._status["coinbase"]["connected"] is False and "escaped" in rec._status["coinbase"]["error"]
    assert any("stream stopped" in line for line in logged)


def test_the_recorder_ends_other_recorders_only_later_and_only_holding_its_lock(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(live, "FLUSH_SECONDS", 0.01)
    reaped = []
    monkeypatch.setattr(live, "reap_strays", lambda log: reaped.append(1))
    locks = live.take_locks(tmp_path / "m.db")

    def run_briefly():
        rec = live.Recorder(tmp_path / "m.db", coins=(), sources=(), log=lambda *_: None, connect=lambda url: None,
                            locks=locks)
        threading.Timer(0.2, rec.stop.set).start()
        rec.run()

    try:
        run_briefly()
        assert reaped == []  # not as it starts: its lock check has not looked yet
        monkeypatch.setattr(live, "FIRST_REAP_SECONDS", 0)
        run_briefly()
        assert reaped == [1]
        live.machine_lock().write_text("4242 7")  # another recorder holds the machine's lock now
        run_briefly()
        assert reaped == [1]  # it ends nobody: it is the one to stop
    finally:
        live.release_locks(locks)


def test_a_lock_is_never_seen_empty_and_one_being_written_is_held(tmp_path):
    import os
    import time

    from massive_collate import proc

    lock = tmp_path / "run.lock"
    assert proc.acquire(lock) and lock.read_text().split()[0] == str(os.getpid())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.lock"]  # written whole elsewhere, then linked
    lock.unlink()
    lock.write_text("")  # another process created it and has not written its id yet
    assert not proc.acquire(lock) and lock.read_text() == ""
    old = time.time() - 2 * proc.FRESH_SECONDS  # left empty long ago: its owner died writing it
    os.utime(lock, (old, old))
    assert proc.acquire(lock) and proc.owns(lock)
    proc.release(lock)


T0 = 1_792_764_000_000  # 2026-10-23 14:00 UTC, ms


def test_an_iex_correction_that_keeps_its_trade_id_is_not_deleted_with_the_original(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=(), stock_keys=("PKTESTKEY", "test-secret"), log=lambda *_: None,
                        connect=lambda url: None)
    iex = rec.streams[0]
    iex.confirmed = frozenset({"AAPL"})
    rec._since["iex"] = T0
    rec._subs["iex"] = {"AAPL": T0}

    def receive(msgs):
        for ticker, t in iex.parse(msgs):
            rec._buffer[("iex", ticker)].append(t)

    trade = {"T": "t", "S": "AAPL", "i": 7, "x": "V", "p": 230.0, "s": 100, "c": ["@"], "t": "2026-10-23T14:00:01Z",
             "z": "C"}
    correction = {"T": "c", "S": "AAPL", "x": "V", "oi": 7, "op": 230.0, "os": 100, "oc": ["@"], "ci": 7, "cp": 231.0,
                  "cs": 100, "cc": ["@"], "t": "2026-10-23T14:00:01Z", "z": "C"}
    close = "SELECT close FROM built_bars WHERE timespan='minute'"
    receive([trade])
    with Store(tmp_path / "m.db") as store:
        rec.flush(store, now_ms=T0 + 5_000)
        receive(correction)  # in a later flush: the stored original goes, the correction stays
        rec.flush(store, now_ms=T0 + 7_000)
        assert store.trade_edges("iex", "AAPL") == ((T0 + 1000) * 1_000_000,) * 2
        assert store._bars("iex").execute(close).fetchone() == (231.0,)
        receive([{**trade, "i": 8, "t": "2026-10-23T14:00:02Z"}])
        receive({**correction, "oi": 8, "ci": 8, "cp": 232.0, "t": "2026-10-23T14:00:02Z"})  # both in one flush
        rec.flush(store, now_ms=T0 + 9_000)
        assert store.trade_edges("iex", "AAPL") == ((T0 + 1000) * 1_000_000, (T0 + 2000) * 1_000_000)
        assert store._bars("iex").execute(close).fetchone() == (232.0,)  # the correction stands
        assert json.loads(store.kv_get(live.STATUS_KEY))["streams"]["iex"]["pairs"]["AAPL"]["trades"] == 2


def test_live_derivations_are_kept_per_day_of_the_bars_and_per_rules(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    midnight = 1_792_800_000_000  # 2026-10-24 00:00 UTC
    rec._since["coinbase"] = midnight - 10_000
    for i, ms in enumerate((midnight - 2_000, midnight + 1_000)):  # one batch over midnight
        rec._buffer[("coinbase", "BTCUSD")].append(live._trade(ms * 1_000_000, i, 81000 + i, 0.1, i))
    with Store(tmp_path / "m.db") as store:
        rec.flush(store, now_ms=midnight + 5_000)
        (d,) = store.derivations(kind="bars-from-ticks")  # the same settings: one record, as wide as its bars
        assert (d["span_lo"], d["span_hi"]) == (midnight - live.DAY_MS, midnight + live.DAY_MS)  # not the flush's day
        assert d["rows"] == 4  # a second and a minute bar on each side of midnight

    stocks = live.Recorder(tmp_path / "m.db", coins=(), stock_keys=("K", "S"), log=lambda *_: None,
                           connect=lambda url: None)
    held = [{1: (True, True, True)}, {1: (False, True, True)}]

    class Rules:
        def condition_rules(self, feed):
            return held[0]

    first = stocks._tape_rules(Rules(), "iex")
    held.pop(0)
    stocks._rules["iex"] = (1.0, *stocks._rules["iex"][1:])  # read long ago: read again
    second = stocks._tape_rules(Rules(), "iex")
    assert first[1] != second[1]  # other rules, another digest: another derivation record
