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
    # off the exchanges listed here (to OTC, a delisting) it is not followed, whoever it is
    store.watch("fake", ["OTCX"], "minute", exchange="XNYS")
    store.set_listing("fake", "OTCX", None, "BBG-OTCX", None, "Otc Co")
    otc_market = Exchange("fake", -1, "OTC", country("US"), mic="OTCM")
    otc = Stock("OTCX", name="Otc Co", figi="BBG-OTCX", primary_exchange=otc_market)
    assert not collector.check_listing(FakeSource(listing=otc), store, store.watchlist("fake", "OTCX")[0],
                                       NOW.date(), log=_log).ok
    assert store.watchlist("fake", "OTCX")[0]["exchange"] == "XNYS"


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


# -- the fetch check in chunks (finding 6) -----------------------------------------------------------

def test_the_fetch_check_reads_a_chunk_at_a_time_and_only_where_the_finer_frame_is_held(store, monkeypatch):
    from massive_collate import baddata
    from massive_collate.coverage import FeedProfile

    first, last = datetime(2025, 10, 1, 10, tzinfo=ET), datetime(2026, 10, 20, 10, tzinfo=ET)
    store.upsert_bars("fake", "AAA", "minute", 1, [_bar(first), _bar(last)])  # a year of minutes changed
    store.upsert_bars("fake", "AAA", "day", 1, [_bar(datetime(2025, 10, 1, tzinfo=ET)), _bar(datetime(2026, 10, 20, tzinfo=ET))])
    store.add_coverage("fake", "AAA", "minute", ms(first), ms(last) + 60_000)
    seconds_from = ms(datetime(2026, 10, 19, tzinfo=ET))  # seconds held for the last two days only
    store.add_coverage("fake", "AAA", "second", seconds_from, ms(datetime(2026, 10, 21, tzinfo=ET)))
    judged, finer = [], []  # the bars checked, and the finer bars they are checked against
    rows, window = store.series_rows, store.bars_window
    monkeypatch.setattr(store, "series_rows", lambda s, t, ts, lo, hi: judged.append((ts, lo, hi)) or rows(s, t, ts, lo, hi))
    monkeypatch.setattr(store, "bars_window",
                        lambda s, t, ts, lo, hi, **kw: finer.append((ts, lo, hi)) or window(s, t, ts, lo, hi, **kw))
    me = FeedProfile("fake", FREE, frozenset({"stock"}), 0)
    out = collector.verify_fetched(FakeSource(), store, "AAA", "stock", "AAA", False, 0, me, {"fake": me}, _log)
    assert out == {"failed": 0, "asked": 0, "corrected": 0}
    chunk = {"day": baddata.CHUNK_MS["minute"], "minute": baddata.CHUNK_MS["second"]}  # the finer frame's
    assert {ts for ts, _, _ in judged} == {"day", "minute"}
    assert all(hi - lo <= chunk[ts] for ts, lo, hi in judged)  # a chunk at a time, not the year
    assert all(lo >= seconds_from for ts, lo, _ in judged if ts == "minute")  # none where no seconds are held
    assert finer and all(hi - lo <= chunk["day"] + 86_400_000 for _, lo, hi in finer)


# -- the audit queue (finding 17) -----------------------------------------------------------------------

def test_the_audit_queue_keeps_one_entry_per_series_and_expires_old_ones_at_once(store):
    from massive_collate import audit
    from massive_collate.coverage import FeedProfile

    for run in range(30):  # a month of runs, each queueing what it fetched
        store.queue_audit("AAA", "minute", 1000 * run, 1000 * run + 500, "fake")
    store.queue_audit("AAA", "day", 0, 10, "fake")
    store.queue_audit("AAA", "minute", 0, 10, "other")
    assert store.audit_queue_count() == 3
    assert [q["start_ms"] for q in store.audit_queue() if (q["timespan"], q["owner"]) == ("minute", "fake")] == [29_000]
    assert {q["owner"] for q in store.audit_queue(exclude_owner="fake")} == {"other"}
    with store.conn:
        store.conn.execute("UPDATE audit_queue SET queued_at='2020-01-01T00:00:00+00:00' WHERE owner='other'")
    me = FeedProfile("fake", FREE, frozenset({"stock"}), 0)
    # expired before any audit, whether or not this run audits anything (it was only while it did)
    audit.run_audits(FakeSource(), store, me, {"fake": me}, datetime.now(ET), queued=0, random_count=0, log=_log)
    assert store.audit_queue_count() == 2


# -- the collector's calendar takes the official holidays (finding 18) -------------------------------

def test_an_unscheduled_closure_from_the_official_calendar_is_not_asked_for(store):
    from massive_collate import freshness
    from massive_collate.sessions import Overrides

    store.watch("fake", ["AAA"], "minute")
    store.set_backfilled("fake", "AAA", date(2003, 9, 10))
    store.set_listing("fake", "AAA", None, None, NOW.date(), "AAA Inc", "stock")
    store.set_details("fake", "AAA", {"name": "AAA Inc"})
    start = ms(datetime(2003, 9, 10, tzinfo=ET))
    store.add_coverage("fake", "AAA", "minute", start, ms(datetime(2026, 10, 19, 20, 0, tzinfo=ET)))  # through Monday
    store.add_coverage("fake", "AAA", "day", start, ms(datetime(2026, 10, 20, tzinfo=ET)))
    store.set_synced("fake", "AAA", datetime(2026, 10, 20, 6, 0, tzinfo=ET))  # once Monday was published
    # Tuesday closed (a day of mourning): the official upcoming holidays say so, the rules cannot
    store.save_market_holidays("massive", [{"date": "2026-10-20", "exchange": "NYSE", "status": "closed"}])
    try:
        src = FakeSource(entitled=("minute",), bars={"minute": [], "day": []})
        sync(src, store, FREE, NOW, log=_log)
        assert not freshness.CALENDAR.is_trading_day(date(2026, 10, 20))
        assert [c for c in getattr(src, "bar_calls", []) if c[0] == "AAA"] == []  # nothing new: Tuesday was closed
    finally:
        freshness.CALENDAR.overrides = Overrides()
        freshness.expected.cache_clear()


