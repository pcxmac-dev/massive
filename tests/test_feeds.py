"""Several feeds: each span downloaded by one feed only (coverage ledger), ranking, shared
listing checks, and audits (the one deliberate second copy, compared and never stored)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from massive_collate import audit
from massive_collate.collector import ET, sync
from massive_collate.config import ALPACA_TIERS, TIERS
from massive_collate.coverage import FeedProfile, has_open_time, ms, plan, subtract
from massive_collate.models import Instrument
from massive_collate.symbols import resolve

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)  # a Wednesday, markets open
FREE, ALPACA = TIERS["free"], ALPACA_TIERS["basic"]
STOCKS = frozenset({"stock", "crypto"})


class Other(FakeSource):
    name = "other"


def _log(_):
    pass


def _minutes(day: date, start=(9, 30), count=3, price=100.0):
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=ET)
    return [{"t": ms(t0 + timedelta(minutes=i)), "o": price, "h": price + 0.1, "l": price - 0.1, "c": price,
             "v": 1000, "vw": price, "n": 10} for i in range(count)]


def _feeds():
    return {"fake": FeedProfile("fake", FREE, STOCKS, 0), "other": FeedProfile("other", ALPACA, STOCKS, 1)}


def test_subtract_and_open_time():
    assert subtract([(0, 100)], [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
    assert subtract([(0, 100)], [(-5, 200)]) == []
    sat = ms(datetime(2026, 10, 24, 10, tzinfo=ET))
    assert not has_open_time("stock", sat, sat + 86_400_000)  # a weekend needs no request
    assert has_open_time("crypto", sat, sat + 60_000)


def test_second_feed_downloads_only_what_nobody_holds_or_will_serve(store):
    """Massive-like feed (2 years, end of day) holds its span; the Alpaca-like feed (from 2016,
    15 minutes delayed) takes the older history and today's session, nothing in between."""
    store.watch_spec("fake", resolve("SPCX"))
    store.watch_spec("other", resolve("SPCX"))
    store.set_listing("fake", "SPCX", None, None, NOW.date(), "SpaceX", "stock")
    held = _minutes(date(2025, 6, 2)) + _minutes(date(2026, 10, 20), (19, 57))
    store.upsert_bars("fake", "SPCX", "minute", 1, held)
    store.set_backfilled("fake", "SPCX", date(2024, 10, 22))
    today = _minutes(NOW.date(), (9, 30), 5, 101.0)
    other = Other(entitled=("minute",), bars={"minute": today, "day": []}, listing=Instrument("SPCX"))
    sync(other, store, ALPACA, NOW, log=_log, feeds=_feeds())
    windows = [(s, e) for _, s, e, ts, _ in other.bar_calls if ts == "minute"]
    assert len(windows) == 2
    newest, oldest = windows
    assert newest == (datetime(2026, 10, 20, 20, 0, tzinfo=ET), datetime(2026, 10, 21, 11, 45, tzinfo=ET))
    assert oldest == (datetime(2016, 1, 1, tzinfo=ET), datetime(2024, 10, 22, tzinfo=ET))  # stops at the other's floor
    cov = store.coverage("SPCX", "minute")
    assert cov["other"][-1] == (ms(newest[0]), ms(newest[1]))  # today's session is in: held to the frontier
    # The first feed's next run (end of day published) finds today's session held: nothing to ask for.
    later = datetime(2026, 10, 22, 6, 0, tzinfo=ET)
    first = FakeSource(entitled=("minute",), bars={"minute": today, "day": []})
    gaps = plan(store, _feeds()["fake"], _feeds(), "SPCX", "stock", "minute", date(2024, 10, 22), later)
    assert all(not (a < ms(newest[1]) and b > ms(newest[0])) for a, b in gaps)
    sync(first, store, FREE, later, log=_log, feeds=_feeds())
    asked = [(s, e) for _, s, e, ts, _ in getattr(first, "bar_calls", []) if ts == "minute"]
    assert all(e <= newest[0] or s >= newest[1] for s, e in asked)


