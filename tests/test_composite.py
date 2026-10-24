"""Composite and reference models of an asset on several exchanges, and quality scores."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from massive_collate import composite
from massive_collate.coverage import FeedProfile
from massive_collate.config import TIERS
from massive_collate.symbols import resolve

T0 = int(datetime(2026, 10, 1, 15, tzinfo=timezone.utc).timestamp() * 1000)


def _bars(closes, highs=None, lows=None, vol=10.0):
    idx = [T0 + i * 60_000 for i in range(len(closes))]
    highs = highs or [c + 0.2 for c in closes]
    lows = lows or [c - 0.2 for c in closes]
    return pd.DataFrame({"o": closes, "h": highs, "l": lows, "c": closes, "v": [vol] * len(closes),
                         "vw": [np.nan] * len(closes), "n": [5] * len(closes)}, index=idx)


def test_composite_weights_by_volume_and_quality_and_measures_the_spread():
    bars = {"coinbase": _bars([100.0, 100.0], vol=10), "kraken": _bars([100.02, 100.02], vol=10),
            "bitfinex": _bars([100.10, 100.10], vol=10)}
    even = composite.combine(bars, {"coinbase": 1, "kraken": 1, "bitfinex": 1})
    downgraded = composite.combine(bars, {"coinbase": 1, "kraken": 1, "bitfinex": 0.2})
    assert even["c"].iloc[0] == pytest.approx(100.04)
    assert downgraded["c"].iloc[0] < even["c"].iloc[0]  # an outlier exchange of low quality pulls less
    r = even.iloc[0]
    assert r["venues"] == 3 and r["v"] == 30 and r["members"] == "bitfinex,coinbase,kraken"
    assert r["env_h"] == pytest.approx(100.30) and r["env_l"] == pytest.approx(99.80)
    assert r["range_bps"] == pytest.approx(10.0, rel=1e-3)
    assert 3.0 < r["sd_bps"] < 5.0


def test_an_outlier_exchange_is_flagged_and_scores_lower():
    closes = [100.0 + i * 0.01 for i in range(60)]
    bars = {"coinbase": _bars(closes), "kraken": _bars([c + 0.005 for c in closes]),
            "bitstamp": _bars([c - 0.004 for c in closes]),
            "bitfinex": _bars([c * 1.0025 for c in closes], highs=[c * 1.0035 for c in closes])}  # +25 bps
    comp = composite.combine(bars, {v: 1.0 for v in bars})
    assert (comp["outliers"] == "bitfinex").all()
    stats = composite.leave_one_out(bars, {v: 1.0 for v in bars})
    q = {v: composite.score(s) for v, s in stats.items()}
    assert q["bitfinex"] < 0.2 < 0.85 < min(q["coinbase"], q["kraken"], q["bitstamp"])
    assert stats["bitfinex"]["outlierShare"] == 1.0 and stats["kraken"]["completeness"] == 1.0


def _feeds():
    mk = lambda n, v: FeedProfile(n, TIERS["free"], frozenset({"crypto"}), venues={"crypto": v})  # noqa: E731
    return {"massive": mk("massive", "consolidated"), "coinbase": mk("coinbase", "coinbase"),
            "alpaca": mk("alpaca", "kraken"), "bitfinex": mk("bitfinex", "bitfinex")}


def test_refresh_builds_the_composite_and_quality_from_exchange_feeds_only(store):
    for feed in ("massive", "coinbase", "alpaca", "bitfinex"):
        store.watch_spec(feed, resolve("BTCUSD"))
    mk = lambda c, d=0.0: [{"t": T0 + i * 60_000, "o": c + d, "h": c + d + 1, "l": c + d - 1, "c": c + d, "v": 2.0}  # noqa: E731
                           for i in range(30)]
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, mk(84000.0))
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, mk(84000.0, 3.0))
    store.upsert_bars("bitfinex", "BTCUSD", "minute", 1, mk(84000.0, 80.0))  # ~ +9.5 bps
    store.upsert_bars("massive", "BTCUSD", "minute", 1, mk(84000.0, 500.0))  # a consolidation: not an input
    now = datetime.fromtimestamp((T0 + 3_600_000) / 1000, timezone.utc)
    done = composite.refresh(store, _feeds(), now, log=lambda m: None)
    assert done["BTCUSD"] == 30
    stats = store.composite_stats("BTCUSD", "minute")
    assert len(stats) == 30 and set(stats["venues"]) == {3} and stats["members"].iloc[0] == "bitfinex,coinbase,kraken"
    comp = store.load_bars(["BTCUSD"], "minute", source="composite")
    assert comp["close"].max() < 84050  # Massive's consolidated bars are not part of it
    assert store.quality("bitfinex", "crypto")["score"] < store.quality("coinbase", "crypto")["score"]
    assert composite.reference_feed(_feeds(), "crypto", {"coinbase", "bitfinex"}) == "coinbase"
    assert composite.quality_of(store, "massive", "crypto") == 0.9  # not measured here: its prior


def test_chart_engine_serves_the_composite_with_its_band(store):
    from massive_collate.gui.bars import BarEngine

    store.watch_spec("coinbase", resolve("BTCUSD"))
    store.watch_spec("alpaca", resolve("BTCUSD"))
    comp = composite.combine({"coinbase": _bars([84000.0, 84010.0, 84020.0]),
                              "kraken": _bars([84008.0, 84012.0, 84030.0])}, {"coinbase": 1, "kraken": 1})
    store.save_composite("BTCUSD", "minute", comp)
    engine = BarEngine(store)
    page = engine.bars("composite", "BTCUSD", "minute", 3, session="extended")
    bar = page["bars"][0]
    assert bar["env_high"] == pytest.approx(84030.2) and bar["env_low"] == pytest.approx(83999.8)
    assert bar["venues_n"] == 2 and bar["dev_sd"] > 0
    assert "composite" not in engine.sources_for("BTCUSD")  # a model, not a feed to merge


def test_single_exchange_minutes_have_no_composite(store):
    for feed in ("coinbase", "alpaca"):
        store.watch_spec(feed, resolve("BTCUSD"))
    mk = lambda start, n: [{"t": T0 + i * 60_000, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0} for i in range(start, n)]  # noqa: E731
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, mk(0, 10))
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, mk(5, 10))  # Kraken only from minute 5
    composite.refresh(store, _feeds(), datetime.fromtimestamp((T0 + 3_600_000) / 1000, timezone.utc), log=lambda m: None)
    assert list(store.composite_stats("BTCUSD", "minute")["venues"]) == [2] * 5
    store.save_composite("BTCUSD", "minute", composite.combine({"coinbase": _bars([5.0])}, {"coinbase": 1}))
    assert store.drop_single_exchange_composites() == 1


def test_audits_pair_only_feeds_of_the_same_market(store):
    import random

    from massive_collate.audit import random_span

    feeds = _feeds()
    store.watch_spec("coinbase", resolve("BTCUSD"))
    store.watch_spec("alpaca", resolve("BTCUSD"))
    store.add_coverage("alpaca", "BTCUSD", "minute", T0 - 10 * 86_400_000, T0)
    from massive_collate.sources.public import CoinbaseSource, PublicClient

    src = CoinbaseSource(PublicClient("https://x.test"))
    now = datetime.fromtimestamp(T0 / 1000, timezone.utc)
    assert random_span(store, src, feeds["coinbase"], now, random.Random(1), feeds=feeds) is None  # Kraken is another market


def test_two_exchanges_cannot_outvote_each_other_and_backfills_are_rebuilt(store):
    two = composite.combine({"coinbase": _bars([100.0]), "kraken": _bars([100.5])}, {"coinbase": 1, "kraken": 1})
    assert two["outliers"].iloc[0] == ""  # 50 bps apart, but which one is off? needs a third
    for feed in ("coinbase", "alpaca", "bitfinex"):
        store.watch_spec(feed, resolve("BTCUSD"))
    mk = lambda days, n=3: [{"t": T0 - days * 86_400_000 + i * 60_000, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0}  # noqa: E731
                            for i in range(n)]
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, mk(0) + mk(40))
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, mk(0) + mk(40))
    now = datetime.fromtimestamp((T0 + 3_600_000) / 1000, timezone.utc)
    composite.refresh(store, _feeds(), now, log=lambda m: None)
    assert set(store.composite_stats("BTCUSD", "minute")["venues"]) == {2}
    store.upsert_bars("bitfinex", "BTCUSD", "minute", 1, mk(0) + mk(40))  # a new exchange, 40 days back
    composite.refresh(store, _feeds(), now, log=lambda m: None)
    assert set(store.composite_stats("BTCUSD", "minute")["venues"]) == {3}  # old days rebuilt too
