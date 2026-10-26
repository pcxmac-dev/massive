"""A run lasts hours: a symbol opened in the chart part-way through is fetched up to the time it is
served, not to when the run began, and its stamp meets its request. (SPCX, opened at 15:06 ET in a
run begun at 12:57, was fetched only to 12:54, leaving a two-hour hole the live feed could not fill,
and stayed "next in line".)"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from massive_collate import collector, freshness
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS

from .conftest import FakeSource

ADV = TIERS["advanced"]
START = datetime(2026, 10, 21, 12, 57, tzinfo=ET)  # a Wednesday, the session under way
LATER = timedelta(hours=2, minutes=9)  # SPCX opened at 15:06


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_symbol_opened_mid_run_is_fetched_up_to_when_it_is_served(store, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(collector, "monotonic", clock)
    store.watch("fake", ["SPCX"], "minute")  # every bar through Tuesday's session, synced after it
    store.set_backfilled("fake", "SPCX", date(2003, 9, 10))
    store.set_synced("fake", "SPCX", datetime(2026, 10, 20, 21, 0, tzinfo=ET))
    head = _ms(datetime(2003, 9, 10, tzinfo=ET))
    store.add_coverage("fake", "SPCX", "minute", head, _ms(datetime(2026, 10, 20, 20, 0, tzinfo=ET)))
    store.add_coverage("fake", "SPCX", "day", head, _ms(datetime(2026, 10, 21, tzinfo=ET)))
    store.watch("fake", ["AAA", "ZZZ"], "minute")  # still filling: the run works on AAA when SPCX is opened
    asked = START + LATER

    def on_item(ticker):
        if ticker == "AAA":
            clock.now += LATER.total_seconds()
            collector.request_priority(store, "SPCX", asked)

    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    sync(src, store, ADV, START, log=lambda *_: None, on_item=on_item)
    ends = [c[2] for c in src.bar_calls if c[0] == "SPCX" and c[3] == "minute"]
    assert ends, "SPCX was not fetched"
    edge = asked - freshness.SETTLE - timedelta(minutes=1)
    assert max(ends) >= edge > START  # up to 15:06 less its settling, not the run's 12:57
    synced = store.watchlist("fake", "SPCX")[0]["synced_at"]
    assert synced >= asked  # stamped when it was served
    assert collector._unserved_request(store, "fake", asked + timedelta(minutes=1)) is None  # not "next in line"