# -- the directory's allowance (finding 16) ------------------------------------------------------------

def test_the_directorys_allowance_is_reckoned_after_the_watchlist_has_spent(store, monkeypatch):
    left_today = 20  # the day's cap less the reserve, before this run
    store.watch("fake", ["AAA"], "minute")
    src = FakeSource(entitled=("minute",), bars={"minute": [_bar(datetime(2026, 10, 20, 10, tzinfo=ET))], "day": []})
    given = []
    monkeypatch.setattr(collector, "refresh_directory", lambda *a, **k: 0)
    monkeypatch.setattr(collector, "fill_directory_icons",
                        lambda source, store, allowance, today, log: given.append(allowance) or 0)
    sync(src, store, FREE, NOW, log=_log, directory_allowance=lambda: left_today - src.requests_made)
    assert src.requests_made > 0 and given == [left_today - src.requests_made]  # what the run left, not the reserve


# -- the lows ---------------------------------------------------------------------------------------

def test_a_live_futures_contract_is_up_to_date_on_a_monday_with_fridays_session(store):
    from massive_collate.symbols import resolve

    monday = datetime(2026, 10, 26, 12, 0, tzinfo=ET)
    day = lambda d, v: {"t": ms(datetime(d.year, d.month, d.day, tzinfo=ET)), "o": 1, "h": 1, "l": 1, "c": 1, "v": v}  # noqa: E731
    d1, d2, friday = date(2026, 9, 17), date(2026, 9, 18), date(2026, 10, 23)
    src = FakeSource(entitled=("minute",))
    src.bars = {("NQU6", "day"): [day(d1, 900), day(d2, 100)],
                ("NQZ6", "day"): [day(d1, 100), day(d2, 800), day(friday, 700)],
                ("NQZ6", "minute"): [_bar(datetime(2026, 10, 23, 10, tzinfo=ET))]}
    store.watch_spec("fake", resolve("NQ1!"))
    sync(src, store, FREE, monday, log=_log)
    src.bar_calls = []
    sync(src, store, FREE, monday + timedelta(hours=1), log=_log)
    assert [c for c in src.bar_calls if c[3] == "day"] == []  # Friday's is the session before Monday's


def test_one_symbols_refusal_leaves_the_class_entitlement_alone(store):
    from massive_collate.errors import NotEntitled

    class OneRefused(FakeSource):
        def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
            if ticker == "ODD" and timespan == "minute":
                self._spend()
                raise NotEntitled(403, "not this symbol on your plan")
            yield from super().bar_pages(ticker, start, end, timespan, multiplier, descending, asset)

    store.watch("fake", ["AAA", "ODD"], "auto")
    src = OneRefused(entitled=("second", "minute"), bars={"minute": [], "second": [], "day": []})
    sync(src, store, FREE, NOW, log=_log)
    assert {t for _, t, a in src.probed if a == "stock"} == {"AAPL"}  # the class asked with a symbol all plans cover
    assert store.watchlist("fake", "ODD")[0]["resolution"] == "auto"  # found again next run
    assert store.entitlement("fake", "stock", "second") is True  # not cleared for every stock by one refusal


def test_a_build_that_fails_keeps_the_dirty_mark(store, monkeypatch):
    import pytest

    from massive_collate import derive

    t0 = datetime(2026, 10, 20, 10, 0, tzinfo=ET)
    store.upsert_bars("fake", "AAA", "minute", 1, [_bar(t0 + timedelta(minutes=i)) for i in range(10)])
    derive.build_chain(store, "fake", "AAA", "minute")
    derive.mark_dirty(store, "fake", "AAA", "minute", ms(t0), ms(t0) + 600_000)  # a pruned minute there

    def fails(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(derive, "aggregate", fails)
    with pytest.raises(RuntimeError):
        derive.build_chain(store, "fake", "AAA", "minute")
    assert derive.take_dirty(store, "fake", "AAA", "minute") == (ms(t0), ms(t0) + 600_000)  # still to rebuild
    monkeypatch.undo()
    derive.build_chain(store, "fake", "AAA", "minute")
    assert derive.take_dirty(store, "fake", "AAA", "minute") is None


def test_a_watch_row_left_at_trades_reserves_no_window_of_ticks(store):
    import pytest

    from massive_collate.cli import build_parser
    from massive_collate.coverage import FeedProfile, plan

    feeds = {"fake": FeedProfile("fake", FREE, frozenset({"stock"}), 1),
             "other": FeedProfile("other", FREE, frozenset({"stock"}), 0)}
    store.watch("fake", ["AAA"], "minute")
    store.watch("other", ["AAA"], "trades")  # from before ticks were dropped for history
    lo = ms(datetime(2025, 10, 22, tzinfo=ET))
    store.add_coverage("other", "AAA", "minute", lo, ms(datetime(2026, 10, 21, tzinfo=ET)), origin="none")
    # the other feed was asked and has nothing there: this feed takes it (ticks for it never come)
    gaps = plan(store, feeds["fake"], feeds, "AAA", "stock", "minute", date(2025, 10, 22), NOW)
    assert gaps == [(lo, ms(datetime(2026, 10, 20, 20, 0, tzinfo=ET)))]
    with pytest.raises(SystemExit):  # and no symbol is watched at 'trades' any more
        build_parser().parse_args(["watch", "add", "AAA", "--resolution", "trades"])
