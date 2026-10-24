"""What was known when: bar versions, corrections, and reads as of a moment."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime

import numpy as np

from massive_collate import freshness
from massive_collate.config import ALPACA_TIERS, TIERS
from massive_collate.storage import Store
from massive_collate.storage import feeds as feeds_mod

ET = freshness.ET


def _bar(t, c):
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 10.0}


def _stored_at(monkeypatch, ms):
    monkeypatch.setattr(feeds_mod, "_stored_ms", lambda: ms)


def test_a_correction_keeps_the_replaced_version_and_the_same_values_change_nothing(store, monkeypatch):
    _stored_at(monkeypatch, 1_000)
    store.upsert_bars("f", "X", "minute", 1, [_bar(60_000, 10.0), _bar(120_000, 11.0)])
    _stored_at(monkeypatch, 2_000)
    store.upsert_bars("f", "X", "minute", 1, [_bar(60_000, 10.0), _bar(120_000, 11.5)])  # one corrected
    rev = store.bar_revisions("f", "X")
    assert rev[["ts", "close", "stored_at", "replaced_at"]].values.tolist() == [[120_000, 11.0, 1_000, 2_000]]
    conn = store._bars("f")
    assert conn.execute("SELECT stored_at FROM bars WHERE ts=60000").fetchone()[0] == 1_000  # same values: untouched
    assert conn.execute("SELECT stored_at FROM bars WHERE ts=120000").fetchone()[0] == 2_000


def test_a_series_as_it_stood_at_a_moment(store, monkeypatch):
    _stored_at(monkeypatch, 1_000)
    store.upsert_bars("f", "X", "minute", 1, [_bar(60_000, 10.0)])
    _stored_at(monkeypatch, 5_000)
    store.upsert_bars("f", "X", "minute", 1, [_bar(60_000, 10.5), _bar(120_000, 12.0)])
    now = store.bars_window(["f"], "X", "minute", 0, 10**6)
    assert now["close"].tolist() == [10.5, 12.0]
    then = store.bars_window(["f"], "X", "minute", 0, 10**6, as_of=3_000)
    assert then["close"].tolist() == [10.0, 12.0]  # the version in effect; 120000 not stored yet: earliest known
    published = lambda ts: ts + 60_000 + 100  # the bar's end plus a delay
    gated = store.bars_window(["f"], "X", "minute", 0, 10**6, as_of=150_000, published=published)
    assert gated["ts"].tolist() == [60_000]  # the 02:00 bar was not published yet


def test_bars_stored_before_tracking_get_the_column_not_a_rewrite(tmp_path):
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    with closing(sqlite3.connect(feeds / "old.db")) as c, c:
        c.execute("CREATE TABLE bars (ticker TEXT NOT NULL, timespan TEXT NOT NULL, multiplier INTEGER NOT NULL, "
                  "ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL, vwap REAL, "
                  "transactions INTEGER, PRIMARY KEY (ticker, timespan, multiplier, ts)) WITHOUT ROWID")
        c.execute("INSERT INTO bars VALUES ('X', 'minute', 1, 60000, 1, 1, 1, 1, 1, NULL, NULL)")
    with Store(tmp_path / "m.db") as s:
        assert s.bars_window(["old"], "X", "minute", 0, 10**6, as_of=0)["close"].tolist() == [1.0]  # NULL: before tracking
        s.upsert_bars("old", "X", "minute", 1, [_bar(60_000, 2.0)])
        assert s.bar_revisions("old", "X")["stored_at"].isna().all()  # the replaced version had no stored time


def test_the_composite_keeps_no_revisions(store):
    store.upsert_bars("composite", "BTCUSD", "minute", 1, [_bar(60_000, 1.0)])
    store.upsert_bars("composite", "BTCUSD", "minute", 1, [_bar(60_000, 2.0)])
    assert store.bar_revisions("composite", "BTCUSD").empty  # derived and rebuilt: not corrections


def _ms(*a):
    return int(datetime(*a, tzinfo=ET).timestamp() * 1000)


def test_publication_by_plan_rule():
    minute = np.array([_ms(2026, 10, 20, 10, 0)])
    assert freshness.published_at(minute, "minute", "stock", TIERS["free"])[0] == _ms(2026, 10, 21, 4, 0)  # overnight
    assert freshness.published_at(minute, "minute", "stock", ALPACA_TIERS["basic"])[0] == _ms(2026, 10, 20, 10, 17)  # 16 minutes after the bar closes
    utc = int(datetime(2026, 10, 20, 23, 59, tzinfo=freshness.timezone.utc).timestamp() * 1000)
    assert freshness.published_at(np.array([utc]), "minute", "crypto", TIERS["free"])[0] == utc + 60_000  # next UTC midnight
    day = np.array([_ms(2026, 11, 1)])  # the day the clocks change
    assert freshness.published_at(day, "day", "stock", TIERS["free"])[0] == _ms(2026, 11, 2, 4, 0)
