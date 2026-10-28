"""Tiingo's forex: currency pairs' 1-minute bars from 2020 on a feed of its own, on Tiingo's limits."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from massive_collate import freshness
from massive_collate.collector import history_floor
from massive_collate.config import load_settings
from massive_collate.errors import NotEntitled
from massive_collate.sources import feed_profiles, providers
from massive_collate.sources.plugins import tiingofx
from massive_collate.sources.public import TiingoFxSource


class FakeTiingo:
    """Answers /tiingo/fx/prices with a bar every 6 hours of the days asked (and one outside them)."""

    def __init__(self):
        self.asked = []
        self.requests_made = 0

    def get(self, path, params):
        self.asked.append((path, dict(params)))
        self.requests_made += 1
        d, last = date.fromisoformat(params["startDate"]), date.fromisoformat(params["endDate"])
        rows = []
        while d <= last:
            for h in (0, 6, 12, 18):
                t = datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)
                rows.append({"date": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "ticker": params["tickers"],
                             "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15})
            d += timedelta(days=1)
        return rows


def test_a_pair_is_asked_three_whole_days_at_a_time_newest_first():
    client = FakeTiingo()
    pages = list(TiingoFxSource(client, has_key=True).bar_pages(
        "C:EURUSD", date(2026, 10, 20), date(2026, 10, 27), "minute", descending=True, asset="fx"))
    assert [(p["startDate"], p["endDate"]) for _, p in client.asked] == [
        ("2026-10-26", "2026-10-27"), ("2026-10-23", "2026-10-25"), ("2026-10-20", "2026-10-22")]
    assert {p["tickers"] for _, p in client.asked} == {"eurusd"} and client.asked[0][0] == "/tiingo/fx/prices"
    assert [len(p) for p in pages] == [8, 12, 12]
    first = pages[0]
    assert first[0]["t"] > first[-1]["t"] and first[0]["v"] is None  # newest first; forex has no volume
    day = 86_400_000
    lo = int(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp() * 1000)
    assert first.window == (lo, lo + 2 * day)  # each window recorded, empty or not


def test_nothing_before_2020_is_asked_and_only_minutes_with_a_key():
    client = FakeTiingo()
    pages = list(TiingoFxSource(client, has_key=True).bar_pages(
        "C:GBPUSD", date(2019, 12, 1), date(2020, 1, 2), "minute", asset="fx"))
    assert client.asked[0][1]["startDate"] == "2020-01-01" and len(client.asked) == 1  # Tiingo refuses earlier
    assert sum(len(p) for p in pages) == 8
    with pytest.raises(NotEntitled):
        next(TiingoFxSource(client, has_key=True).bar_pages("C:EURUSD", date(2026, 1, 5), date(2026, 1, 5), "hour"))
    with pytest.raises(NotEntitled):
        next(TiingoFxSource(client, has_key=False).bar_pages("C:EURUSD", date(2026, 1, 5), date(2026, 1, 5), "minute"))


def test_the_feed_is_its_own_market_on_tiingos_plan_and_limits(monkeypatch):
    settings = load_settings(env={"TIINGO_API_KEY": "k", "TIINGO_PLAN": "power"}, env_file=None)
    view = settings.for_source("tiingofx")
    assert view.tier.name == "tiingo-power" and view.max_requests_per_day == 95_000  # Tiingo's plan and caps
    assert history_floor(view.tier, date(2026, 10, 28), "fx") == date(2020, 1, 1)
    assert freshness.plan_delay(view.tier, "fx").kind == "utc_day"  # whole days: history, not live
    assert providers()["tiingofx"].rate_window(settings) == providers()["tiingo"].rate_window(settings)
    profile = feed_profiles(settings)["tiingofx"]
    assert profile.assets == frozenset({"fx"}) and profile.venues == {"fx": "tiingofx"}  # not Massive's 'otc'
    assert not feed_profiles(load_settings(env={}, env_file=None))["tiingofx"].assets  # no key: serves nothing
    # one account: the window and the day's cap are held under Tiingo's name, shared with its stocks feed
    held = []
    monkeypatch.setattr(tiingofx, "limiter", lambda name, s, key: held.append(("window", name)))
    monkeypatch.setattr(tiingofx, "machine_caps", lambda client, name, s, key: held.append(("day", name)) or client)
    source = tiingofx.build(settings, 5)
    assert held == [("window", "tiingo"), ("day", "tiingo")] and source.name == "tiingofx"


def test_a_sync_fills_a_pair_from_2020_inverted_as_shown_through_the_day_before(store):
    from massive_collate.collector import sync
    from massive_collate.coverage import FeedProfile
    from massive_collate.symbols import resolve

    settings = load_settings(env={"TIINGO_API_KEY": "k", "TIINGO_PLAN": "power"}, env_file=None)
    tier = settings.for_source("tiingofx").tier
    store.watch_spec("tiingofx", resolve("USDEUR"), "minute")  # shown as USD/EUR: Tiingo's eurusd inverted
    client = FakeTiingo()
    now = datetime(2020, 1, 10, 12, tzinfo=timezone.utc)
    sync(TiingoFxSource(client, has_key=True), store, tier, now, log=lambda *_: None,
         feeds={"tiingofx": FeedProfile("tiingofx", tier, frozenset({"fx"}), 1, venues={"fx": "tiingofx"})})
    asked = [(p["startDate"], p["endDate"]) for _, p in client.asked]
    assert asked and {p["tickers"] for _, p in client.asked} == {"eurusd"}
    assert min(a for a, _ in asked) == "2020-01-01" and max(b for _, b in asked) <= "2020-01-09"  # whole days to yesterday
    lo = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bars = store.bars_window(["tiingofx"], "USDEUR", "minute", lo, lo + 10 * 86_400_000, filtered=False)
    # every day held, inverted; the history floor's day starts at New York midnight, so 00:00 UTC on
    # 1 January (New Year's Day: the market shut) is before it
    assert len(bars) == 9 * 4 - 1 and abs(bars["close"].iloc[0] - 1 / 1.15) < 1e-9
    assert abs(bars["high"].iloc[0] - 1 / 1.0) < 1e-9 and abs(bars["low"].iloc[0] - 1 / 1.2) < 1e-9
