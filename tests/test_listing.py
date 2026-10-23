"""Only the instrument currently using a symbol is collected."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from massive_collate.collector import ET, sync
from massive_collate.config import TIERS
from massive_collate.models import Exchange, Stock, country, parse_symbol
from massive_collate.storage import Store

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]
NASDAQ = Exchange("fake", -1, "XNAS", country("US"), mic="XNAS")
LISTED = date(2026, 6, 12)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 10, tzinfo=ET).timestamp() * 1000)


def _bar(d: date) -> dict:
    return {"t": _ms(d), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}


def _spcx(**kw) -> Stock:
    base = dict(ticker="SPCX", name="Space Exploration Technologies Corp.", primary_exchange=NASDAQ,
                list_date=LISTED, figi="BBG000NQF3Z5")
    base.update(kw)
    return Stock(**base)


def _log(_):
    pass


@pytest.mark.parametrize("text, expected", [
    ("SPCX::NASDAQ", ("SPCX", "XNAS")),
    ("nasdaq:spcx", ("SPCX", "XNAS")),
    ("SPCX::XNAS", ("SPCX", "XNAS")),
    ("spcx", ("SPCX", None)),
])
def test_parse_symbol(text, expected):
    assert parse_symbol(text) == expected


def test_rows_from_before_listing_are_removed_and_never_fetched(store):
    store.watch("fake", ["SPCX"], "minute", exchange="XNAS")
    store.upsert_bars("fake", "SPCX", "minute", 1, [_bar(date(2025, 3, 3)), _bar(LISTED), _bar(date(2026, 9, 1))])
    store.set_backfilled("fake", "SPCX", date(2024, 10, 23))
    src = FakeSource(entitled=("minute",), listing=_spcx())
    sync(src, store, FREE, NOW, log=_log)
    days = {t.date() for t in store.load_bars(["SPCX"], "minute", source="fake")["time"]}
    assert min(days) == LISTED
    w = store.watchlist("fake")[0]
    assert w["listed_from"] == LISTED and w["figi"] == "BBG000NQF3Z5"
    assert w["backfilled_to"] == LISTED


def test_backfill_stops_at_listing_date(store):
    calls = []

    class Recording(FakeSource):
        def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
            if timespan != "day":
                calls.append(start)
            return super().bar_pages(ticker, start, end, timespan, multiplier, descending, asset)

    store.watch("fake", ["SPCX"], "minute")
    sync(Recording(entitled=("minute",), listing=_spcx()), store, FREE, NOW, log=_log)
    assert calls and min(calls).date() == LISTED
    assert store.watchlist("fake")[0]["backfilled_to"] == LISTED


def test_inactive_listing_is_skipped(store):
    store.watch("fake", ["OLD"], "minute")
    src = FakeSource(entitled=("minute",), listing=_spcx(ticker="OLD", active=False))
    sync(src, store, FREE, NOW, log=_log)
    assert store.bar_range("fake", "OLD", "minute") is None


def test_wrong_exchange_is_skipped(store):
    store.watch("fake", ["SPCX"], "minute", exchange="XNYS")
    src = FakeSource(entitled=("minute",), bars={"minute": [_bar(date(2026, 9, 1))]}, listing=_spcx())
    sync(src, store, FREE, NOW, log=_log)
    assert store.bar_range("fake", "SPCX", "minute") is None


def test_symbol_reassigned_clears_old_instrument(store):
    store.watch("fake", ["SPCX"], "minute")
    store.upsert_bars("fake", "SPCX", "minute", 1, [_bar(date(2026, 9, 1))])
    store.set_listing("fake", "SPCX", LISTED, "OLD-FIGI", NOW.date() - timedelta(days=30))
    new = _spcx(figi="NEW-FIGI", list_date=date(2026, 10, 1))
    sync(FakeSource(entitled=("minute",), listing=new), store, FREE, NOW, log=_log)
    assert store.watchlist("fake")[0]["figi"] == "NEW-FIGI"
    assert store.bar_range("fake", "SPCX", "minute") is None


def test_listing_checked_weekly_not_every_run(store):
    store.watch("fake", ["SPCX"], "minute")
    src = FakeSource(entitled=("minute",), listing=_spcx())
    sync(src, store, FREE, NOW, log=_log)
    sync(src, store, FREE, NOW + timedelta(days=1), log=_log)
    assert src.instrument_calls == 1
    sync(src, store, FREE, NOW + timedelta(days=8), log=_log)
    assert src.instrument_calls == 2


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE watchlist (source TEXT NOT NULL, ticker TEXT NOT NULL, resolution TEXT NOT NULL "
                "DEFAULT 'auto', backfilled_to TEXT, added_at TEXT NOT NULL, PRIMARY KEY (source, ticker))")
    con.execute("INSERT INTO watchlist VALUES ('massive', 'SPCX', 'minute', '2024-10-23', 'x')")
    con.commit()
    con.close()
    with Store(path) as s:
        w = s.watchlist()[0]
        assert w["ticker"] == "SPCX" and w["exchange"] is None and w["listed_from"] is None
