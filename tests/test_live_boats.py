"""Blue Ocean's night under way, live from Tiingo (live.BoatsPoller): polled while a night is open for
the stocks open on a chart, the minutes over stored final, the forming one built, on Tiingo's limits."""
from __future__ import annotations

from datetime import datetime, timedelta

from massive_collate import killswitch, live
from massive_collate.config import load_settings
from massive_collate.sessions import ET

TUE_2305 = datetime(2026, 10, 27, 23, 5, 40, tzinfo=ET)


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


class FakeTiingo:
    """Tiingo's /boats answers: the last sale and quote of each stock asked, and three minutes of SPCX."""

    def __init__(self):
        self.asked = []

    def get(self, path, params):
        self.asked.append((path, dict(params)))
        if path == "/boats/":
            return [{"ticker": t, "last": 149.89, "lastSaleTimestamp": "2026-10-27T23:05:30.168898757-04:00",
                     "bidPrice": 149.8, "askPrice": 149.95} for t in params["tickers"].split(",")]
        minute = lambda m, c: {"date": f"2026-10-28T03:0{m}:00.000Z", "open": 149.0, "high": 150.0, "low": 148.5,  # noqa: E731
                               "close": c, "volume": 100}
        return [minute(3, 149.5), minute(4, 149.7), minute(5, 149.89)]  # 23:03, 23:04, 23:05 ET (forming)


def _recorder(tmp_path, now=TUE_2305):
    poller = live.BoatsPoller(FakeTiingo(), now=lambda: now)
    rec = live.Recorder(tmp_path / "m.db", coins=(), boats=poller)
    rec.set_wanted(("SPCX",))
    return rec, poller


def test_only_on_a_paid_tiingo_plan_with_live_stocks_online():
    keys = {"TIINGO_API_KEY": "k"}
    assert live.boats_live(load_settings(env={**keys, "TIINGO_PLAN": "power"}, env_file=None))
    assert not live.boats_live(load_settings(env=keys, env_file=None))  # Starter: 50 an hour would all go on this
    assert not live.boats_live(load_settings(env={**keys, "TIINGO_PLAN": "power", "LIVE_STOCKS": "off"}, env_file=None))
    assert not live.boats_live(load_settings(env={"TIINGO_PLAN": "power"}, env_file=None))  # no key


def test_a_night_open_stores_the_minutes_over_and_builds_the_forming_one(tmp_path, store):
    rec, poller = _recorder(tmp_path)
    assert rec.boats_step(store) == 1.0
    assert [p for p, _ in poller.client.asked] == ["/boats/", "/boats/spcx/prices"]  # one request for every quote
    assert poller.client.asked[1][1]["startDate"] == "2026-10-28"  # the night's open, 20:00 ET, is UTC's 28th
    t = lambda h, m: _ms(datetime(2026, 10, 27, h, m, tzinfo=ET))  # noqa: E731
    own = store.bars_window([live.TIINGO_BOATS], "SPCX", "minute", t(20, 0), t(23, 10), filtered=False)
    assert own["ts"].tolist() == [t(23, 3), t(23, 4), t(23, 5)] and own["close"].tolist() == [149.5, 149.7, 149.89]
    assert store.bar_edges(live.TIINGO_BOATS, "SPCX", received_only=True) == (t(23, 3), t(23, 4))  # 23:05: built
    assert store.coverage("SPCX", "minute")[live.TIINGO_BOATS] == [(t(20, 0), t(23, 5))]  # held from the night's open
    status = rec._status[live.TIINGO_BOATS]
    assert status["connected"] and status["pairs"]["SPCX"]["price"] == 149.89 and status["symbols"] == ["SPCX"]
    assert "SPCX" in rec._touched  # the frames above a minute built with the next derive
    poller.client.asked.clear()
    rec.boats_step(store)
    assert poller.client.asked == []  # the quotes every 5 s, the minutes every 15 s: not again at once