def test_unpublished_session_stays_open_for_the_next_run(store):
    store.watch_spec("other", resolve("SPCX"))
    store.set_listing("other", "SPCX", None, None, NOW.date(), "SpaceX", "stock")
    yesterday = _minutes(date(2026, 10, 20), (19, 58), 2)
    other = Other(entitled=("minute",), bars={"minute": yesterday, "day": []})
    sync(other, store, ALPACA, NOW, log=_log, feeds={"other": _feeds()["other"]})
    end = max(b for _, b in store.coverage("SPCX", "minute")["other"])
    assert end == ms(datetime(2026, 10, 20, 20, 0, tzinfo=ET))  # today's session not published: not held


def test_finer_resolution_outranks_priority(store):
    store.watch("fake", ["SPCX"], "minute")
    store.watch("other", ["SPCX"], "trades")
    feeds = _feeds()
    gaps = plan(store, feeds["fake"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW)
    assert gaps == []  # the tick feed can serve the whole span, so the minute feed takes none of it
    store.watch("other", ["SPCX"], "minute")
    # Same resolution: the fresher plan (15 min delayed) outranks the end-of-day one
    assert plan(store, feeds["fake"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW) == []
    assert plan(store, feeds["other"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW)


def test_with_resolution_and_freshness_equal_priority_decides(store):
    store.watch("fake", ["SPCX"], "minute")
    store.watch("other", ["SPCX"], "minute")
    feeds = {"fake": FeedProfile("fake", FREE, STOCKS, 0), "other": FeedProfile("other", FREE, STOCKS, 1)}
    assert plan(store, feeds["fake"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW)
    assert plan(store, feeds["other"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW) == []


def test_a_real_time_plan_with_ticks_takes_the_spans_from_a_delayed_one(store):
    """SPCX: Massive Advanced (ticks, real time) over Alpaca Basic (ticks, 15 minutes behind)."""
    store.watch("fake", ["SPCX"], "trades")
    store.watch("other", ["SPCX"], "trades")
    feeds = {"fake": FeedProfile("fake", TIERS["advanced"], STOCKS, 1), "other": FeedProfile("other", ALPACA, STOCKS, 0)}
    assert plan(store, feeds["other"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW) == []
    assert plan(store, feeds["fake"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW)


def test_accuracy_from_audits_outranks_priority(store):
    store.watch("fake", ["SPCX"], "minute")
    store.watch("other", ["SPCX"], "minute")
    third = FeedProfile("third", FREE, STOCKS, 2)
    feeds = {**_feeds(), "third": third}
    store.watch("third", ["SPCX"], "minute")
    for owner, auditor, verdict in [("fake", "third", "fail"), ("other", "third", "pass"), ("fake", "other", "fail")]:
        store.save_audit({"ticker": "SPCX", "timespan": "minute", "start_ms": 0, "end_ms": 1, "owner": owner,
                          "auditor": auditor, "verdict": verdict})
    # 'fake' failed both its audits: 'third' (same plan, lower priority) now ranks above it
    assert plan(store, feeds["fake"], feeds, "SPCX", "stock", "minute", date(2024, 10, 22), NOW) == []


def test_listing_check_is_shared_between_feeds(store):
    for feed in ("fake", "other"):
        store.watch_spec(feed, resolve("SPCX"))
    first = FakeSource(entitled=("minute",), listing=Instrument("SPCX", name="SpaceX", list_date=date(2026, 6, 12)))
    sync(first, store, FREE, NOW, log=_log, feeds=_feeds())
    second = Other(entitled=("minute",), listing=Instrument("SPCX"))
    sync(second, store, ALPACA, NOW, log=_log, feeds=_feeds())
    assert first.instrument_calls == 1 and second.instrument_calls == 0
    row = next(w for w in store.watchlist("other") if w["ticker"] == "SPCX")
    assert row["listed_from"] == date(2026, 6, 12)


def test_deps_flags_a_span_held_by_two_feeds(store):
    from massive_collate.deps import coverage_overlaps

    store.add_coverage("fake", "SPCX", "minute", 0, 100)
    store.add_coverage("other", "SPCX", "minute", 100, 200)
    assert coverage_overlaps(store) == []
    store.add_coverage("other", "SPCX", "minute", 50, 60)
    assert len(coverage_overlaps(store)) == 1


# -- audits -----------------------------------------------------------------------------------

def test_compare_within_margin_passes_and_reports_box_stats():
    owner = _minutes(NOW.date(), count=100)
    auditor = [dict(b, c=b["c"] * 1.0002, v=b["v"] * 1.01) for b in owner[:-1]]  # 2 bps, 1 %, one bar missing
    r = audit.compare(owner, auditor)
    assert r["verdict"] == "pass" and r["matched"] == 99 and r["onlyOwner"] == 1
    assert r["box"]["c"]["median"] == pytest.approx(2.0, abs=1e-6)
    assert r["box"]["v"]["median"] == pytest.approx(1.0, abs=1e-6)
    assert r["box"]["range"]["max"] == pytest.approx(0.0, abs=1e-9)
    assert r["timing"]["bestShiftSeconds"] == 0 and r["timing"]["stampsOffTheGrid"] == 0


def test_compare_fails_on_large_errors_and_shifted_stamps():
    owner = _minutes(NOW.date(), count=50)
    wrong = [dict(b, h=b["h"] * 1.01) for b in owner]  # highs 100 bps off
    assert audit.compare(owner, wrong)["verdict"] == "fail"
    rising = [dict(b, c=100 + i) for i, b in enumerate(owner)]
    shifted = [dict(b, t=b["t"] - 60_000) for b in rising]  # same bars stamped one minute earlier
    r = audit.compare(rising, shifted)
    assert r["timing"]["bestShiftSeconds"] == -60 and r["verdict"] == "fail"
    off = [dict(b, t=b["t"] + 1500) for b in owner]
    assert audit.compare(owner, off)["timing"]["stampsOffTheGrid"] == 50
    assert audit.compare([], owner)["verdict"] == "no-data"


def test_audit_compares_without_storing_the_second_copy(store):
    store.watch_spec("fake", resolve("SPCX"))
    store.watch_spec("other", resolve("SPCX"))
    bars = _minutes(date(2026, 10, 20), count=30)
    store.upsert_bars("fake", "SPCX", "minute", 1, bars)
    auditor = Other(entitled=("minute",), bars={"minute": [dict(b, c=b["c"] + 0.005) for b in bars]})
    a, b = bars[0]["t"], bars[-1]["t"] + 60_000
    r = audit.audit_span(auditor, store, "SPCX", "minute", a, b, "fake", "manual", log=_log)
    assert r["verdict"] == "pass" and r["matched"] == 30
    assert store.bar_range("other", "SPCX", "minute") is None  # compared, never stored
    saved = store.audits(5)[0]
    assert saved["owner"] == "fake" and saved["auditor"] == "other" and saved["report"]["box"]["c"]["n"] == 30
    assert store.audit_scores()["other"] == {"audits": 1, "passed": 1, "accuracy": 1.0}


def test_cross_venue_crypto_audits_say_so_and_do_not_judge_volume(store):
    from massive_collate.gui import activity  # noqa: F401  (the GUI reads report["note"])

    class Alpaca(FakeSource):
        name = "alpaca"

    store.watch_spec("massive", resolve("BTCUSD"))
    store.watch_spec("alpaca", resolve("BTCUSD"))
    bars = _minutes(date(2026, 10, 20), (0, 0), 60, 80000.0)
    store.upsert_bars("massive", "BTCUSD", "minute", 1, bars)
    kraken = Alpaca(entitled=("minute",), bars={"minute": [dict(b, v=b["v"] * 0.05) for b in bars]})
    r = audit.audit_span(kraken, store, "BTCUSD", "minute", bars[0]["t"], bars[-1]["t"] + 60_000, "massive",
                         "manual", log=_log)
    assert r["verdict"] == "pass"  # 95 % less volume on one exchange is not an error
    assert "cross-venue" in r["report"]["note"] and r["report"]["volumeJudged"] is False


def test_new_data_queues_an_audit_that_the_other_feed_runs(store):
    for feed in ("fake", "other"):
        store.watch_spec(feed, resolve("SPCX"))
        store.set_listing(feed, "SPCX", None, None, NOW.date(), "SpaceX", "stock")
    store.upsert_bars("fake", "SPCX", "minute", 1, _minutes(date(2024, 10, 22)))
    store.set_backfilled("fake", "SPCX", date(2024, 10, 22))
    today = _minutes(NOW.date(), (9, 30), 5, 101.0)
    other = Other(entitled=("minute",), bars={"minute": today, "day": []})
    sync(other, store, ALPACA, NOW, log=_log, feeds=_feeds())
    queued = store.audit_queue()
    assert queued and queued[0]["owner"] == "other" and queued[0]["end_ms"] == today[-1]["t"] + 60_000
    # Massive-like feed: today's session is not on its end-of-day plan yet, so the audit waits ...
    first = FakeSource(entitled=("minute",), bars={"minute": today})
    assert audit.run_audits(first, store, _feeds()["fake"], _feeds(), NOW, queued=2, random_count=0, log=_log) == 0
    # ... and runs once it is published, then leaves the queue
    later = datetime(2026, 10, 22, 6, 0, tzinfo=ET)
    assert audit.run_audits(first, store, _feeds()["fake"], _feeds(), later, queued=2, random_count=0, log=_log) == 1
    assert store.audit_queue() == [] and store.audits(1)[0]["reason"] == "after-download"


def test_fetcher_rows_show_what_this_feed_still_needs(store):
    from massive_collate.config import load_settings
    from massive_collate.gui.activity import symbol_rows

    settings = load_settings(env={"MASSIVE_API_KEY": "x"}, env_file=None)  # Alpaca without keys
    for feed in ("massive", "alpaca"):
        store.watch_spec(feed, resolve("AAPL::NASDAQ"))
        store.watch_spec(feed, resolve("BTCUSD"))
    rows = {r["ticker"]: r for r in symbol_rows(store, settings, "alpaca", NOW)}
    assert rows["AAPL"]["state"] == "blocked" and rows["AAPL"]["note"] == "keys not set"
    assert rows["AAPL"]["requestsLeft"] == 0
    # crypto: Kraken (via Alpaca) is another market than Massive's consolidated Bitcoin, so it fills its
    # own history from 2016 whatever Massive holds; two feeds of the same market would share it instead
    assert rows["BTCUSD"]["state"] != "blocked" and 3940 < rows["BTCUSD"]["gapDays"] < 3950


def test_one_download_per_span_holds_within_a_market_not_across(store):
    from massive_collate.config import load_settings
    from massive_collate.sources import feed_profiles

    settings = load_settings(env={"MASSIVE_API_KEY": "x", "ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"},
                             env_file=None)
    feeds = feed_profiles(settings)
    for feed in ("massive", "alpaca"):
        store.watch(feed, ["AAPL"], "minute")
    store.watch("massive", ["BTCUSD"], "minute")
    store.watch("coinbase", ["BTCUSD"], "minute")
    store.upsert_bars("massive", "AAPL", "minute", 1, [{"t": ms(datetime(2026, 10, 20, 10, tzinfo=ET)), "o": 1, "h": 1,
                                                      "l": 1, "c": 1, "v": 1}])
    store.upsert_bars("massive", "BTCUSD", "minute", 1, [{"t": ms(datetime(2026, 10, 20, 10, tzinfo=ET)), "o": 1,
                                                        "h": 1, "l": 1, "c": 1, "v": 1}])
    held = ms(datetime(2026, 10, 20, 10, tzinfo=ET))
    sip = plan(store, feeds["alpaca"], feeds, "AAPL", "stock", "minute", date(2024, 10, 22), NOW)
    assert not any(a <= held < b for a, b in sip)  # same market (SIP): the span Massive holds is not fetched again
    venue = plan(store, feeds["coinbase"], feeds, "BTCUSD", "crypto", "minute", date(2026, 9, 1), NOW)
    assert any(a <= held < b for a, b in venue)  # Coinbase is its own market: it fetches that span too


def test_audits_are_one_hour_spot_checks_within_two_percent_of_a_run():
    import random

    from massive_collate.audit import allowance, spot

    rng = random.Random(4)
    a, b = 0, 86_400_000
    s0, s1 = spot(a, b, rng)
    assert s1 - s0 == 3_600_000 and a <= s0 and s1 <= b and s0 % 60_000 == 0
    assert spot(0, 600_000, rng) == (0, 600_000)  # shorter than an hour: all of it

    class Client:
        request_budget = 3000

    class Src:
        client = Client()

    assert allowance(Src()) == 60  # 2 % of 3,000
    Client.request_budget = 250
    assert allowance(Src()) == 5
    assert allowance(object()) == 1  # no cap known: one audit
