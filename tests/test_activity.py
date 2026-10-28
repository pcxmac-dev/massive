"""The fetcher activity view: live run, plan usage, per-symbol backfill state and the log tail."""
import calendar
from datetime import date, datetime, timedelta, timezone

from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings
from massive_collate.gui import activity as act
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store
from massive_collate.symbols import resolve

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
TODAY = NOW.date()
FLOOR = date(2024, 10, 22)


def _item(**kw):
    base = {"ticker": "AAPL", "asset": "stock", "resolution": "minute", "backfilled_to": None,
            "listed_from": None, "synced_at": None}
    return base | kw


def _status(item, edges=None, current=None, now=NOW):
    return act.symbol_status(item, edges, FLOOR, now.date(), now, TIERS["free"], current)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 10, tzinfo=ET).timestamp() * 1000)


def test_new_symbol_is_queued_with_a_request_estimate():
    s = _status(_item())
    assert s["state"] == "queued" and s["progress"] == 0 and s["first"] is None
    assert 8 <= s["requestsLeft"] <= 16  # two years of minute bars, ~50k bars per request


def test_half_backfilled_symbol_shows_progress():
    half = TODAY - (TODAY - FLOOR) / 2
    s = _status(_item(backfilled_to=half), edges=(_ms(half), _ms(TODAY)))
    assert s["state"] == "backfilling" and 0.45 < s["progress"] < 0.55
    assert 0 < s["requestsLeft"] < _status(_item())["requestsLeft"]


def test_backfill_target_is_the_listing_date_for_young_stocks():
    listed = date(2026, 6, 12)
    s = _status(_item(listed_from=listed, backfilled_to=listed), edges=(_ms(listed), _ms(TODAY)))
    assert s["progress"] == 1 and s["target"] == "2026-06-12" and s["requestsLeft"] == 0


def test_finished_symbols_are_current_until_the_next_session_is_published():
    done = _item(backfilled_to=FLOOR)
    yesterday = (_ms(FLOOR), int(datetime(2026, 10, 20, 19, 59, tzinfo=ET).timestamp() * 1000))
    s = _status(done, yesterday)
    assert s["state"] == "current" and s["nextDue"].startswith("2026-10-22T04:00")
    assert _status(done, yesterday, now=NOW + timedelta(days=1))["state"] == "new"
    assert _status(done, yesterday, current="AAPL")["state"] == "fetching"


