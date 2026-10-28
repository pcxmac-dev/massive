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
    assert not rec._status[live.TIINGO_BOATS]["connected"]
    rec, poller = _recorder(tmp_path)
    killswitch.strike(store, "tiingo", "fetch", "HTTP 429", now=TUE_2305 - timedelta(minutes=1))
    assert rec.boats_step(store) == live.BOATS_IDLE_SECONDS and poller.client.asked == []  # Tiingo's kill is its own
    rec, poller = _recorder(tmp_path)
    rec.set_wanted(())
    assert rec.boats_step(store) == live.BOATS_IDLE_SECONDS and poller.client.asked == []


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
    assert not rec._status[live.TIINGO_BOATS]["connected"]
