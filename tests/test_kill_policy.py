"""The kill policy (killswitch.py): a feed that refuses or keeps failing is cut off, longer each
time, in every process; every connection's and run's outcome is recorded; one recorder per
machine, the others ended."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from massive_collate import cli, killswitch, live, proc
from massive_collate.collector import sync
from massive_collate.config import TIERS
from massive_collate.errors import NotEntitled, RateLimited, SourceError
from massive_collate.gui import activity
from massive_collate.storage import Store

from .conftest import FakeSource

NOW = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.mark.parametrize("error, status, refused", [
    ("coinbase: Failed to subscribe subscription limit reached for channel: matches", None, True),
    ("closed by the server (1000)", None, False),
    ("closed by the server (1008)", None, True),
    ("closed by the server (4001)", None, True),
    ("WebSocket upgrade refused: HTTP/1.1 429 Too Many Requests", None, True),
    ("WebSocket upgrade refused: HTTP/1.1 503 Service Unavailable", None, False),
    ("alpaca 406: connection limit exceeded", None, True),
    ("alpaca 500: internal error", None, False),
    ("HTTP 404: unknown symbol", 404, False),
    ("slow down", 429, True),
])
def test_what_counts_as_the_server_saying_no(error, status, refused):
    assert killswitch.is_refusal(error, status) is refused


def test_each_kill_lasts_longer_until_a_quiet_day(store):
    k = killswitch.strike(store, "coinbase", "live", "refused", now=NOW)
    assert k["until"] == (NOW + timedelta(minutes=30)).isoformat(timespec="seconds") and k["strikes"] == 1
    assert killswitch.killed(store, "coinbase", "live", NOW + timedelta(minutes=29))
    assert killswitch.killed(store, "coinbase", "live", NOW + timedelta(minutes=31)) is None
    assert killswitch.killed(store, "coinbase", "fetch", NOW) is None  # the stream, not the fetching
    again = killswitch.strike(store, "coinbase", "live", "still refused", now=NOW + timedelta(minutes=5))
    assert again["until"] == k["until"] and again["strikes"] == 1  # killed already: the same kill
    second = killswitch.strike(store, "coinbase", "live", "refused", now=NOW + timedelta(hours=1))
    assert second["strikes"] == 2 and second["until"] == (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    later = killswitch.strike(store, "coinbase", "live", "refused", now=NOW + timedelta(days=3))
    assert later["strikes"] == 1  # a day and more without one: from the start again
    assert [e["outcome"] for e in store.feed_events("coinbase")].count("killed") == 3


def test_only_the_user_lifts_a_kill(store):
    killswitch.strike(store, "massive", "fetch", "429", now=NOW)
    assert [k["feed"] for k in killswitch.kills(store, ["massive", "alpaca"], NOW)] == ["massive"]
    assert killswitch.release(store, "massive") == ["fetch"]
    assert killswitch.killed(store, "massive", "fetch", NOW) is None
    assert store.feed_events("massive")[0]["outcome"] == "released"


class _WS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    def send(self, m):
        pass

    def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        raise ConnectionError("closed by the server (1000)")

    def close(self):
        pass


def _recorder(tmp_path, connect):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=connect)
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if rec._status["coinbase"].get("killed"):
            rec.stop.set()  # killed: the test ends here
        return rec.stop.is_set()

    rec.stop.wait = wait
    return rec, waits


def test_a_stream_the_server_refuses_is_killed_at_once(tmp_path):
    calls = []

    def connect(url):
        calls.append(url)
        return _WS([json.dumps({"type": "error", "message": "Failed to subscribe",
                                "reason": "subscription limit reached for channel: matches"})])

    rec, _ = _recorder(tmp_path, connect)
    rec._run_stream(rec.streams[0])
    assert len(calls) == 1  # not asked again
    with Store(tmp_path / "m.db") as s:
        k = killswitch.killed(s, "coinbase", "live")
        assert k and "subscription limit" in k["why"]
        assert [e["outcome"] for e in reversed(s.feed_events("coinbase"))] == ["connected", "refused", "killed"]
    assert rec._status["coinbase"]["error"].startswith("killed until")


def test_a_stream_the_server_keeps_dropping_is_killed(tmp_path):
    calls = []

    def connect(url):
        calls.append(url)
        return _WS([])  # dropped as soon as it connects

    rec, waits = _recorder(tmp_path, connect)
    rec._run_stream(rec.streams[0])
    assert len(calls) == killswitch.FLAP_LIMIT and waits[:3] == [1.0, 2.0, 4.0]  # backoff, then the kill
    with Store(tmp_path / "m.db") as s:
        assert "dropped within" in killswitch.killed(s, "coinbase", "live")["why"]


def test_a_killed_stream_is_not_connected(tmp_path):
    with Store(tmp_path / "m.db") as s:
        killswitch.strike(s, "coinbase", "live", "by hand", minutes=60)
    calls = []
    rec, _ = _recorder(tmp_path, lambda url: calls.append(url) or _WS([]))
    rec._run_stream(rec.streams[0])
    assert calls == []


def test_fetching_symbols_that_fail_on_the_server_in_a_row_kills_the_feed(store):
    class Down(FakeSource):
        def bar_pages(self, ticker, *a, **k):
            self._spend()
            raise SourceError(503, "service unavailable")

    store.watch("fake", ["A", "B", "C", "D"], "minute")
    result = sync(Down(entitled=("minute",)), store, TIERS["free"], NOW, log=lambda *_: None)
    assert result.stopped.startswith(f"{killswitch.FETCH_FAILURES} symbols in a row failed")
    assert killswitch.killed(store, "fake", "fetch")


def test_a_symbol_the_plan_does_not_cover_is_not_the_feed_refusing(store):
    class Plan(FakeSource):
        def bar_pages(self, ticker, *a, **k):
            if ticker == "NOPE":
                self._spend()
                raise NotEntitled(403, "forbidden for this plan")
            yield from super().bar_pages(ticker, *a, **k)

    store.watch("fake", ["NOPE", "GOOD"], "minute")
    sync(Plan(entitled=("minute",)), store, TIERS["free"], NOW, log=lambda *_: None)
    assert killswitch.killed(store, "fake", "fetch") is None


def test_a_rate_limited_run_kills_the_feed(store):
    class Limited(FakeSource):
        def bar_pages(self, ticker, *a, **k):
            self._spend()
            raise RateLimited(429, "too many requests")

    store.watch("fake", ["A"], "minute")
    result = sync(Limited(entitled=("minute",)), store, TIERS["free"], NOW, log=lambda *_: None)
    assert "rate limited" in result.stopped and killswitch.killed(store, "fake", "fetch")["strikes"] == 1


def test_nothing_is_sent_to_a_killed_feed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "data" / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "FAKE")
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("a request was sent"))
    assert cli.main(["watch", "add", "SPCX"]) == 0
    assert cli.main(["kills", "kill", "massive", "--kind", "fetch", "--hours", "2", "--why", "testing"]) == 0
    assert cli.main(["--source", "massive", "sync", "--only"]) == 0  # its run, as the hourly sync starts it
    with Store(tmp_path / "data" / "m.db") as s:
        assert s.feed_events("massive")[0]["outcome"] == "skipped"
    capsys.readouterr()
    assert cli.main(["kills"]) == 0
    out = capsys.readouterr().out
    assert "massive   fetch until" in out and "by hand: testing" in out and "1 skipped" in out
    assert cli.main(["kills", "release", "massive"]) == 0
    assert "fetch released" in capsys.readouterr().out


def test_the_fetcher_view_shows_a_killed_feed(store):
    from massive_collate.config import load_settings

    killswitch.strike(store, "coinbase", "fetch", "rate limited", now=NOW)
    rows = activity.health(store, load_settings(), NOW + timedelta(minutes=1), [], None)
    row = next(r for r in rows if r["source"] == "coinbase")
    assert row["state"] == "killed" and "kills release coinbase" in row["why"]
    assert not activity.can_fetch([row])


def test_outcomes_add_up_per_feed(store):
    for outcome, held in (("connected", None), ("dropped", 2.0), ("connected", None), ("dropped", 600.0),
                          ("connected", None), ("refused", 1.0)):
        store.feed_event("coinbase", "live", outcome, seconds=held, now=NOW)
    (row,) = store.feed_scores(NOW - timedelta(hours=1))
    assert row["outcomes"] == {"connected": 3, "dropped": 2, "refused": 1} and row["medianHeld"] == 2.0


def test_the_recorder_ends_other_recorders_but_not_itself_or_its_launcher(monkeypatch):
    me = proc.os.getpid()
    rows = [(1, 0, "explorer.exe"),
            (500, 1, r'"C:\venv\Scripts\pythonw.exe" -m massive_collate live'),  # this recorder's launcher
            (me, 500, r"C:\Python\pythonw.exe -m massive_collate live"),
            (700, 1, r"C:\venv\Scripts\pythonw.exe -m massive_collate live"),  # a stray, and its child
            (701, 700, r"C:\Python\pythonw.exe -m massive_collate live"),
            (800, 1, r"C:\venv\Scripts\python.exe -m massive_collate live status"),
            (900, 1, r"C:\venv\Scripts\python.exe -m massive_collate sync")]
    monkeypatch.setattr(proc, "processes", lambda: rows)
    assert sorted(live.recorders()) == [700, 701]
    ended, logged = [], []
    assert live.reap_strays(logged.append, terminate=lambda pid: ended.append(pid) or True) == 2
    assert sorted(ended) == [700, 701] and "ended 2 other recorder processes" in logged[0]


def test_tests_cannot_see_or_end_this_machines_processes():
    assert proc.processes() == []  # conftest: a test recorder once ended the user's real one
    with pytest.raises(AssertionError, match="tried to end"):
        proc.terminate(1)


def test_an_offline_copy_says_so_rather_than_unable_to_fetch(store, monkeypatch, tmp_path):
    from massive_collate.config import load_settings

    monkeypatch.setenv("MASSIVE_OFFLINE", "1")
    out = activity.activity(store, load_settings(), tmp_path / "m.db", "massive")
    assert out["offline"] is True and out["canFetch"] is False  # every feed off: the page says "offline copy"
    js = (activity.Path(activity.__file__).parent / "static" / "activity.js").read_text(encoding="utf-8")
    assert js.index("data?.offline") < js.index("Unable to fetch data currently")


def test_tests_cannot_start_processes_from_the_chart_app():
    from massive_collate.gui import server

    with pytest.raises(AssertionError, match="tried to start a process"):
        server.subprocess.Popen(["python", "-m", "massive_collate", "sync"])  # conftest: a test once started a real sync


def test_a_feed_on_another_feeds_account_is_killed_with_it_for_as_long_as_the_server_asked(store):
    # Blue Ocean's requests are Alpaca's account's: Alpaca's 429 is Blue Ocean's too
    k = killswitch.strike(store, "boats", "fetch", "HTTP 429: too many", now=NOW, at_least_s=3600)
    assert k["until"] == (NOW + timedelta(hours=1)).isoformat(timespec="seconds")  # not the ladder's 30 min
    assert killswitch.killed(store, "alpaca", "fetch", NOW + timedelta(minutes=59))
    assert killswitch.killed(store, "tiingo", "fetch", NOW) is None  # another account
    killswitch.strike(store, "tiingo", "fetch", "refused", now=NOW)
    assert killswitch.killed(store, "tiingofx", "fetch", NOW)  # Tiingo forex: Tiingo's key
    killswitch.strike(store, "coinbase", "fetch", "by hand", now=NOW, minutes=60)
    assert killswitch.killed(store, "bitstamp", "fetch", NOW) is None
    killswitch.strike(store, "massive", "fetch", "by hand", now=NOW, minutes=60)  # by hand: the feed named only
    assert killswitch.sharing("massive") == [] and sorted(killswitch.sharing("alpaca")) == ["boats"]


def test_a_429_carries_its_wait_and_a_key_with_a_line_break_is_never_sent_or_repeated():
    from massive_collate.errors import Unauthorized
    from massive_collate.sources.massive import MassiveClient

    from .conftest import FakeResponse, FakeSession

    session = FakeSession([FakeResponse(429, {"message": "slow down"}, {"Retry-After": "3600"})])
    client = MassiveClient("k", base_url="https://x.test", session=session, sleep=lambda s: None)
    with pytest.raises(RateLimited) as caught:
        client.get("/v2/aggs")
    assert caught.value.retry_after == 3600 and len(session.calls) == 1  # longer than a run waits: not tried again
    assert killswitch.retry_after_s(cli.collector.StopRun("rate limited")) is None

    class Refusing:
        calls = 0

        def get(self, *a, **k):
            Refusing.calls += 1
            raise requests.exceptions.InvalidHeader("Invalid leading whitespace in header value: 'Bearer SECRETKEY\n'")

    client = MassiveClient("SECRETKEY\n", base_url="https://x.test", session=Refusing(), sleep=lambda s: None)
    with pytest.raises(Unauthorized) as caught:
        client.get("/v2/aggs")
    assert "SECRETKEY" not in str(caught.value) and Refusing.calls == 1  # not repeated, not tried again
    # Bitfinex answers a list: read as the error it is, not a crash
    session = FakeSession([FakeResponse(429, ["error", 11010, "ratelimit: error"])])
    client = MassiveClient("k", base_url="https://x.test", session=session, sleep=lambda s: None, max_retries=0)
    with pytest.raises(RateLimited, match="ratelimit: error"):
        client.get("/v2/candles")


def test_keys_are_read_without_the_spaces_and_line_breaks_around_them():
    from massive_collate.config import load_settings

    s = load_settings(env={"MASSIVE_API_KEY": " mk\r\n", "TIINGO_API_KEY": "tk\n", "ALPACA_API_KEY_ID": " id ",
                           "ALPACA_API_SECRET_KEY": "sk\n"}, env_file=None)
    assert (s.api_key, s.tiingo_api_key, s.alpaca.key_id, s.alpaca.secret_key) == ("mk", "tk", "id", "sk")


def test_a_rate_limit_in_the_weekly_scan_stops_the_run_and_other_failures_do_not(store, monkeypatch):
    class Limited:
        scan = True

        def run_scan(self, source):
            raise RateLimited(429, "slow down", 600)

    class Odd(Limited):
        def run_scan(self, source):
            raise ValueError("an answer it could not read")

    monkeypatch.setattr(cli, "providers", lambda: {"x": Limited()})
    with pytest.raises(RateLimited):  # swallowed, the sync sent its requests into the 429 straight after
        cli._scan("x", object(), store, lambda *_: None)
    logged = []
    monkeypatch.setattr(cli, "providers", lambda: {"x": Odd()})
    cli._scan("x", object(), store, logged.append)
    assert logged and "capability scan failed" in logged[0]
