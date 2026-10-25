"""The change log (storage/feeds.py): whether a series changed, and what changed since a moment,
from a seek in a small table its triggers keep, never by reading the series. The chart's live
check counted a stock's 13 million bars and its trades every few seconds while the sync wrote,
which held 31 GB of the tick file in Windows' file cache."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from massive_collate.storage import Store
from massive_collate.storage import feeds as feeds_mod

MIN = 60_000


def _bar(t: int, c: float = 1.0) -> dict:
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 2.0, "vw": c, "n": 3}


@pytest.fixture
def clock(monkeypatch):
    """The time writers stamp (stored_at), set by the test: from an hour ago (a removal is logged
    at SQLite's own clock, the real time)."""
    now = {"ms": (int(time.time() * 1000) - 3_600_000) // MIN * MIN}
    monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: now["ms"])
    return now


def _reads_of_the_series(conn: sqlite3.Connection, call) -> list[str]:
    """The statements `call` ran on a feed's connection that read its bars or trades."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return [s for s in seen if any(f"FROM {t}" in s for t in ("bars", "built_bars", "ticks.trades"))]


def test_what_changed_since_a_moment_comes_from_the_log_and_matches_the_rows(clock):
    with Store(":memory:") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN), _bar(11 * MIN)])
        clock["ms"] += 5 * MIN
        mark = clock["ms"] - 1
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(3 * MIN), _bar(20 * MIN)])  # an old bar and a new one
        s.upsert_bars("massive", "OTHER", "minute", 1, [_bar(1 * MIN)])
        conn = s._bars("massive")
        assert _reads_of_the_series(conn, lambda: s.changed_span("massive", "SPCX", "minute", mark)) == []
        assert s.changed_span("massive", "SPCX", "minute", mark) == (3 * MIN, 20 * MIN)
        assert s.changed_span("massive", "SPCX", "day", mark) is None
        assert s.changed_span("massive", "SPCX", "minute", clock["ms"] + MIN) is None  # nothing since
        # the same values again leave the bar as stored: no change
        clock["ms"] += 5 * MIN
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(20 * MIN)])
        assert s.changed_span("massive", "SPCX", "minute", clock["ms"] - 1) is None


def test_corrections_and_removals_are_changes_too(clock):
    with Store(":memory:") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN), _bar(11 * MIN)])
        did = 1  # a derivation record's id (built bars only point to it)
        s.upsert_built_bars("massive", "SPCX", "second", 1, [_bar(40 * MIN), _bar(41 * MIN)], did)
        clock["ms"] += 10 * MIN
        mark = clock["ms"] - 1
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN, 2.0)])  # corrected in place
        assert s.changed_span("massive", "SPCX", "minute", mark) == (10 * MIN, 10 * MIN)
        assert s.changed_span("massive", "SPCX", "second", mark) is None
        s.drop_built_between("massive", "SPCX", ("second",), 41 * MIN, 42 * MIN)  # removed: no stored_at left
        assert s.changed_span("massive", "SPCX", "second", mark) == (41 * MIN, 41 * MIN)


def test_the_fingerprint_changes_with_every_write_and_reads_no_series(clock):
    with Store(":memory:") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN)])
        conn = s._bars("massive")
        before = s.series_fingerprint("massive", "SPCX")
        assert _reads_of_the_series(conn, lambda: s._fingerprint("massive", "SPCX")) == []
        assert s.series_fingerprint("massive", "SPCX") == before  # nothing written: the same
        s.upsert_bars("massive", "OTHER", "minute", 1, [_bar(10 * MIN)])
        assert s.series_fingerprint("massive", "SPCX") == before  # another symbol's write
        seen = {before}
        for write in (lambda: s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN, 3.0)]),  # corrected
                      lambda: s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(11 * MIN)]),  # added
                      lambda: s.delete_before("massive", "SPCX", 11 * MIN * 1_000_000)):  # removed
            write()
            fp = s.series_fingerprint("massive", "SPCX")
            assert fp not in seen
            seen.add(fp)


def test_a_reader_further_back_than_the_log_reads_the_rows_once(clock):
    with Store(":memory:") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(10 * MIN)])
        clock["ms"] += 10 * 86_400_000
        assert s.prune_changes(now_ms=clock["ms"]) == 1  # older than CHANGES_KEEP_MS
        since = s._logged_since(s._bars("massive"))
        assert since > clock["ms"] - 10 * 86_400_000
        assert s.changed_span("massive", "SPCX", "minute", 0) == (10 * MIN, 10 * MIN)  # from stored_at
        assert s.bars_changed_since("massive", "SPCX", ["minute"], 0)["minute"][0] == 10 * MIN
        assert s.prune_changes(now_ms=clock["ms"] + 60_000) == 0  # at most hourly
        assert s._logged_since(s._bars("massive")) == since


def test_every_writer_keeps_the_log_whatever_its_code(tmp_path, clock):
    with Store(tmp_path / "m.db") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [_bar(MIN)])
        mark = clock["ms"] + MIN  # the next minute (one minute's writes are named together)
    # another process on older code writes a bar directly: the file's triggers log it
    with closing(sqlite3.connect(tmp_path / "feeds" / "massive.db")) as c, c:
        c.execute("INSERT INTO bars (ticker, timespan, multiplier, ts, close, stored_at) VALUES (?, ?, 1, ?, 1, ?)",
                  ("SPCX", "minute", 7 * MIN, clock["ms"] + 2 * MIN))
    with Store(tmp_path / "m.db") as s:
        assert s.changed_span("massive", "SPCX", "minute", mark) == (7 * MIN, 7 * MIN)


def test_bars_built_count_the_bars_not_the_log_entries(clock):
    with Store(":memory:") as s:
        did = 1  # a derivation record's id (built bars only point to it)
        assert s.upsert_built_bars("massive", "SPCX", "second", 1, [_bar(MIN), _bar(2 * MIN)], did) == 2
