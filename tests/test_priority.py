"""The symbol open in the chart goes first on every feed, and every server's limits hold across
runs (each run is a new process with a new limiter) and are shown with any wait."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from massive_collate import cli, collector
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings
from massive_collate.gui import server
from massive_collate.gui.activity import limits
from massive_collate.gui.server import ChartApp
from massive_collate.ratelimit import KvLedger, RateLimiter, describe, window_state
from massive_collate.sources.massive import MassiveClient
from massive_collate.storage import Store

from .conftest import FakeResponse, FakeSession, FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def _trades() -> list[dict]:
    day = NOW.date() - timedelta(days=1)
    ns = int(datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET).timestamp()) * 10**9
    return [{"sip_timestamp": ns + s * 10**9, "sequence_number": s, "price": 10.0 + s, "size": 100, "exchange": 4}
            for s in range(3)]


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now, self.sleeps = now, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# -- the symbol open in the chart goes first --------------------------------------------------

def test_a_requested_symbol_goes_first_even_when_asked_mid_run(store):
    src = FakeSource(trades=_trades())
    store.watch("fake", ["AAA", "BBB", "CCC", "DDD"], "trades")
    order = []

    def on_item(ticker):
        order.append(ticker)
        if ticker == "AAA":  # opened in the chart while the run is going
            collector.request_priority(store, "DDD", NOW)

    collector.request_priority(store, "CCC", NOW - timedelta(minutes=1))
    sync(src, store, FREE, NOW, log=lambda m: None, on_item=on_item)
    assert order == ["CCC", "AAA", "DDD", "BBB"]


def test_a_requested_symbol_skips_the_refresh_interval_but_not_its_plans_publication(store):
    src = FakeSource(entitled=("minute",))  # minute bars only: no 1-second window waits for its pass
    store.watch("fake", ["AAA"], "minute")
    sync(src, store, FREE, NOW, log=lambda m: None)
    later = NOW + timedelta(days=1)  # the next session is published
    assert sync(src, store, FREE, later, log=lambda m: None, refresh_hours=48).skipped == 1
    collector.request_priority(store, "AAA", later)
    assert sync(src, store, FREE, later, log=lambda m: None, refresh_hours=48).skipped == 0
    again = later + timedelta(minutes=1)  # nothing published since: asked or not, nothing to ask for
    collector.request_priority(store, "AAA", again)
    assert sync(src, store, FREE, again, log=lambda m: None, refresh_hours=48).skipped == 1


def test_requests_expire_and_count_the_runs_started_for_them(store):
    collector.request_priority(store, "aaa", NOW)
    collector.priority_started(store, "AAA")
    assert collector.priority_requests(store, NOW)["AAA"] == {"at": NOW, "runs": 1}
    assert collector.priority_requests(store, NOW + timedelta(hours=collector.PRIORITY_HOURS + 1)) == {}


def test_opening_a_symbol_starts_a_run_for_it_or_queues_it_behind_the_running_one(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db", check_same_thread=False)
    for feed in ("massive", "alpaca"):
        store.watch(feed, ["SPCX"], "minute", exchange="XNAS")
    started = []

    class Proc:
        pid = 4242

    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: started.append(args) or Proc())
    monkeypatch.setattr(server.activity, "lock_owner", lambda path: None)
    app = ChartApp(store)
    first = app.priority("spcx")
    assert first["state"] == "started" and first["feeds"] == ["alpaca", "massive"]
    assert started[0][-3:] == ["sync", "--tickers", "SPCX"]
    assert app.priority("SPCX")["state"] == "recent" and len(started) == 1  # opened again within minutes
    monkeypatch.setattr(server.activity, "lock_owner", lambda path: 99)  # a sync is running
    monkeypatch.setattr(app, "PRIORITY_COOLDOWN", 0)
    assert app.priority("SPCX")["state"] == "queued" and len(started) == 1  # it takes the symbol next
    monkeypatch.setattr(server.activity, "lock_owner", lambda path: None)
    monkeypatch.setattr(app, "PRIORITY_COOLDOWN", 300)
    assert app.priority("SPCX")["state"] == "started" and len(started) == 2  # the run ended: its turn now
    with pytest.raises(LookupError):
        app.priority("NOPE")
    store.close()


def test_sync_takes_tickers():
    args = cli.build_parser().parse_args(["sync", "--tickers", "spcx", "aapl"])
    assert args.tickers == ["spcx", "aapl"] and args.all_sources


# -- limits held across runs, and shown -------------------------------------------------------

def test_a_new_run_continues_the_window_the_last_one_left(store):
    clock = FakeClock()
    first = RateLimiter(5, 60.0, clock=clock, sleep=clock.sleep)
    first.share(KvLedger(store, "massive"))
    for _ in range(5):  # all in the same millisecond: still five calls
        first.acquire()
    assert clock.sleeps == [] and len(json.loads(store.kv_get("ratelimit:massive"))) == 5
    second = RateLimiter(5, 60.0, clock=clock, sleep=clock.sleep)  # the next run: a new process
    waits = []
    second.on_wait = waits.append
    second.share(KvLedger(store, "massive"))
    second.acquire()
    assert waits == [60.0] and clock.sleeps == [60.0]  # it waited out the window the first one filled


def test_window_state_and_descriptions():
    assert window_state([0, 10, 20, 30, 40], 5, 60.0, now=50) == {"used": 5, "allowed": 5, "periodSeconds": 60.0,
                                                                   "freeIn": 10.0}
    assert window_state([0, 10], 5, 60.0, now=50)["freeIn"] == 0 and window_state([0], 5, 60.0, now=70)["used"] == 0
    assert describe(5, 60.0) == "5 requests per minute" and describe(45, 3600.0) == "45 requests per hour"
    assert describe(None, 60.0) == "no limit"


def test_a_servers_retry_wait_is_reported():
    session = FakeSession([FakeResponse(429, {}, headers={"Retry-After": "120"}), FakeResponse(200, {"ok": 1})])
    slept, told = [], []
    client = MassiveClient("KEY", session=session, sleep=slept.append)
    client.on_retry = lambda s, status: told.append((s, status))
    assert client.get("/x") == {"ok": 1}
    assert told == [(120.0, 429)] and slept == [120.0]


def test_limits_list_every_server_with_its_window_and_wait(store):
    settings = load_settings(env={}, env_file=None)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    t = now.timestamp()
    store.kv_set("ratelimit:massive", json.dumps([t - 50, t - 40, t - 30, t - 20, t - 10]))
    store.kv_set("fetch:wait:massive", json.dumps({"until": t + 10, "seconds": 10, "reason": "limit: 5 requests per minute"}))
    store.kv_set("fetch:wait:coinbase", json.dumps({"until": t - 5, "seconds": 1, "reason": "over"}))
    rows = {r["source"]: r for r in limits(store, settings, now)}
    assert {"massive", "alpaca", "bitstamp", "coinbase", "bitfinex", "cboe", "tiingo"} <= set(rows)
    m = rows["massive"]
    assert m["keptTo"] == "5 requests per 62 s" and m["window"]["used"] == 5 and m["window"]["freeIn"] == 12.0
    assert m["wait"]["reason"].startswith("limit") and rows["coinbase"]["wait"] is None  # a past wait is over
    assert rows["tiingo"]["keptTo"] == "45 requests per hour" and "10 requests per second" in rows["coinbase"]["published"]
    assert m["perDay"] == 2000 and m["leftToday"] == 2000


def test_an_offline_copy_of_the_app_starts_no_runs(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db", check_same_thread=False)
    store.watch("massive", ["SPCX"], "minute", exchange="XNAS")
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    app = ChartApp(store, settings=load_settings(env={"MASSIVE_OFFLINE": "1"}, env_file=None))
    assert app.priority("SPCX")["state"] == "offline"
    with pytest.raises(RuntimeError):
        app.start_scan("SPCX")
    store.close()


def test_pausing_stops_background_fetching_before_its_next_request_and_resumes(tmp_path):
    from massive_collate.errors import Paused

    now = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
    with Store(tmp_path / "m.db") as store:
        assert collector.pause_state(store, now) is None
        p = collector.set_pause(store, True, now, minutes=600)  # asked for 10 h: two at most
        assert p["until"] == "2026-10-24T20:00:00+00:00" and collector.pause_state(store, now)["until"] == p["until"]
        assert collector.pause_state(store, now + timedelta(hours=2, seconds=1)) is None  # it ends on its own
        real = datetime.now(timezone.utc)  # the client's check reads the clock: a pause from now
        until = collector.set_pause(store, True, real, minutes=30)["until"]
        check = cli._pause_check(store, every=0)
        assert check() == f"fetching paused by you until {until[11:16]} UTC"

        session = FakeSession([FakeResponse(body={"results": []})])
        client = MassiveClient("k", session=session, request_budget=10)
        client.should_stop = check
        with pytest.raises(Paused):
            client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
        assert session.calls == [] and client.requests_made == 0  # stopped before sending anything

        def pages():
            raise Paused("fetching paused by you until 20:00 UTC")
            yield []
        with pytest.raises(collector.StopRun, match="paused by you"):
            list(collector._guard(pages()))

        collector.set_pause(store, False, now)
        assert collector.pause_state(store, now) is None and check() is None


def test_the_chart_pauses_and_resumes_fetching(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store = Store(tmp_path / "m.db", check_same_thread=False)
    app = ChartApp(store)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: SimpleNamespace(pid=7))  # no real sync
    assert app.set_fetching({"paused": True})["paused"]["until"]
    assert collector.pause_state(store) is not None
    assert app.set_fetching({"paused": False}) == {"paused": None, "started": 7}  # resuming starts a sync
    store.close()


def test_a_symbol_still_backfilling_is_never_skipped_for_want_of_new_data(store):
    """A plan reaching further back (Massive Advanced: from 2003) leaves synced symbols with history
    to fill: nothing new published must not keep them waiting for the next session."""
    src = FakeSource(trades=_trades())
    store.watch("fake", ["AAA"], "minute")
    sync(src, store, FREE, NOW, log=lambda m: None)
    later = NOW + timedelta(minutes=5)  # nothing published since
    assert sync(src, store, FREE, later, log=lambda m: None).skipped == 1  # complete: left alone
    store.reset_backfill("fake", "AAA")  # as a plan with more history leaves it
    assert sync(src, store, FREE, later, log=lambda m: None).skipped == 0


def test_a_symbol_asked_for_after_the_run_passed_it_is_taken_next(tmp_path):
    now = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
    with Store(tmp_path / "m.db") as store:
        store.watch("massive", ["AMD", "NVDA", "MU"], "minute")
        store.set_synced("massive", "AMD", now - timedelta(hours=1))  # skipped as up to date: no longer in the line
        pending = [w for w in store.watchlist("massive") if w["ticker"] != "AMD"]
        collector.request_priority(store, "AMD", now)  # the chart opens it while the run goes on
        served = {}
        item, urgent = collector._next_item(pending, store, now, "massive", served)
        assert item["ticker"] == "AMD" and urgent and len(pending) == 2  # fetched from the watchlist again
        item, urgent = collector._next_item(pending, store, now, "massive", served)
        assert item["ticker"] != "AMD" and not urgent  # taken once


def test_a_halt_stops_every_fetch_until_resumed_and_a_pause_only_the_background(tmp_path):
    with Store(tmp_path / "m.db") as store:
        collector.set_pause(store, True, minutes=30)
        assert cli._pause_check(store, every=0)().startswith("fetching paused by you")
        assert cli._pause_check(store, every=0, halt_only=True)() is None  # the open symbol's run goes on
        collector.set_pause(store, True, halt=True)
        assert collector.pause_state(store)["halt"]
        assert cli._pause_check(store, every=0, halt_only=True)() == "fetching halted by you"  # it too stops
        collector.set_pause(store, False)
        assert collector.pause_state(store) is None


def test_the_chart_halts_and_resumes_and_resuming_starts_a_sync(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db", check_same_thread=False)
    store.watch("massive", ["SPCX"], "minute")
    app = ChartApp(store, settings=load_settings(env={}, env_file=None))
    started = []

    class Proc:  # tests never start processes
        pid = 4343

        def __init__(self, args, **kw):
            started.append(args)

    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    assert app.set_fetching({"paused": True, "halt": True})["paused"]["halt"]
    assert app.priority("SPCX")["state"] == "halted" and started == []  # nothing starts while halted
    with pytest.raises(RuntimeError, match="halted"):
        app.start_scan("SPCX")
    out = app.set_fetching({"paused": False})
    assert out["paused"] is None and out["started"] == 4343 and started[-1][-2:] == ["massive_collate", "sync"]


def test_opening_an_up_to_date_symbol_asks_for_nothing(tmp_path, monkeypatch):
    # out of session (Sunday) with its last session stored: nothing new can exist, so no request
    # and no "next in line" (it read so for hours while a long sync ran)
    from datetime import datetime as dt

    from massive_collate import collector
    from massive_collate.collector import ET

    store = Store(tmp_path / "m.db", check_same_thread=False)
    store.watch("massive", ["SPCX"], "minute", exchange="XNAS")
    store.set_backfilled("massive", "SPCX", collector.history_floor(TIERS["free"], dt(2026, 10, 25).date(), "stock"))
    store.set_synced("massive", "SPCX", dt(2026, 10, 24, 9, 0, tzinfo=ET))  # after Friday's session was released
    monkeypatch.setattr(server.activity, "lock_owner", lambda path: 99)  # a long sync is running
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))

    class Sunday(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 10, 26, 3, 0, tzinfo=timezone.utc)  # Sunday 23:00 ET

    monkeypatch.setattr(server, "datetime", Sunday)
    app = ChartApp(store, settings=load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None))
    out = app.priority("SPCX")
    assert out["state"] == "current" and out["nextDue"]
    assert collector.priority_requests(store, Sunday.now(timezone.utc)) == {}  # nothing asked of the running sync
    store.close()


def test_an_opened_symbol_waits_only_for_the_feeds_with_something_to_fetch(tmp_path, monkeypatch):
    # every feed up to date but Tiingo, a closed archive that has not visited SPCX yet: nothing to
    # wait for (it read "next in line · coinbase is fetching" for hours); once Massive has a new
    # session to fetch, the request names Massive alone
    from datetime import datetime as dt

    store = Store(tmp_path / "m.db", check_same_thread=False)
    for feed in ("massive", "tiingo"):
        store.watch(feed, ["SPCX"], "minute", exchange="XNAS")
    store.set_backfilled("massive", "SPCX", collector.history_floor(FREE, dt(2026, 10, 25).date(), "stock"))
    store.set_synced("massive", "SPCX", dt(2026, 10, 24, 9, 0, tzinfo=ET))  # after Friday's session was released
    monkeypatch.setattr(server.activity, "lock_owner", lambda path: 99)  # a long sync is running
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    clock = {"now": dt(2026, 10, 26, 3, 0, tzinfo=timezone.utc)}  # Sunday 23:00 ET

    class Fixed(dt):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(server, "datetime", Fixed)
    app = ChartApp(store, settings=load_settings(env={"MASSIVE_API_KEY": "k", "TIINGO_API_KEY": "t"}, env_file=None))
    out = app.priority("SPCX")
    assert out["state"] == "current" and out["feeds"] == ["massive", "tiingo"]
    assert collector.priority_requests(store, clock["now"]) == {}  # nothing asked of the running sync
    clock["now"] = dt(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # Tuesday: Monday's session is out
    out = app.priority("SPCX")
    assert out["state"] == "queued" and out["need"] == ["massive"]
    store.close()
