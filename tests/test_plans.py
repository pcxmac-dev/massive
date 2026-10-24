"""Massive sells a plan per asset class: the Stocks plan (MASSIVE_TIER) does not reach the other
classes, which keep their own history, publication delay and rate; and a symbol refused run after
run is left alone for a while."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from massive_collate.collector import ET, Refusals, history_floor
from massive_collate.config import asset_tier, load_settings
from massive_collate.freshness import plan_delay
from massive_collate.sources import build_source
from massive_collate.sources.massive import asset_class
from massive_collate.storage import Store

from .conftest import FakeResponse, FakeSession

TODAY = date(2026, 10, 24)


def test_the_stocks_plan_does_not_reach_the_other_asset_classes():
    tier = load_settings(env={"MASSIVE_TIER": "advanced", "MASSIVE_TIER_FUTURES": "starter"}, env_file=None).tier
    assert tier.name == "advanced" and asset_tier(tier, "stock") is tier
    assert asset_tier(tier, "crypto").name == "free" and asset_tier(tier, "future").name == "starter"
    assert history_floor(tier, TODAY, "stock") == date(2003, 9, 10)
    assert history_floor(tier, TODAY, "crypto") == date(2024, 10, 25)  # the free plan's two years
    assert plan_delay(tier, "stock").describe() == "real time"
    assert plan_delay(tier, "crypto").kind == "utc_day"  # free crypto: the previous UTC day
    free = load_settings(env={}, env_file=None).tier
    assert free.by_asset == () and asset_tier(free, "crypto") is free


def test_each_request_is_counted_against_its_asset_classs_rate():
    assert asset_class("https://api.massive.com/v2/aggs/ticker/X:BTCUSD/range/1/minute/a/b") == "crypto"
    assert asset_class("https://api.massive.com/v2/aggs/ticker/C%3AEURUSD/range/1/day/a/b") == "fx"
    assert asset_class("/v3/reference/tickers/I:SPX") == "index"
    assert asset_class("/futures/v1/aggs/ESZ6") == "future"
    assert asset_class("/v2/aggs/grouped/locale/global/market/crypto/2026-10-23") == "crypto"
    assert asset_class("/v2/aggs/ticker/AAPL/range/1/minute/a/b") == "stock"

    settings = load_settings(env={"MASSIVE_API_KEY": "k", "MASSIVE_TIER": "advanced"}, env_file=None)
    src = build_source("massive", settings)
    assert src.client._limiter.max_calls is None  # stocks: unlimited on Advanced
    assert src.client.class_limiters["crypto"].max_calls == 5  # crypto: the free plan's 5 a minute
    used = []

    class Counted:
        def __init__(self, name):
            self.name = name

        def acquire(self):
            used.append(self.name)

    src.client._limiter, src.client.class_limiters = Counted("stock"), {"crypto": Counted("crypto")}
    src.client._session = FakeSession([FakeResponse(body={"results": []}), FakeResponse(body={"results": []})])
    src.client.get("/v2/aggs/ticker/X:BTCUSD/range/1/minute/2026-10-01/2026-10-02")
    src.client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
    assert used == ["crypto", "stock"]


def test_a_symbol_refused_run_after_run_is_left_alone_for_a_day(tmp_path):
    now = datetime(2026, 10, 24, 12, tzinfo=ET)
    with Store(tmp_path / "m.db") as store:
        r = Refusals(store, "massive")
        assert r.note("BTCUSD", now, "not in your plan") == "" and not r.paused("BTCUSD", now)
        r.note("BTCUSD", now, "not in your plan")
        assert "left alone" in r.note("BTCUSD", now, "not in your plan")
        again = Refusals(store, "massive")  # the next run reads it back
        assert again.paused("BTCUSD", now + timedelta(hours=23))
        assert not again.paused("BTCUSD", now + timedelta(hours=25))  # a day later: asked again
        again.clear("BTCUSD")
        assert Refusals(store, "massive").rows == {}