def test_nothing_is_asked_by_day_while_killed_or_with_nothing_open(tmp_path, store):
    rec, poller = _recorder(tmp_path, now=datetime(2026, 10, 28, 12, 0, tzinfo=ET))
    assert rec.boats_step(store) == live.BOATS_IDLE_SECONDS and poller.client.asked == []
    status = rec._status[live.TIINGO_BOATS]
    assert not status["connected"] and status["idle"] and status["note"].startswith("by day")  # idle, not "down"
    rec, poller = _recorder(tmp_path)
    killswitch.strike(store, "tiingo", "fetch", "HTTP 429", now=TUE_2305 - timedelta(minutes=1))
    assert rec.boats_step(store) == live.BOATS_IDLE_SECONDS and poller.client.asked == []  # Tiingo's kill is its own
    assert rec._status[live.TIINGO_BOATS]["note"] == "paused: HTTP 429"
    rec, poller = _recorder(tmp_path)
    rec.set_wanted(())
    assert rec.boats_step(store) == live.BOATS_IDLE_SECONDS and poller.client.asked == []
    assert rec._status[live.TIINGO_BOATS]["idle"] and rec._status[live.TIINGO_BOATS]["note"] is None


def test_tiingo_names_and_times_are_read():
    assert live.BoatsPoller.symbol("BRK.B") == "brk-b"
    assert live._iso_ms("2026-10-28T03:43:14.003025142-04:00") == _ms(datetime(2026, 10, 28, 3, 43, 14, 3025, tzinfo=ET))
    assert live._iso_ms("2026-10-28T07:43:00.000Z") == _ms(datetime(2026, 10, 28, 3, 43, tzinfo=ET))
    assert live.TIINGO_BOATS in live.PARTIAL_FEEDS  # ranked last in a chart of all feeds, never for days


def test_a_429_kills_tiingos_fetching_and_its_forex_with_it(tmp_path):
    from massive_collate.errors import RateLimited
    from massive_collate.storage import Store

    rec, poller = _recorder(tmp_path)

    class Limited(FakeTiingo):
        def get(self, path, params):
            rec.stop.set()  # one step: the loop ends after it
            raise RateLimited(429, "too many requests", 120)

    poller.client = Limited()
    rec._boats_loop()
    with Store(tmp_path / "m.db") as store:
        kill = killswitch.killed(store, "tiingo", "fetch")
        assert kill and "live Blue Ocean" in kill["why"]
        assert killswitch.killed(store, "tiingofx", "fetch")  # the same key: not asked by the other feed either
    status = rec._status[live.TIINGO_BOATS]
    assert not status["connected"] and not status["idle"] and "too many requests" in status["error"]  # down, with why


def test_the_day_cap_shows_idle_on_tiingos_limits_and_any_other_error_down(tmp_path):
    from massive_collate.errors import BudgetExhausted

    for fail, idle, words in ((BudgetExhausted("the daily request cap is reached"), True, "Tiingo's limits"),
                              (ConnectionError("network down"), False, "network down")):
        rec, poller = _recorder(tmp_path)

        class Failing(FakeTiingo):
            def get(self, path, params, fail=fail):
                rec.stop.set()
                raise fail

        poller.client = Failing()
        rec._status[live.TIINGO_BOATS].update(connected=True)  # as after an earlier step
        rec._boats_loop()
        status = rec._status[live.TIINGO_BOATS]
        assert not status["connected"] and status["idle"] is idle
        assert words in (status["note"] if idle else status["error"])  # never left "connected" on old quotes


def test_a_lower_limit_found_while_a_client_runs_is_kept_from_then_on():
    from massive_collate.ratelimit import RateLimiter
    from massive_collate.sources.massive import MassiveClient

    client = MassiveClient("k", rate_limiter=RateLimiter(190, 60.0))
    assert client.tighten(95, 60.0) and client._limiter.max_calls == 95
    assert not client.tighten(190, 60.0) and client._limiter.max_calls == 95  # never looser
    assert not client.tighten(None, 60.0)
    assert client.tighten(1, 3.0) and (client._limiter.max_calls, client._limiter.period) == (1, 3.0)


def test_the_publications_thread_lives_through_a_file_that_will_not_open(tmp_path, monkeypatch):
    from massive_collate import gov

    rec = live.Recorder(tmp_path / "m.db", coins=(), publications=object(), log=lambda *_: None)
    opened = []

    def locked(path):
        opened.append(path)
        if len(opened) == 2:
            rec.stop.set()
        raise OSError("database is locked")

    monkeypatch.setattr(gov, "GovDB", locked)
    monkeypatch.setattr(rec.stop, "wait", lambda s: rec.stop.is_set())
    rec._publications_loop()  # returns once stopped: it did not die at the first
    assert len(opened) == 2
