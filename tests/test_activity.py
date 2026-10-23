"""The fetcher activity view: live run, plan usage, per-symbol backfill state and the log tail."""
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


def _status(item, edges=None, current=None, refresh=12):
    return act.symbol_status(item, edges, FLOOR, TODAY, NOW, refresh, current)


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


def test_finished_symbols_are_fresh_then_due():
    done = _item(backfilled_to=FLOOR, synced_at=NOW - timedelta(hours=2))
    assert _status(done)["state"] == "fresh"
    assert _status(done | {"synced_at": NOW - timedelta(hours=13)})["state"] == "due"
    assert _status(done, current="AAPL")["state"] == "fetching"


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