def test_log_tail_reads_only_the_end(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("".join(f"line {i}\n" for i in range(20000)), encoding="utf-8")
    tail = act.log_tail(log, 5)
    assert tail == [f"line {i}" for i in range(19995, 20000)]
    assert act.log_tail(tmp_path / "missing.log") == []


def test_sync_reports_the_symbol_it_is_working_on_and_its_request_cost(store):
    store.watch_spec("fake", resolve("IXIC"))
    seen, lines = [], []
    src = FakeSource(entitled=("minute",), bars={"minute": [{"t": _ms(date(2026, 10, 20)), "o": 1, "h": 1, "l": 1, "c": 1}]})
    sync(src, store, TIERS["free"], NOW, log=lines.append, on_item=seen.append)
    assert seen == ["IXIC"]
    assert any(line.startswith("IXIC minute:") and "requests)" in line for line in lines)


def test_activity_endpoint_summarises_store_runs_and_log(tmp_path):
    db = tmp_path / "massive.db"
    settings = load_settings(env={"MASSIVE_DB": str(db)}, env_file=None)
    with Store(db, check_same_thread=False) as s:
        s.watch_spec("massive", resolve("AAPL"))
        s.watch_spec("massive", resolve("MSFT"))
        s.upsert_bars("massive", "AAPL", "minute", 1, [{"t": _ms(date(2026, 10, 20)), "o": 1, "h": 1, "l": 1, "c": 1}])
        run = s.start_run("massive", "sync")
        s.set_run_requests(run, 42)
        s.set_run_current(run, "AAPL")
        (tmp_path / "logs").mkdir()
        act.log_path(db).write_text("2026-10-21T12:00:00 sync: source=massive\n2026-10-21T12:00:05 AAPL minute: 5 rows\n",
                                    encoding="utf-8")
        out = ChartApp(s, settings=settings).activity({})
    assert out["today"]["requests"] == 42 and out["plan"]["maxPerDay"] == 2000
    assert out["running"] is False  # no live process holds the lock
    assert out["runs"][0]["status"] == "abandoned" and out["runs"][0]["current"] == "AAPL"
    assert {x["ticker"]: x["state"] for x in out["symbols"]} == {"AAPL": "backfilling", "MSFT": "queued"}
    assert out["backfill"]["total"] == 2 and out["backfill"]["requestsLeft"] > 0 and out["backfill"]["runs"] == 1
    assert out["log"][-1].endswith("AAPL minute: 5 rows")


def test_activity_sees_a_live_run(tmp_path, monkeypatch):
    import os

    db = tmp_path / "massive.db"
    settings = load_settings(env={"MASSIVE_DB": str(db)}, env_file=None)
    with Store(db, check_same_thread=False) as s:
        s.watch_spec("massive", resolve("AAPL"))
        run = s.start_run("massive", "sync")
        s.set_run_current(run, "AAPL")
        db.with_suffix(".lock").write_text(str(os.getpid()))
        out = act.activity(s, settings, db, now=datetime.now(timezone.utc))
    assert out["running"] and out["live"]["current"] == "AAPL"
    assert out["symbols"][0]["state"] == "fetching"


def test_every_source_has_a_state_and_the_status_bar_knows_when_none_can_fetch(tmp_path):
    from datetime import datetime, timezone

    from massive_collate.config import load_settings
    from massive_collate.gui.activity import can_fetch, health, limits
    from massive_collate.storage import Store

    now = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
    settings = load_settings(env={"MASSIVE_API_KEY": "k", "MASSIVE_MAX_REQUESTS_PER_DAY": "100"}, env_file=None)
    with Store(tmp_path / "m.db") as store:
        rid = store.start_run("coinbase", "sync")
        store.finish_run(rid, 3000, 9000, "stopped", "request cap reached (request cap of 3000 reached)")
        rid = store.start_run("bitstamp", "sync")
        store.finish_run(rid, 2, 0, "error", "HTTPSConnectionPool: Max retries exceeded (network down)")
        rid = store.start_run("bitfinex", "sync")
        store.finish_run(rid, 40, 0, "stopped", "rate limited (HTTP 429: slow down); stopping until the next run")
        rid = store.start_run("massive", "sync")
        store.finish_run(rid, 100, 5, "ok", "")
        store.set_run_requests(rid, 100)
        rows = {h["source"]: h for h in health(store, settings, now, limits(store, settings, now), None)}
    assert rows["coinbase"]["state"] == "ok" and "continues next run" in rows["coinbase"]["why"]
    assert rows["bitstamp"]["state"] == "down" and "network down" in rows["bitstamp"]["why"]
    assert rows["bitfinex"]["state"] == "limited"
    assert rows["tiingo"]["state"] == "off" and "TIINGO_API_KEY" in rows["tiingo"]["why"]
    assert rows["cboe"]["state"] == "idle"
    assert can_fetch(list(rows.values()))
    assert not can_fetch([{"state": "down"}, {"state": "limited"}, {"state": "off"}])
    assert not can_fetch([{"state": "off"}])


def test_the_fetcher_shows_the_rows_most_worth_seeing_and_counts_the_rest(tmp_path):
    import json
    from datetime import datetime, timezone

    from massive_collate.config import load_settings
    from massive_collate.gui.activity import activity
    from massive_collate.storage import Store

    settings = load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None)
    with Store(tmp_path / "m.db") as store:
        tickers = [f"T{i:04d}" for i in range(400)]
        store.watch("massive", tickers, "minute")
        store.kv_set("watchlist:order", json.dumps({"T0399": 0, "T0398": 1}))  # the most traded first
        out = activity(store, settings, tmp_path / "m.db", "massive", datetime(2026, 10, 24, 18, tzinfo=timezone.utc),
                       focus="t0350")
    shown = [s["ticker"] for s in out["symbols"]]
    assert out["symbolTotal"] == 400 and sum(out["symbolCounts"].values()) == 400
    assert len(shown) == 301 and "T0350" in shown  # 300 by relevance, and the open symbol beyond them
    assert shown[:2] == ["T0399", "T0398"]  # within a state, the initializer's order
    assert out["backfill"]["total"] == 400  # tiles count every symbol


