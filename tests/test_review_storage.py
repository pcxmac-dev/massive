"""Storage findings of the code review (CODE_REVIEW.md 8, 9, 26-31 and the storage "Low" items):
a second rebuild keeps the version the first made, compaction by copying (never in place on a large
file), a release rebuilds what was built over it, a built bar fills a pruned feed bar's place in the
filtered view, listings and summaries by seeks, the coverage ledger keyed by origin, keys out of
reprs, and the transactions, migrations, revisions, export days, composite and settings beside them."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import date

import pandas as pd
import pytest

from massive_collate import baddata, cli, composite, config, derive
from massive_collate.storage import Store
from massive_collate.storage import admin as admin_mod
from massive_collate.storage import feeds as feeds_mod
from massive_collate.storage.common import add_column

ET = "America/New_York"


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _minutes(day: str, start: str, end: str, price: float, volume: float) -> list[dict]:
    out = []
    for i, t in enumerate(pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", tz=ET, inclusive="left")):
        p = price + (i % 5) * 0.01
        out.append({"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.02, "l": p - 0.02, "c": p + 0.01,
                    "v": volume, "vw": p, "n": 10})
    return out


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        yield s


@pytest.fixture
def spcx(store):
    """Two days of SPCX minutes on Massive (thin outside regular hours, as in test_baddata)."""
    store.watch("massive", ["SPCX"], "minute")
    for day in ("2026-10-19", "2026-10-20"):
        store.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "04:00", "09:30", 50.0, 100))
        store.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "09:30", "16:00", 50.0, 10_000))
        store.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "16:00", "20:00", 50.0, 100))
    return store


def _spike(store, t: str = "2026-10-19 17:08") -> None:
    """An after-hours minute ranging $45 on its usual volume: a spike without volume (rule 2)."""
    row = store.bars_window(["massive"], "SPCX", "minute", _ms(t), _ms(t) + 60_000, filtered=False).iloc[0]
    store.upsert_bars("massive", "SPCX", "minute", 1, [{"t": _ms(t), "o": row["open"], "h": 75.0, "l": 30.0,
                                                        "c": row["close"], "v": 120, "vw": row["vwap"], "n": 10}])


def _five(store, t: str) -> tuple:
    return store._bars("massive").execute("SELECT high, low FROM built_bars WHERE ticker='SPCX' AND timespan='minute' "
                                          "AND multiplier=5 AND ts=?", (_ms(t),)).fetchone()


# -- 8: a second rebuild keeps the version the first one made ------------------------------------

def test_a_second_rebuild_keeps_the_version_the_first_made(store, monkeypatch):
    clock = iter(range(1_000, 10**9, 1_000))
    monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: next(clock))
    t = _ms("2026-10-01 17:08")
    did = store.record_derivation("bars-from-ticks", "massive:SPCX", "test", {}, {})

    def build(high: float) -> None:
        store.upsert_built_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 231.0, "h": high, "l": 230.0, "c": 231.0,
                                                                   "v": 10.0}], did)

    def rows():
        return store.built_rows("massive", "SPCX", t, t + 60_000, [("minute", 1)])

    build(251.77)  # the wrong rules
    first = rows()
    build(240.0)  # the first repair
    second = rows()
    assert store.prune_replaced("massive", "SPCX", first, second, "first repair") == 1
    build(231.15)  # a second repair: the version the first made is replaced in turn
    assert store.prune_replaced("massive", "SPCX", second, rows(), "second repair") == 1
    kept = store.pruned("massive", "SPCX", rule="rebuilt").sort_values("high")
    assert list(kept["high"]) == [240.0, 251.77] and kept["stored_at"].nunique() == 2
    assert [json.loads(e)["now"]["high"] for e in kept["evidence"]] == [231.15, 240.0]
    assert store.prune_replaced("massive", "SPCX", second, rows(), "again") == 0  # a version is listed once


def test_two_scans_at_once_list_a_bar_once(tmp_path):
    path = tmp_path / "m.db"
    with Store(path) as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 60_000, "o": 1, "h": 9, "l": 1, "c": 1, "v": 1}])
    bar = pd.DataFrame([{"ts": 60_000, "open": 1.0, "high": 9.0, "low": 1.0, "close": 1.0, "volume": 1.0}])
    start, added = threading.Barrier(6), []

    def scan():
        with Store(path, check_same_thread=False) as s:
            start.wait()
            added.append(s.prune("massive", "SPCX", "minute", 1, bar, "spike", True, code="test"))

    threads = [threading.Thread(target=scan) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    with Store(path) as s:
        assert sum(added) == 1 and len(s.pruned("massive", "SPCX")) == 1


# -- 26: a prune or a release is a change: the frames above and the caches see it ----------------

def test_a_prune_is_logged_in_the_change_log_and_a_rebuilt_version_is_not(store):
    t = _ms("2026-10-19 10:00")
    bar = pd.DataFrame([{"ts": t, "open": 1.0, "high": 9.0, "low": 1.0, "close": 1.0, "volume": 1.0}])
    store.upsert_bars("massive", "SPCX", "minute", 1, [{"t": t - 3_600_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    conn = store._bars("massive")
    logged = lambda: conn.execute("SELECT SUM(n), MAX(hi) FROM changes WHERE timespan='minute'").fetchone()  # noqa: E731
    store.prune("massive", "SPCX", "minute", 1, bar, "rebuilt", excludes=False, code="test")
    assert logged() == (1, t - 3_600_000)  # a version a rebuild replaced: the rebuild's own writes are logged
    store.prune("massive", "SPCX", "minute", 1, bar, "spike", excludes=True, code="test")
    assert logged() == (2, t)
    assert store.changed_span("massive", "SPCX", "minute", store._logged_since(conn))[1] == t  # derive rebuilds there


def test_a_release_rebuilds_the_frames_above_and_changes_the_fingerprint(spcx):
    store = spcx
    derive.run(store, log=lambda *_: None)
    _spike(store)
    derive.run(store, log=lambda *_: None)  # the scan prunes it, then the 5-minute bar is built without it
    assert _five(store, "2026-10-19 17:05")[0] < 51
    before = store.series_fingerprint("massive", "SPCX")
    assert store.release_pruned("massive", "SPCX", "minute", 1, _ms("2026-10-19 17:08")) == 1
    assert store.series_fingerprint("massive", "SPCX") != before  # every cache keyed on it sees the release
    derive.run(store, log=lambda *_: None)
    assert _five(store, "2026-10-19 17:05") == (75.0, 30.0)  # built again with the released bar
    assert len(store.pruned("massive", "SPCX", active=True)) == 0


def test_the_release_command_also_marks_the_frames_above_dirty(spcx, tmp_path, monkeypatch, capsys):
    _spike(spcx)
    baddata.scan_series(spcx, "massive", "SPCX", "minute", "stock")
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.chdir(tmp_path)
    t = _ms("2026-10-19 17:08")
    derive.take_dirty(spcx, "massive", "SPCX", "minute")  # what the scan marked: taken
    assert cli.main(["pruned", "release", "--feed", "massive", "--tickers", "SPCX", "--ts", str(t)]) == 0
    assert "1 entry released" in capsys.readouterr().out
    assert derive.take_dirty(spcx, "massive", "SPCX", "minute") == (t, t + 60_000)


# -- 27: a built bar fills the filtered view where the feed's own is pruned ----------------------

def _day(store, day: str, **bar) -> None:
    base = {"t": _ms(f"{day} 00:00"), "o": 50.0, "h": 50.06, "l": 49.98, "c": 50.03, "v": 1e6, "vw": 50.0, "n": 900}
    store.upsert_bars("massive", "SPCX", "day", 1, [{**base, **bar}])


def test_a_built_day_takes_the_place_of_a_pruned_feed_day_in_the_filtered_view_only(spcx):
    store = spcx
    store.add_coverage("massive", "SPCX", "minute", _ms("2026-10-19 00:00"), _ms("2026-10-21 00:00"))
    _day(store, "2026-10-19", h=78.95)  # a high no minute of the feed's own reached (NVDA 2024-06-10)
    _day(store, "2026-10-20")
    derive.run(store, log=lambda *_: None)  # the scan prunes the 19th; the days are built from the minutes
    lo, hi = _ms("2026-10-19 00:00"), _ms("2026-10-21 00:00")
    filtered = store.bars_window(["massive"], "SPCX", "day", lo, hi)
    every = store.bars_window(["massive"], "SPCX", "day", lo, hi, filtered=False)
    assert list(filtered["derived"]) == [1, 0] and filtered["high"].iloc[0] < 51  # built from its minutes
    assert list(every["derived"]) == [0, 0] and every["high"].iloc[0] == 78.95  # all data: the feed's own, as before
    days = store.load_bars(["SPCX"], "day", source="massive")
    assert list(days["derived"]) == [True, False] and days["high"].iloc[0] < 51
    assert store.load_bars(["SPCX"], "day", source="massive", filtered=False)["high"].iloc[0] == 78.95
    # where the feed's own day is fine, nothing is built beside it
    assert store._bars("massive").execute("SELECT COUNT(*) FROM built_bars WHERE timespan='day'").fetchone() == (1,)
    # released: the feed's own is back in the filtered view (it comes first)
    store.release_pruned("massive", "SPCX", "day", 1, lo)
    assert store.bars_window(["massive"], "SPCX", "day", lo, hi)["high"].iloc[0] == 78.95


def test_a_pruned_bar_built_here_stays_out_of_the_filtered_view(store):
    t = _ms("2026-10-19 10:00")
    did = store.record_derivation("bars-from-ticks", "massive:SPCX", "test", {}, {})
    store.upsert_built_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 1, "h": 9, "l": 1, "c": 1, "v": 1}], did)
    built = store.series_rows("massive", "SPCX", "minute", t, t + 60_000)
    store.prune("massive", "SPCX", "minute", 1, built, "malformed", True, code="test")
    assert store.bars_window(["massive"], "SPCX", "minute", t, t + 60_000).empty
    assert store.load_bars(["SPCX"], "minute", source="massive").empty
    assert len(store.bars_window(["massive"], "SPCX", "minute", t, t + 60_000, filtered=False)) == 1


# -- 28: the pruned list's symbols by seeks ------------------------------------------------------

def test_the_pruned_lists_symbols_come_from_a_loose_index_scan(store, monkeypatch, tmp_path, capsys):
    bar = pd.DataFrame([{"ts": 60_000, "open": 1.0, "high": 9.0, "low": 1.0, "close": 1.0, "volume": 1.0}])
    for ticker in ("NVDA", "AAPL", "SPCX"):
        store.prune("massive", ticker, "minute", 1, bar, "spike", True, code="test")
    store.prune("alpaca", "MSFT", "minute", 1, bar, "spike", True, code="test")
    assert store.pruned_tickers("massive") == ["AAPL", "NVDA", "SPCX"]
    assert store.pruned_tickers() == ["AAPL", "MSFT", "NVDA", "SPCX"]
    plan = store._bars("massive").execute("EXPLAIN QUERY PLAN SELECT ticker FROM pruned INDEXED BY pruned_by_time "
                                          "WHERE ticker > ? ORDER BY ticker LIMIT 1", ("A",)).fetchall()
    assert plan[0][-1] == "SEARCH pruned USING COVERING INDEX pruned_by_time (ticker>?)"  # a seek, then one entry
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.chdir(tmp_path)
    asked = []
    real = Store.pruned
    monkeypatch.setattr(Store, "pruned", lambda self, source=None, ticker=None, **kw:
                        asked.append(ticker) or real(self, source, ticker, **kw))
    assert cli.main(["pruned", "--feed", "massive"]) == 0
    out = capsys.readouterr().out
    assert "AAPL: massive 1 spike" in out and "SPCX:" in out and None not in asked  # never the whole list


# -- 29: status from seeks, counts on request ----------------------------------------------------

def test_the_summary_seeks_and_counts_only_when_asked(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-19", "09:30", "10:00", 50.0, 1))
    store.upsert_bars("massive", "AAPL", "minute", 1, _minutes("2026-10-20", "09:30", "09:40", 50.0, 1))
    seen: list[str] = []
    conn = store._bars("massive")
    conn.set_trace_callback(seen.append)
    s = store.summary()
    conn.set_trace_callback(None)
    assert not [q for q in seen if "COUNT(" in q or "GROUP BY" in q]
    assert s["bars"] == [("massive", "minute", 1, 2, None, _ms("2026-10-19 09:30"), _ms("2026-10-20 09:39"))]
    assert store.summary(counts=True)["bars"][0][4] == 40


def test_status_prints_without_counts_and_with_them(store, tmp_path, monkeypatch, capsys):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-19", "09:30", "10:00", 50.0, 1))
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if "tickers" in x)
    assert "1/minute" in line and "bars" not in line
    assert cli.main(["status", "--counts"]) == 0
    assert "30 bars" in capsys.readouterr().out


# -- 30: the coverage ledger keyed by origin, in one transaction ---------------------------------

def test_spans_of_two_origins_starting_together_are_both_kept(store):
    store.add_coverage("massive", "SPCX", "minute", 1_000, 5_000, origin="ticks")
    store.add_coverage("massive", "SPCX", "minute", 1_000, 9_000)  # replaced the ticks span before
    rows = store.conn.execute("SELECT origin, start_ms, end_ms FROM coverage ORDER BY origin").fetchall()
    assert rows == [("download", 1_000, 9_000), ("ticks", 1_000, 5_000)]
    store.add_coverage("massive", "SPCX", "minute", 9_000, 12_000)  # merged with its own origin's only
    assert sorted(store.coverage_detail("SPCX", "minute")) == [("massive", "download", 1_000, 12_000),
                                                                ("massive", "ticks", 1_000, 5_000)]
    store.drop_coverage("massive", "SPCX", before_ms=2_000)  # both start before: both trimmed (a key clash before)
    assert sorted(store.conn.execute("SELECT origin, start_ms FROM coverage")) == [("download", 2_000), ("ticks", 2_000)]


def test_an_older_ledger_is_rekeyed_with_its_spans_kept(tmp_path):
    path = tmp_path / "m.db"
    Store(path).close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE coverage")
        conn.execute("CREATE TABLE coverage (source TEXT NOT NULL, ticker TEXT NOT NULL, timespan TEXT NOT NULL, "
                     "start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, origin TEXT NOT NULL DEFAULT 'download', "
                     "PRIMARY KEY (source, ticker, timespan, start_ms))")
        conn.executemany("INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
                         [("massive", "SPCX", "minute", 1_000, 5_000, "download"),
                          ("massive", "SPCX", "minute", 7_000, 9_000, "none"),
                          ("alpaca", "SPCX", "day", 0, 86_400_000, "ticks")])
    with Store(path) as s:
        key = [r[1] for r in sorted(s.conn.execute("PRAGMA table_info(coverage)"), key=lambda r: r[5]) if r[5]]
        assert key == ["source", "ticker", "timespan", "origin", "start_ms"]
        assert sorted(s.conn.execute("SELECT * FROM coverage")) == [
            ("alpaca", "SPCX", "day", 0, 86_400_000, "ticks"), ("massive", "SPCX", "minute", 1_000, 5_000, "download"),
            ("massive", "SPCX", "minute", 7_000, 9_000, "none")]
        assert s.conn.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_series'").fetchone()
        s.add_coverage("massive", "SPCX", "minute", 1_000, 2_000, origin="ticks")
        assert len(s.coverage_detail("SPCX", "minute")) == 2
    with Store(path) as s:  # once only
        assert s.conn.execute("SELECT COUNT(*) FROM coverage").fetchone() == (4,)


def test_an_empty_head_becomes_none_in_one_transaction(store):
    day = 86_400_000
    store.upsert_bars("massive", "SPCX", "day", 1, [{"t": 30 * day, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    store.add_coverage("massive", "SPCX", "day", 0, 40 * day)
    commits: list[str] = []
    store.conn.set_trace_callback(lambda q: commits.append(q) if q.startswith(("BEGIN", "COMMIT")) else None)
    assert store.mark_empty_heads() == 1
    store.conn.set_trace_callback(None)
    assert commits.count("BEGIN IMMEDIATE") == 1 and commits.count("COMMIT") == 1
    assert sorted(store.conn.execute("SELECT origin, start_ms, end_ms FROM coverage")) == [
        ("download", 30 * day, 40 * day), ("none", 0, 30 * day)]


# -- 31: the API key out of reprs ----------------------------------------------------------------

def test_the_api_key_is_not_in_a_repr_of_the_settings():
    s = config.load_settings({"MASSIVE_API_KEY": "SECRET-KEY-123"}, env_file=None)
    assert s.api_key == "SECRET-KEY-123" and "SECRET-KEY-123" not in repr(s)


# -- Low: derivation records, migrations, revisions, exports, composite, settings ----------------

def test_a_derivation_record_is_widened_in_sql_and_its_rows_never_shrink(store):
    a = store.record_derivation("composite", "composite:X:minute", "c", {}, {}, (100, 200), rows=50)
    assert store.record_derivation("composite", "composite:X:minute", "c", {}, {}, (150, 300)) == a
    assert store.record_derivation("composite", "composite:X:minute", "c", {}, {}, (50, 120)) == a
    store.set_derivation_rows(a, 7)  # a later build's own few bars
    d = store.derivation(a)
    assert (d["span_lo"], d["span_hi"], d["rows"]) == (50, 300, 50)
    store.set_derivation_rows(a, 80)
    assert store.derivation(a)["rows"] == 80
    nothing = []
    store.conn.set_trace_callback(nothing.append)
    store.record_derivation("composite", "composite:X:minute", "c", {}, {}, (60, 290))  # inside: no write, no lock
    store.conn.set_trace_callback(None)
    assert not [q for q in nothing if q.startswith(("BEGIN", "UPDATE", "INSERT"))]


class _StaleSchema(sqlite3.Connection):
    """A connection that saw the table before another process added the column."""

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info"):
            return super().execute("SELECT 1 WHERE 0")
        return super().execute(sql, *args)


def test_a_column_another_process_added_meanwhile_is_no_error(tmp_path):
    with closing(sqlite3.connect(tmp_path / "x.db", factory=_StaleSchema)) as conn:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        add_column(conn, "t", "b", "TEXT")  # "duplicate column": already there
        add_column(conn, "t", "c", "TEXT")
        assert [r[1] for r in sqlite3.Connection.execute(conn, "PRAGMA table_info(t)")] == ["a", "b", "c"]


def test_a_bar_written_twice_in_one_batch_keeps_the_version_readers_saw(store, monkeypatch):
    t, later = 60_000, 4 * 10**12
    bar = lambda c: {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1}  # noqa: E731
    store.upsert_bars("massive", "SPCX", "minute", 1, [bar(10.0)])
    monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: later)
    store.upsert_bars("massive", "SPCX", "minute", 1, [bar(11.0), bar(12.0)])  # one page with the bar twice: it failed
    revs = store.bar_revisions("massive", "SPCX")
    assert list(revs["close"]) == [10.0] and list(revs["replaced_at"]) == [later]
    assert store.bars_window(["massive"], "SPCX", "minute", 0, 10**6, as_of=later - 1)["close"].tolist() == [10.0]
    assert store.bars_window(["massive"], "SPCX", "minute", 0, 10**6)["close"].tolist() == [12.0]


_OLD_TRIGGERS = {  # as files made before this change hold them
    "bars_revised": """CREATE TRIGGER bars_revised BEFORE UPDATE ON bars
