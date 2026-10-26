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


# -- exchange transfers and reused symbols (findings 5 and 19) ---------------------------------------

def _directory(store, ticker: str, exchange: str, figi: str | None, name: str) -> None:
    """The directory of listings, where a bulk-watched stock's exchange pin came from."""
    store.update_directory("fake", "stock", [{"ticker": ticker, "name": name, "exchange": exchange, "type": "CS",
                                              "figi": figi}], NOW.date() - timedelta(days=3))


def test_a_stock_that_moved_exchange_is_followed_when_it_is_the_same_instrument(store):
    for feed in ("fake", "other"):  # pinned by the initializer from the directory, never checked
        store.watch(feed, ["BCPC"], "minute", exchange="XNYS")
    _directory(store, "BCPC", "XNYS", "BBG-BCPC", "Balchem Corp")
    moved = Stock("BCPC", name="Balchem Corp", primary_exchange=NASDAQ, list_date=date(2020, 1, 2), figi="BBG-BCPC")
    src = FakeSource(entitled=("minute",), bars={"minute": [_bar(datetime(2026, 10, 20, 10, tzinfo=ET))], "day": []},
                     listing=moved)
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    assert {w["exchange"] for w in store.watchlist(ticker="BCPC")} == {"XNAS"}  # every feed's pin
    assert any("BCPC: moved from XNYS to XNAS (the same FIGI); followed there" in m for m in logged)
    assert store.bar_range("fake", "BCPC", "minute") is not None
    # without FIGIs to compare, the same name (its legal form aside) is the same instrument
    store.watch("fake", ["KO"], "minute", exchange="XNYS")
    store.set_listing("fake", "KO", None, None, None, "The Coca-Cola Company")
    renamed = Stock("KO", name="Coca-Cola Co Common Stock", primary_exchange=NASDAQ)
    assert collector.check_listing(FakeSource(listing=renamed), store, store.watchlist("fake", "KO")[0],
                                   NOW.date(), log=_log).ok


def test_a_stock_elsewhere_not_known_to_be_the_same_is_asked_weekly_and_holds_back_no_seconds(store):
    store.watch("fake", ["AAA"], "auto")
    store.watch("fake", ["ZZZ"], "auto", exchange="XNYS")  # no FIGI or name to compare: skipped
    listing = Stock("X", name="Some Co", primary_exchange=NASDAQ, list_date=date(2026, 1, 2), figi="BBG-X")
    src = FakeSource(entitled=("second", "minute"), bars={"minute": [], "second": [], "day": []}, listing=listing)
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    assert any("ZZZ: listed on XNAS, not XNYS as watched, and not known to be the same instrument" in m for m in logged)
    assert store.watchlist("fake", "ZZZ")[0]["checked_at"] == NOW.date()
    assert src.instrument_calls == 2 and not [c for c in src.bar_calls if c[0] == "ZZZ"]
    src.bar_calls = []
    sync(src, store, FREE, NOW + timedelta(days=1), log=_log)
    assert src.instrument_calls == 2  # not asked again within the week
    # ZZZ can never finish its minutes: AAA's 1-second window does not wait for it
    assert [c[0] for c in src.bar_calls if c[3] == "second"] == ["AAA"]
    sync(src, store, FREE, NOW + timedelta(days=8), log=_log)
    assert src.instrument_calls == 4  # a week on, both checked again