def test_the_status_keeps_its_symbol_rows_a_while_and_rebuilds_them_when_another_symbol_is_fetched(tmp_path, monkeypatch):
    db = tmp_path / "massive.db"
    settings = load_settings(env={"MASSIVE_DB": str(db)}, env_file=None)
    built = []
    real = act.symbol_rows
    monkeypatch.setattr(act, "symbol_rows", lambda *a, **k: built.append(a[4]) or real(*a, **k))
    with Store(db, check_same_thread=False) as s:
        s.watch_spec("massive", resolve("AAPL"))
        cache: dict = {}
        first = act.activity(s, settings, db, rows_cache=cache, rows_ttl_s=60)
        again = act.activity(s, settings, db, rows_cache=cache, rows_ttl_s=60)
        assert len(built) == 1 and again["symbols"] == first["symbols"]  # kept: each build seeks every feed file
        monkeypatch.setattr(act, "fetching_feeds", lambda _db: {"massive": 1234})  # a run fetching AAPL now
        run = s.start_run("massive", "sync")
        s.set_run_current(run, "AAPL")
        act.activity(s, settings, db, rows_cache=cache, rows_ttl_s=60)
        assert built == [None, "AAPL"]  # another symbol being fetched: built again at once
        act.activity(s, settings, db)  # no cache: built every time (tests, the command line)
        assert len(built) == 3


def test_the_fetcher_shows_every_feed_fetching_side_by_side(tmp_path):
    import os

    from massive_collate import proc

    db = tmp_path / "massive.db"
    settings = load_settings(env={"MASSIVE_DB": str(db), "MASSIVE_API_KEY": "k"}, env_file=None)
    with Store(db, check_same_thread=False) as s:
        s.watch_spec("massive", resolve("AAPL"))
        dead = s.start_run("bitstamp", "sync")  # its process died: no lock held
        for feed, ticker in (("massive", "AAPL"), ("alpaca", "SPCX")):
            s.set_run_current(s.start_run(feed, "sync"), ticker)
            proc.run_lock_path(db, feed).write_text(str(os.getpid()))
        for _ in range(30):  # the quick feeds' runs since: Massive's pass is older than the newest 25
            s.finish_run(s.start_run("coinbase", "sync"), 0, 0, "ok", "")
        out = act.activity(s, settings, db, source="alpaca", now=datetime.now(timezone.utc))
    assert {f["source"]: f["current"] for f in out["fetching"]} == {"massive": "AAPL", "alpaca": "SPCX"}
    assert out["running"] and out["live"]["source"] == "alpaca"  # this feed's own run
    states = {h["source"]: h["state"] for h in out["health"]}
    assert states["massive"] == states["alpaca"] == "fetching"
    assert next(r for r in out["runs"] if r["id"] == dead)["status"] == "abandoned"


def test_a_feeds_month_against_its_allowance_with_what_lapsed_and_where_the_pace_takes_it():
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    by_day = {"2026-09-30": 999, "2026-10-25": 100, "2026-10-26": 50, "2026-10-27": 80, "2026-10-28": 40}
    m = act.month_allowance(by_day, 100, now)
    assert m["used"] == 270 and m["allowance"] == 3100  # October: 31 days at 100 (September's not counted)
    assert m["lapsed"] == 24 * 100 + 50 + 20  # a day's cap does not carry over
    assert m["left"] == 60 + 3 * 100  # the rest of today and three days
    # the pace of the last three whole days and half of today (270 in 3.5 days): the rest of today, then
    # three days, none past its cap
    assert m["pace"] == 77 and m["projected"] == round(270 + 270 / 3.5 * 0.5 + 270 / 3.5 * 3) == 540
    assert m["share"] == round(540 / 3100, 4)
    # a feed that began yesterday: its pace from yesterday and today, not the empty days before
    started = act.month_allowance({"2026-10-27": 600, "2026-10-28": 1000}, 3000, now)
    assert started["pace"] == round(1600 / 1.5) and started["lapsed"] == 26 * 3000 + 2400


def test_a_months_first_day_paces_on_today_and_no_cap_has_no_allowance():
    now = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)  # a quarter of the day gone
    m = act.month_allowance({"2026-11-01": 25}, 100, now)
    assert m["lapsed"] == 0 and m["pace"] == 100 and m["projected"] == 3000 and m["share"] == 1.0
    assert act.month_allowance({}, 0, now)["share"] is None


def test_the_limits_carry_each_feeds_month(tmp_path):
    now = datetime.now(timezone.utc)
    settings = load_settings(env={"MASSIVE_API_KEY": "k", "MASSIVE_MAX_REQUESTS_PER_DAY": "100"}, env_file=None)
    with Store(tmp_path / "m.db") as store:
        store.count_request("massive", 30, now)
        row = next(r for r in act.limits(store, settings, now) if r["source"] == "massive")
    assert row["month"]["used"] == 30 and row["month"]["allowance"] == 100 * calendar.monthrange(now.year, now.month)[1]