BEGIN
    INSERT OR REPLACE INTO bar_revisions (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap,
        transactions, stored_at, replaced_at) VALUES (old.ticker, old.timespan, old.multiplier, old.ts, old.open,
        old.high, old.low, old.close, old.volume, old.vwap, old.transactions, old.stored_at, new.stored_at);
END""",
    "bars_replace_built": """CREATE TRIGGER bars_replace_built AFTER INSERT ON bars
BEGIN
    INSERT OR REPLACE INTO bar_revisions (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap,
        transactions, stored_at, replaced_at, derivation)
    SELECT ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions, stored_at,
        MAX(COALESCE(new.stored_at, 0), COALESCE(stored_at, 0)), derivation FROM built_bars
    WHERE ticker=new.ticker AND timespan=new.timespan AND multiplier=new.multiplier AND ts=new.ts;
    DELETE FROM built_bars WHERE ticker=new.ticker AND timespan=new.timespan AND multiplier=new.multiplier AND ts=new.ts;
END"""}


def test_an_older_feed_file_gets_the_new_revision_triggers(tmp_path, monkeypatch):
    with Store(tmp_path / "m.db") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
        conn = s._bars("massive")
        with conn:
            for name, sql in _OLD_TRIGGERS.items():
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(sql)
        clock = iter([5_000, 5_000])  # two writes in one millisecond
        monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: next(clock))
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 60_000, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}])
        with pytest.raises(sqlite3.IntegrityError):  # how the old trigger failed a whole write
            s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 60_000, "o": 3, "h": 3, "l": 3, "c": 3, "v": 1}])
    with Store(tmp_path / "m.db") as s:
        found = dict(s._bars("massive").execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                                                "AND name IN ('bars_revised', 'bars_replace_built')"))
        assert len(found) == 2 and all("NOT EXISTS" in sql and "OR REPLACE" not in sql for sql in found.values())
        monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: 5_000)
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 60_000, "o": 3, "h": 3, "l": 3, "c": 3, "v": 1}])
        assert s.bar_revisions("massive", "SPCX")["close"].tolist() == [1.0]  # the version before, kept


def test_an_export_takes_whole_days_in_the_series_day_zone(store, tmp_path, monkeypatch):
    winter = [_ms("2026-01-05 19:30"), _ms("2026-01-06 10:00"), _ms("2026-01-07 19:30"), _ms("2026-01-08 10:00")]
    store.upsert_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for t in winter])
    kept = lambda df: [int(t.timestamp() * 1000) for t in df["time"]]  # noqa: E731
    days = (date(2026, 1, 6), date(2026, 1, 7))
    assert kept(store.load_bars(["SPCX"], "minute", 1, *days, source="massive")) == winter[:3]  # from 19:00 the evening before
    assert kept(store.load_bars(["SPCX"], "minute", 1, *days, source="massive", zone=derive.ET)) == winter[1:3]
    utc = [_ms("2026-01-06 00:30", "UTC"), _ms("2026-01-07 23:30", "UTC"), _ms("2026-01-08 00:30", "UTC")]
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, [{"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for t in utc])
    zone = derive.day_zone("crypto")
    assert kept(store.load_bars(["BTCUSD"], "minute", 1, *days, source="coinbase", zone=zone)) == utc[:2]
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--source", "massive", "export", "SPCX", "--kind", "minute", "--from", "2026-01-06",
                     "--to", "2026-01-07", "--out", str(tmp_path / "x.csv")]) == 0
    assert len(pd.read_csv(tmp_path / "x.csv")) == 2


def test_a_composite_bar_no_longer_made_is_removed_from_the_rebuilt_span(store):
    from datetime import datetime, timezone

    from massive_collate.coverage import FeedProfile
    from massive_collate.config import TIERS
    from massive_collate.symbols import resolve

    t0 = int(datetime(2026, 10, 1, 15, tzinfo=timezone.utc).timestamp() * 1000)
    feeds = {n: FeedProfile(n, TIERS["free"], frozenset({"crypto"}), venues={"crypto": v})
             for n, v in (("coinbase", "coinbase"), ("alpaca", "kraken"))}
    for feed in feeds:
        store.watch_spec(feed, resolve("BTCUSD"))
        store.upsert_bars(feed, "BTCUSD", "minute", 1, [{"t": t0 + i * 60_000, "o": 1e4, "h": 1e4 + 1, "l": 1e4 - 1,
                                                          "c": 1e4, "v": 2.0} for i in range(30)])
    now = datetime.fromtimestamp((t0 + 3_600_000) / 1000, timezone.utc)
    composite.refresh(store, feeds, now, log=lambda m: None)
    assert len(store.composite_stats("BTCUSD", "minute")) == 30
    with store._bars("alpaca") as conn:  # one exchange's last five minutes gone: one venue left there
        conn.execute("DELETE FROM bars WHERE ts >= ?", (t0 + 25 * 60_000,))
    composite.refresh(store, feeds, now, log=lambda m: None)
    assert len(store.composite_stats("BTCUSD", "minute")) == 25
    assert len(store.load_bars(["BTCUSD"], "minute", source="composite")) == 25


@pytest.mark.parametrize("env, hours, tier", [
    ({"MASSIVE_REFRESH_HOURS": "0.5"}, 0.5, "free"),
    ({"MASSIVE_TIER": ""}, 0.0, "free"),
    ({"MASSIVE_TIER": "   "}, 0.0, "free"),
])
def test_settings_parse_fractional_hours_and_an_empty_tier(env, hours, tier):
    s = config.load_settings(env, env_file=None)
    assert s.refresh_hours == hours and s.tier.name == tier


def test_a_settings_file_drops_comments_after_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MASSIVE_TIER=starter   # my plan\nMASSIVE_RPM=30\t# set by hand\nGOV_CONTACT=\"Me #1 <me@x>\" "
                   "# quoted: kept whole\nKEY=abc#def\n", encoding="utf-8")
    values = config.read_env_file(env)
    assert values == {"MASSIVE_TIER": "starter", "MASSIVE_RPM": "30", "GOV_CONTACT": "Me #1 <me@x>", "KEY": "abc#def"}
    with pytest.raises(config.ConfigError):
        config.load_settings({"MASSIVE_REFRESH_HOURS": "-1"}, env_file=None)


# -- 9: compaction: in place only where it is safe, otherwise by copying -------------------------

def _filled(path) -> None:
    """A store whose feed file is half free pages (most of its bars deleted)."""
    with Store(path) as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": i * 60_000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 9.0}
                                                        for i in range(20_000)])
        with s._bars("massive") as conn:
            conn.execute("DELETE FROM bars WHERE ts >= ?", (2_000 * 60_000,))
            conn.execute("DELETE FROM changes")


def test_vacuum_checks_the_room_and_cuts_the_log_back(tmp_path, monkeypatch):
    _filled(tmp_path / "m.db")
    feed = tmp_path / "feeds" / "massive.db"
    with Store(tmp_path / "m.db") as s:
        monkeypatch.setattr(admin_mod, "_free", lambda folder: 0)
        lines = []
        assert s.vacuum(log=lines.append) == []  # no room: nothing touched
        assert any("needs" in x and "free" in x for x in lines)
        monkeypatch.setattr(admin_mod, "_free", lambda folder: 10**15)
        monkeypatch.setattr(admin_mod, "VACUUM_IN_PLACE_MAX", feed.stat().st_size - 1)
        done = s.vacuum(log=lines.append)
        assert str(feed) not in done and any("admin compact" in x for x in lines)  # too large to hold writers off
        monkeypatch.setattr(admin_mod, "VACUUM_IN_PLACE_MAX", 2**40)
        size = feed.stat().st_size
        assert str(feed) in s.vacuum(log=lines.append)
        assert feed.stat().st_size < size / 2
        wal = feed.with_name("massive.db-wal")
        assert not wal.exists() or wal.stat().st_size == 0  # checkpointed and cut back


def test_compact_copies_beside_the_file_and_swaps_only_when_asked(tmp_path):
    _filled(tmp_path / "m.db")
    feed = tmp_path / "feeds" / "massive.db"
    size, lines = feed.stat().st_size, []
    copy = admin_mod.compact_file(feed, log=lines.append)
    assert copy == tmp_path / "feeds" / "massive.compact.db" and copy.stat().st_size < size / 2
    assert feed.stat().st_size == size  # the file itself untouched
    with Store(tmp_path / "m.db") as s:
        assert s.feed_names() == ["massive"]  # the copy is no feed
    assert admin_mod.compact_file(feed, swap=True, log=lines.append) is None  # a copy is there: the user's call
    assert "remove it first" in lines[-1]
    copy.unlink()
    assert admin_mod.compact_file(feed, swap=True, log=lines.append) == feed
    kept = [f.name for f in feed.parent.iterdir() if ".pre-compact-" in f.name]
    assert len(kept) == 1 and (feed.parent / kept[0]).stat().st_size == size  # the old file kept, never deleted
    assert feed.stat().st_size < size / 2 and not copy.exists()
    with Store(tmp_path / "m.db") as s:
        assert s.feed_names() == ["massive"] and s.count_bars("massive", "SPCX", "minute") == 2_000
        assert s.integrity_check() == "ok"


def test_compact_does_not_swap_a_file_in_use_or_written_during_the_copy(tmp_path, monkeypatch):
    _filled(tmp_path / "m.db")
    feed = tmp_path / "feeds" / "massive.db"
    size, lines = feed.stat().st_size, []
    with closing(sqlite3.connect(feed)) as other:  # another process holding it open
        other.execute("SELECT COUNT(*) FROM bars").fetchone()
        assert admin_mod.compact_file(feed, swap=True, log=lines.append) is None
    assert "open in another process" in lines[-1] and feed.stat().st_size == size
    assert not feed.with_name("massive.compact.db").exists()
    monkeypatch.setattr(admin_mod, "_sharing_violation", lambda path: False)  # where it cannot be told before
    with closing(sqlite3.connect(feed)) as other:  # ... it is after the copy: the other connection's log files
        other.execute("SELECT COUNT(*) FROM bars").fetchone()
        assert admin_mod.compact_file(feed, swap=True, log=lines.append) is None
    assert "open in another process" in lines[-1] and "the copy removed" in lines[-1]
    assert feed.stat().st_size == size and not feed.with_name("massive.compact.db").exists()
    real = admin_mod._vacuum_into

    def copy_while_written(conn, copy):
        real(conn, copy)
        with closing(sqlite3.connect(feed)) as writer, writer:
            writer.execute("DELETE FROM bars WHERE ts < 60000")

    monkeypatch.setattr(admin_mod, "_vacuum_into", copy_while_written)
    assert admin_mod.compact_file(feed, swap=True, log=lines.append) is None
    assert "written to during the copy" in lines[-1] and not feed.with_name("massive.compact.db").exists()
    assert not [f for f in feed.parent.iterdir() if ".pre-compact-" in f.name]
    monkeypatch.setattr(admin_mod, "_free", lambda folder: 0)
    assert admin_mod.compact_file(feed, log=lines.append) is None and "needs" in lines[-1]


def test_admin_compact_from_the_command_line(tmp_path, monkeypatch, capsys):
    _filled(tmp_path / "m.db")
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.chdir(tmp_path)
    feed = tmp_path / "feeds" / "massive.db"
    assert cli.main(["admin", "compact", "--file", str(feed), "--swap"]) == 0
    assert "compacted 1 of 1 files" in capsys.readouterr().out
    assert [f.name for f in feed.parent.iterdir() if ".pre-compact-" in f.name]
    with pytest.raises(SystemExit):
        cli.main(["admin", "compact", "--file", str(tmp_path / "elsewhere.db")])


def test_a_compactions_copy_is_no_dataset_provider(tmp_path):
    folder = tmp_path / "datasets"
    folder.mkdir()
    for name in ("french.db", "french.compact.db", "french.pre-compact-20261026-120000.db"):
        sqlite3.connect(folder / name).close()
    with Store(tmp_path / "m.db") as s:
        assert s.dataset_providers() == ["french"]
        assert [f.name for f in s.data_files() if f.parent == folder] == ["french.db"]
        assert os.path.basename(s.vacuum(log=lambda *_: None)[-1]) == "french.db"
