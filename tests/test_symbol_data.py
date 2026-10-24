"""One symbol's data across feeds: what each feed sent, what was built here from it (*derived) and
what is not built yet (inventory.py, /api/inventory, `inventory`); derived frames built from the
span that changed only, whole at chunk edges, and on demand for one symbol."""
from __future__ import annotations

import pandas as pd
import pytest

from massive_collate import cli, derive, inventory
from massive_collate.gui import server
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store

ET = "America/New_York"


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _bar(t: str, price: float = 10.0, volume: float = 1.0, tz: str = ET) -> dict:
    return {"t": _ms(t, tz), "o": price, "h": price, "l": price, "c": price, "v": volume, "vw": price, "n": 1}


def _minutes(day: str, start: str = "10:00", n: int = 10, **kw) -> list[dict]:
    t0 = pd.Timestamp(f"{day} {start}", tz=ET)
    return [_bar(str((t0 + pd.Timedelta(minutes=i)).tz_localize(None)), **kw) for i in range(n)]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        yield s


def test_the_inventory_shows_each_feeds_sent_derived_and_missing_frames(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-20"))
    store.upsert_bars("massive", "SPCX", "day", 1, [_bar("2026-10-20 00:00")])
    store.upsert_bars("alpaca", "SPCX", "minute", 1, _minutes("2026-10-20"))
    for feed in ("massive", "alpaca"):
        store.watch(feed, ["SPCX"], "minute")
    derive.build_symbol(store, "massive", "SPCX", "stock", log=lambda *_: None)  # alpaca's not built yet

    inv = inventory.symbol_inventory(store, "spcx", rank=["alpaca", "massive"])
    assert [f["feed"] for f in inv["feeds"]] == ["alpaca", "massive"]  # as a merged chart takes them
    alpaca, massive = inv["feeds"]
    assert alpaca["notBuilt"] == ["5m", "30m", "1h", "2h", "4h", "1D", "1W", "1M", "1Y"]
    frames = {f["frame"]: f for f in massive["frames"]}
    assert set(frames["1m"]) >= {"sent"} and "derived" not in frames["1m"]
    assert "sent" in frames["1D"] and "derived" not in frames["1D"]  # the feed's own day: none built over it
    assert frames["5m"]["derived"]["rows"] == 2 and frames["5m"]["derived"]["how"]["from"] == "minute"
    assert frames["1W"]["derived"]["how"]["from"] == "day" and massive["notBuilt"] == []
    assert massive["watch"]["resolution"] == "minute"


def test_the_feed_inventory_reads_each_frame_once(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-20", n=3))
    store.upsert_built_bars("massive", "SPCX", "minute", 5, [_bar("2026-10-20 10:00")], 7)
    rows = store.frame_inventory("massive", "SPCX")
    assert [(r["table"], r["timespan"], r["multiplier"], r["rows"]) for r in rows] == [
        ("bars", "minute", 1, 3), ("built_bars", "minute", 5, 1)]
    assert rows[0]["first"] == _ms("2026-10-20 10:00") and rows[0]["last"] == _ms("2026-10-20 10:02")
    assert rows[1]["derivation"] == 7 and store.frame_inventory("massive", "MSFT") == []


def test_a_backfill_rebuilds_the_old_days_it_brought_only(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-20"))
    derive.build_chain(store, "massive", "SPCX", "minute", "stock")
    conn = store._bars("massive")
    conn.execute("UPDATE bars SET stored_at=1")
    conn.execute("UPDATE built_bars SET stored_at=1")
    conn.commit()
    store.kv_set(derive.KEY.format("massive", "SPCX", "minute"), "1000")
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-01-05", n=1))  # older history arrives
    assert store.changed_span("massive", "SPCX", "minute", 1000) == (_ms("2026-01-05 10:00"),) * 2
    assert derive.build_chain(store, "massive", "SPCX", "minute", "stock") == 6  # one bin per frame, that day
    assert conn.execute("SELECT COUNT(*) FROM built_bars WHERE stored_at=1").fetchone()[0] > 0  # October untouched


def test_a_cme_session_across_a_chunk_edge_is_built_whole(store, monkeypatch):
    monkeypatch.setitem(derive.CHUNK_DAYS, "minute", 1)  # a chunk a day: Sunday evening and Monday apart
    evening = _minutes("2026-10-18", "18:00", 5, volume=10.0)
    monday = _minutes("2026-10-19", "09:00", 5, volume=10.0)
    store.upsert_bars("massive", "NQZ6", "minute", 1, evening + monday)
    derive.build_chain(store, "massive", "NQZ6", "minute", "future")
    days = store._bars("massive").execute(
        "SELECT ts, volume FROM built_bars WHERE timespan='day'").fetchall()
    assert days == [(_ms("2026-10-19 00:00"), 100.0)]  # one session, every minute of it


def test_the_chart_app_serves_the_inventory_and_builds_on_demand(store, monkeypatch):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-20"))
    store.watch("massive", ["SPCX"], "minute")
    app = ChartApp(store)
    inv = app.inventory("SPCX")
    assert inv["feeds"][0]["feed"] == "massive" and "5m" in inv["feeds"][0]["notBuilt"]

    started = []

    class Proc:  # tests never start processes
        pid = 4242

        def __init__(self, args, **kw):
            started.append(args)

        def poll(self):
            return None  # still running

    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    assert app.start_derive("spcx") == {"started": True, "pid": 4242}
    assert started[0][-3:] == ["derive", "--tickers", "SPCX"]
    with pytest.raises(RuntimeError, match="already running"):
        app.start_derive("SPCX")
    with pytest.raises(LookupError):
        app.start_derive("NOPE")


def test_the_inventory_command_prints_the_frames(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "data" / "m.db"))
    monkeypatch.setenv("MASSIVE_OFFLINE", "1")
    with Store(tmp_path / "data" / "m.db") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-20"))
        s.watch("massive", ["SPCX"], "minute")
    assert cli.main(["inventory", "SPCX"]) == 0
    out = capsys.readouterr().out
    assert "1m     sent" in out and "5m     not built" in out
    assert cli.main(["derive", "--tickers", "SPCX"]) == 0
    capsys.readouterr()
    assert cli.main(["inventory", "SPCX"]) == 0
    assert "5m     *derived" in capsys.readouterr().out
    assert cli.main(["inventory", "NONE"]) == 1
