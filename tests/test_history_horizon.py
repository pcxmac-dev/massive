"""History takes completed sessions only (freshness.history_horizon): the session under way belongs to
the live feeds, so during market hours a run does not re-fetch the open day of every stock on a
plan that publishes as it trades; a symbol opened in the chart or on a watchlist still gets its
current session (its best feed for it is this one)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from massive_collate import freshness
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS
from massive_collate.live import WATCHLISTS_KEY

from .conftest import FakeSource

ADV, FREE = TIERS["advanced"], TIERS["free"]


def test_the_horizon_is_the_last_completed_session_on_a_plan_that_publishes_as_it_trades():
    wed_noon = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
    assert freshness.history_horizon("stock", ADV, wed_noon) == datetime(2026, 10, 20, 20, 0, tzinfo=ET)  # Tuesday's end
    wed_night = datetime(2026, 10, 21, 20, 30, tzinfo=ET)
    assert freshness.history_horizon("stock", ADV, wed_night) == datetime(2026, 10, 21, 20, 0, tzinfo=ET)
    sunday = datetime(2026, 10, 25, 23, 0, tzinfo=ET)
    assert freshness.history_horizon("stock", ADV, sunday) == datetime(2026, 10, 23, 20, 0, tzinfo=ET)  # Friday's end
    # a delayed plan: the session's end plus its delay
    delayed = TIERS["starter"]
    at_2010 = datetime(2026, 10, 21, 20, 10, tzinfo=ET)
    assert freshness.history_horizon("stock", delayed, at_2010) == datetime(2026, 10, 20, 20, 15, tzinfo=ET)
    # an end-of-day plan has completed sessions only already; crypto trades around the clock
    assert freshness.history_horizon("stock", FREE, wed_noon) == wed_noon
    assert freshness.history_horizon("crypto", ADV, wed_noon) == wed_noon


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _complete(store, tickers, synced):
    """Stocks holding every bar through Tuesday 20 Oct's session, synced after it."""
    for t in tickers:
        store.watch("fake", [t], "minute")
        store.set_backfilled("fake", t, date(2003, 9, 10))
        store.set_synced("fake", t, synced)
        start = _ms(datetime(2003, 9, 10, tzinfo=ET))
        store.add_coverage("fake", t, "minute", start, _ms(datetime(2026, 10, 20, 20, 0, tzinfo=ET)))
        store.add_coverage("fake", t, "day", start, _ms(datetime(2026, 10, 21, tzinfo=ET)))


def test_during_a_session_only_symbols_on_a_watchlist_are_updated(store):
    wed_noon = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
    _complete(store, ["MINE", "OTHER"], datetime(2026, 10, 20, 21, 0, tzinfo=ET))  # after Tuesday's session
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["MINE"]}]))
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    sync(src, store, ADV, wed_noon, log=lambda *_: None)
    asked = {c[0] for c in getattr(src, "bar_calls", []) if c[3] == "minute"}
    assert asked == {"MINE"}  # OTHER holds every completed session: nothing for it until Wednesday's ends
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    sync(src, store, ADV, datetime(2026, 10, 21, 20, 30, tzinfo=ET), log=lambda *_: None)
    assert "OTHER" in {c[0] for c in getattr(src, "bar_calls", []) if c[3] == "minute"}  # Wednesday's is complete


def test_the_fetcher_does_not_call_every_stock_new_during_the_session(store):
    from massive_collate.gui import activity

    wed_noon = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
    item = {"ticker": "OTHER", "asset": "stock", "resolution": "minute", "listed_from": None,
            "backfilled_to": date(2020, 1, 1), "synced_at": datetime(2026, 10, 20, 21, 0, tzinfo=ET)}
    last = int(datetime(2026, 10, 20, 19, 59, tzinfo=ET).timestamp() * 1000)
    horizon = freshness.history_horizon("stock", ADV, wed_noon)
    row = activity.symbol_status(item, (last - 60_000, last), date(2020, 1, 1), wed_noon.date(), wed_noon, ADV, None,
                                 horizon=horizon)
    assert row["state"] == "current"
    row = activity.symbol_status(item, (last - 60_000, last), date(2020, 1, 1), wed_noon.date(), wed_noon, ADV, None)
    assert row["state"] == "new"  # opened or on a list: the session under way is fetched for it
