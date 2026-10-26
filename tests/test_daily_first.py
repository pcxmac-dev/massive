"""Daily history from each stock's listing date: the fast audit of what is held (audit.daily_history,
`audit --daily`), the daily-first sweep that brings every stock's dailies before the minute backfill
(collector.daily_first), and an archive's monthly places spent only on history it can fill."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from massive_collate import collector
from massive_collate.audit import daily_history
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS
from massive_collate.coverage import FeedProfile
from massive_collate.initializer import ORDER_KEY

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def _days(start: date, n: int = 3) -> list[dict]:
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        out.append({"t": _ms(d), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1000})
    return out


def test_the_audit_says_which_stocks_lack_daily_bars_from_their_listing(store):
    store.watch("fake", ["OLD", "NEW", "NONE", "ODD"], "minute")
    store.set_listing("fake", "OLD", date(1980, 12, 12), None, NOW.date())
    store.set_listing("fake", "NEW", date(2024, 3, 1), None, NOW.date())
    store.set_listing("fake", "NONE", date(2010, 1, 4), None, NOW.date())
    store.add_coverage("fake", "OLD", "day", _ms(date(2003, 9, 10)), _ms(NOW.date()))
    store.add_coverage("fake", "NEW", "day", _ms(date(2024, 3, 1)), _ms(NOW.date()))
    store.add_coverage("fake", "ODD", "day", _ms(date(2020, 1, 2)), _ms(NOW.date()))  # listing not checked
    store.kv_set(ORDER_KEY, json.dumps({"NONE": 0, "OLD": 1, "NEW": 2, "ODD": 3}))
    a = daily_history(store)
    assert a["stocks"] == 4 and a["complete"] == 1  # NEW: from its listing
    assert a["head"] == [["OLD", "1980-12-12", "2003-09-10"]]  # before the plan reaches: an archive's to fill
    assert a["none"] == ["NONE"] and a["unchecked"] == ["ODD"]


def test_stocks_without_daily_bars_get_them_first_most_traded_first(store, monkeypatch):
    monkeypatch.setattr(collector, "DAILY_FIRST_PER_RUN", 2)
    store.watch("fake", ["AAA", "BBB", "CCC", "HELD"], "minute")
    store.kv_set(ORDER_KEY, json.dumps({"CCC": 0, "BBB": 1, "AAA": 2, "HELD": 3}))
    store.upsert_bars("fake", "HELD", "day", 1, _days(date(2025, 1, 2)))
    store.add_coverage("fake", "HELD", "day", _ms(NOW.date() - timedelta(days=700)), _ms(NOW.date()))
    src = FakeSource(entitled=("minute",), bars={"day": _days(date(2025, 6, 2)), "minute": []})
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    calls = [(c[0], c[3]) for c in src.bar_calls]
    days = [t for t, ts in calls if ts == "day"]
    # the two most traded without dailies, before any minute request; HELD has its own already
    assert calls[:2] == [("CCC", "day"), ("BBB", "day")]
    assert "HELD" not in days[:2]
    assert any("daily bars first: 2 stocks (1 without any daily bars yet, next runs)" in m for m in logged)
    assert src.instrument_calls == 4  # each stock's listing checked once in the run, not again by the backfill


def test_an_archive_spends_its_months_places_only_on_history_it_can_fill(store, monkeypatch):
    class Archive(FakeSource):  # like Tiingo: a stock's daily history once, then closed
        archival = True
        has_key = True

        def covers(self, ticker):
            return True

        def serves(self, asset):
            return asset == "stock" and self.has_key

        def resolutions(self, asset):
            return ("day",)

    monkeypatch.setitem(collector.SYMBOLS_PER_MONTH, "fake", 5)
    store.watch("fake", ["OLD", "UNCHECKED", "COVERED"], "day")
    store.watch("other", ["COVERED"], "day")
    store.set_listing("fake", "OLD", date(2024, 1, 2), None, NOW.date())
    store.set_listing("fake", "COVERED", date(2024, 1, 2), None, NOW.date())
    # another feed holds COVERED from its listing: nothing is left for the archive
    store.upsert_bars("other", "COVERED", "day", 1, _days(date(2024, 1, 2)) + _days(NOW.date() - timedelta(days=2)))
    store.add_coverage("other", "COVERED", "day", _ms(date(2024, 1, 2)), _ms(NOW.date() + timedelta(days=1)))
    month = f"symbols:fake:{NOW.astimezone(timezone.utc):%Y-%m}"

    keyless = Archive(entitled=("day",), bars={"day": _days(date(2024, 6, 3))})
    keyless.has_key = False
    store.kv_set(month, json.dumps(["X1", "X2"]))  # places taken before any request could be sent
    logged = []
    sync(keyless, store, FREE, NOW, log=logged.append)
    assert keyless.requests_made == 0 and store.kv_json(month, []) == []  # given back, none taken without a key
    assert any("this month's symbol places were taken with nothing sent; given back" in m for m in logged)

    src = Archive(entitled=("day",), bars={"day": _days(date(2024, 6, 3))})
    feeds = {"other": FeedProfile("other", FREE, frozenset({"stock"}), 1),
             "fake": FeedProfile("fake", FREE, frozenset({"stock"}), 2)}
    logged = []
    sync(src, store, FREE, NOW, log=logged.append, feeds=feeds)
    assert store.kv_json(month, []) == ["OLD"]  # the one place: history it can fill
    assert {c[0] for c in src.bar_calls} == {"OLD"}
    rows = {w["ticker"]: w for w in store.watchlist("fake")}
    assert rows["COVERED"]["closed_at"] is not None and rows["UNCHECKED"]["closed_at"] is None
    assert any("1 stocks wait for a listing date from another feed" in m for m in logged)
