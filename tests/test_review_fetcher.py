"""The fetching core's fixes from the code review (CODE_REVIEW.md): retired symbols, exchange
transfers, reused symbols on every feed, the ledger's empty heads and settling frontier, the fetch
check in chunks, the audit queue, the official calendar, the directory's allowance, and the lows."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from massive_collate import collector
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS
from massive_collate.coverage import ms
from massive_collate.models import Exchange, Stock, country

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)  # a Wednesday
FREE, ADV = TIERS["free"], TIERS["advanced"]
NASDAQ = Exchange("fake", -1, "XNAS", country("US"), mic="XNAS")


class Other(FakeSource):
    name = "other"


def _log(_):
    pass


def _bar(t: datetime, price: float = 10.0) -> dict:
    return {"t": ms(t), "o": price, "h": price + 0.1, "l": price - 0.1, "c": price, "v": 100}


# -- retired symbols (findings 4 and 15) ------------------------------------------------------------

def test_a_retired_symbols_last_gap_is_recorded_and_not_asked_again(store):
    # Delisted 15 September, its last bars a few days before: the gap after them used to count as the
    # feed's frontier, held only as far as published, and was asked again on every run.
    gone = Stock("OLD", list_date=date(2020, 1, 2), figi="BBG-OLD", active=False,
                 details={"delisted_utc": "2026-09-15T00:00:00Z"})
    minutes = [_bar(datetime(2026, 9, 10, 10, i, tzinfo=ET)) for i in range(3)]
    src = FakeSource(entitled=("minute",), bars={"minute": minutes, "day": [_bar(datetime(2026, 9, 10, tzinfo=ET))]},
                     listing=gone)
    store.watch("fake", ["OLD"], "minute")
    sync(src, store, FREE, NOW, log=_log)
    assert store.watchlist("fake", "OLD")[0]["retired_on"] == date(2026, 9, 15)
    end = ms(datetime(2026, 9, 16, tzinfo=ET))
    for timespan in ("minute", "day"):
        spans = store.coverage("OLD", timespan, include_none=True)["fake"]
        assert max(b for _, b in spans) == end  # held (or 'none') to the end of its last listed day
    asked = len(src.bar_calls)
    for day in (1, 2, 3):
        sync(src, store, FREE, NOW + timedelta(days=day), log=_log)
    assert len(src.bar_calls) == asked  # nothing asked again
    assert collector.needs_fetch(store, "OLD", {"fake": FREE}, NOW + timedelta(days=3)) == []


def test_only_a_record_that_says_who_the_instrument_is_retires_a_symbol_and_a_listed_one_clears_it(store):
    for feed in ("fake", "other"):
        store.watch(feed, ["AAA"], "minute")
    # Alpaca-like: its own status, no listing date, FIGI or delisting date: not a retirement
    coarse = Other(listing=Stock("AAA", active=False, details={"status": "inactive"}))
    collector.check_listing(coarse, store, store.watchlist("other", "AAA")[0], NOW.date(), log=_log)
    assert {w["retired_on"] for w in store.watchlist(ticker="AAA")} == {None}
    store.set_retired("AAA", date(2026, 10, 1))  # as the old rule marked it, on every feed
    listed = FakeSource(listing=Stock("AAA", list_date=date(2020, 1, 2), figi="BBG-AAA", primary_exchange=NASDAQ))
    logged = []
    collector.check_listing(listed, store, store.watchlist("fake", "AAA")[0], NOW.date(), log=logged.append)
    assert {w["retired_on"] for w in store.watchlist(ticker="AAA")} == {None}
    assert any("retired mark (2026-10-01) cleared" in m for m in logged)
    # a delisting date retires it, whoever reports it
    for feed in ("fake", "other"):
        store.watch(feed, ["BBB"], "minute")
    dated = Other(listing=Stock("BBB", active=False, details={"delisted_utc": "2026-10-20T00:00:00Z"}))
    collector.check_listing(dated, store, store.watchlist("other", "BBB")[0], NOW.date(), log=_log)
    assert {w["retired_on"] for w in store.watchlist(ticker="BBB")} == {date(2026, 10, 20)}