def test_a_reused_symbol_is_cleared_on_every_feed_watching_it(store):
    old = [_bar(datetime(2026, 9, 1, 10, tzinfo=ET))]
    for feed in ("fake", "other", "tiingo"):
        store.watch(feed, ["SPCX"], "minute")
        store.upsert_bars(feed, "SPCX", "minute", 1, old)
        store.add_coverage(feed, "SPCX", "minute", old[0]["t"], old[0]["t"] + 60_000)
        store.set_backfilled(feed, "SPCX", date(2024, 10, 22))
        store.set_listing(feed, "SPCX", date(2020, 1, 2), "OLD-FIGI", NOW.date() - timedelta(days=30))
    store.set_closed("tiingo", "SPCX", NOW)  # an archive's history complete
    new = Stock("SPCX", name="New Co", primary_exchange=NASDAQ, list_date=date(2026, 10, 1), figi="NEW-FIGI")
    collector.check_listing(FakeSource(listing=new), store, store.watchlist("fake", "SPCX")[0], NOW.date(), log=_log)
    rows = {w["source"]: w for w in store.watchlist(ticker="SPCX")}
    for feed in ("fake", "other", "tiingo"):
        assert store.bar_range(feed, "SPCX", "minute") is None and feed not in store.coverage("SPCX", "minute")
        assert rows[feed]["backfilled_to"] is None
        assert rows[feed]["listed_from"] == date(2026, 10, 1) and rows[feed]["figi"] == "NEW-FIGI"
    assert rows["tiingo"]["closed_at"] is None  # the new issuer's history to fill
    assert rows["other"]["checked_at"] is None  # checks again (sharing this answer)
    # rows from before the listing date go on every feed too
    store.upsert_bars("other", "SPCX", "minute", 1, old + [_bar(datetime(2026, 10, 2, 10, tzinfo=ET))])
    store.set_listing("fake", "SPCX", date(2026, 10, 1), "NEW-FIGI", None)
    collector.check_listing(FakeSource(listing=new), store, store.watchlist("fake", "SPCX")[0], NOW.date(), log=_log)
    assert store.bar_range("other", "SPCX", "minute")[0] >= ms(datetime(2026, 10, 1, tzinfo=ET))


# -- the ledger: empty heads and the settling frontier (findings 20 and 14) --------------------------

def test_a_first_fetch_whose_history_starts_later_leaves_the_head_to_another_feed(store):
    from massive_collate.coverage import FeedProfile, plan

    feeds = {"fake": FeedProfile("fake", FREE, frozenset({"stock"}), 0),
             "other": FeedProfile("other", FREE, frozenset({"stock"}), 1)}
    for feed in feeds:
        store.watch(feed, ["META"], "minute")
    first, newest = datetime(2025, 6, 2, 9, 30, tzinfo=ET), datetime(2026, 10, 20, 19, 59, tzinfo=ET)
    src = FakeSource(entitled=("minute",), bars={"minute": [_bar(first), _bar(newest)]})
    floor = date(2024, 10, 22)
    # one span from the floor to the newest published: the frontier, its head empty on this feed
    collector.sync_bars(src, store, "META", "minute", floor, NOW, me=feeds["fake"], feeds=feeds, log=_log)
    a = ms(datetime(2024, 10, 22, tzinfo=ET))
    assert store.coverage("META", "minute")["fake"] == [(ms(first), ms(newest) + 60_000)]
    assert (a, ms(first)) in store.coverage("META", "minute", include_none=True)["fake"]  # 'none': not held
    assert plan(store, feeds["other"], feeds, "META", "stock", "minute", floor, NOW) == [(a, ms(first))]


def test_a_real_time_plans_newest_minutes_are_left_to_settle(store):
    from massive_collate import freshness
    from massive_collate.coverage import FeedProfile, frontier_ms

    now = datetime(2026, 10, 21, 12, 0, 30, tzinfo=ET)
    assert frontier_ms("stock", ADV, now, "minute") == ms(datetime(2026, 10, 21, 11, 57, tzinfo=ET))
    assert frontier_ms("stock", TIERS["starter"], now, "minute") == ms(datetime(2026, 10, 21, 11, 45, tzinfo=ET))
    # a symbol opened in the chart: the minutes just ended are not recorded held, so they are asked again
    feeds = {"fake": FeedProfile("fake", ADV, frozenset({"stock"}), 0)}
    bars = [_bar(datetime(2026, 10, 21, 11, m, tzinfo=ET)) for m in range(50, 60)]
    src = FakeSource(entitled=("minute",), bars={"minute": bars})
    collector.sync_bars(src, store, "AAA", "minute", date(2026, 10, 20), now, me=feeds["fake"], feeds=feeds, log=_log)
    assert max(b for _, b in store.coverage("AAA", "minute")["fake"]) == ms(datetime(2026, 10, 21, 11, 57, tzinfo=ET))
    # a completed session counts once settled, and then whole
    just_after = datetime(2026, 10, 21, 20, 1, tzinfo=ET)
    assert freshness.history_horizon("stock", ADV, just_after) == datetime(2026, 10, 20, 20, 3, tzinfo=ET)
    horizon = freshness.history_horizon("stock", ADV, datetime(2026, 10, 21, 20, 5, tzinfo=ET))
    assert frontier_ms("stock", ADV, horizon, "minute") == ms(datetime(2026, 10, 21, 20, 0, tzinfo=ET))
