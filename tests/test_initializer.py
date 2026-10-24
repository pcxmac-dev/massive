"""The one-time initializer (every NYSE and Nasdaq listing, most traded first), the sync taking
backfills in that order, and finding which Massive plan the key has."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from massive_collate import cli, initializer
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings, tier_for
from massive_collate.errors import NotEntitled
from massive_collate.sources.massive import MassiveSource
from massive_collate.storage import Store

from .conftest import FakeSource

TODAY = date(2026, 10, 24)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


def _listings(store):
    rows = [{"ticker": t, "name": t, "exchange": mic, "type": ty, "kind": "stock"} for t, mic, ty in (
        ("AAPL", "XNAS", "CS"), ("PENNY", "XNAS", "CS"), ("KO", "XNYS", "CS"), ("BACPE", "XNYS", "PFD"),
        ("QUIET", "XNYS", "CS"), ("SPY", "ARCX", "ETF"))]
    store.update_directory("massive", "stock", rows, TODAY)
    for day in (date(2026, 10, 22), date(2026, 10, 23)):  # AAPL trades the most value, PENNY the most shares
        store.save_market_snapshot("massive", day, [
            {"T": "AAPL", "c": 250.0, "v": 50e6, "vw": 250.0}, {"T": "PENNY", "c": 0.5, "v": 900e6, "vw": 0.5},
            {"T": "KO", "c": 70.0, "v": 10e6, "vw": 70.0}, {"T": "BACPE", "c": 25.0, "v": 1e6, "vw": 25.0},
            {"T": "SPY", "c": 600.0, "v": 80e6, "vw": 600.0}])


def test_listings_are_ranked_by_traded_value_or_shares(store):
    _listings(store)
    ranked = store.listings_by_volume("massive", ("XNYS", "XNAS"), skip_types=("PFD",))
    assert [r["ticker"] for r in ranked] == ["AAPL", "KO", "PENNY", "QUIET"]  # no volume yet: last
    assert ranked[0]["measure"] == pytest.approx(50e6 * 250.0) and ranked[-1]["measure"] is None
    by_shares = store.listings_by_volume("massive", ("XNYS", "XNAS"), by="shares", skip_types=("PFD",))
    assert [r["ticker"] for r in by_shares] == ["PENNY", "AAPL", "KO", "QUIET"]


def test_the_initializer_watches_every_listing_once_and_keeps_the_order(store):
    _listings(store)
    store.watch("massive", ["KO"], "trades", exchange="XNYS")  # already watched: left as it is
    dry = initializer.initialize(store, ["massive", "alpaca", "tiingo"], dry_run=True)
    assert dry["added"] == {"massive": 3, "alpaca": 4, "tiingo": 4} and len(store.watchlist()) == 1
    out = initializer.initialize(store, ["massive", "alpaca", "tiingo"])
    assert out["listed"] == 4 and out["ranked"] == 3 and out["top"] == ["AAPL", "KO", "PENNY", "QUIET"]
    rows = {(w["source"], w["ticker"]): w for w in store.watchlist()}
    assert len(rows) == 12 and ("massive", "SPY") not in rows and ("massive", "BACPE") not in rows
    assert rows[("massive", "KO")]["resolution"] == "trades"  # kept
    assert rows[("massive", "AAPL")]["resolution"] == "minute" and rows[("alpaca", "AAPL")]["resolution"] == "minute"
    assert rows[("tiingo", "AAPL")]["resolution"] == "auto" and rows[("massive", "PENNY")]["exchange"] == "XNAS"
    assert initializer.load_order(store) == {"AAPL": 0, "KO": 1, "PENNY": 2, "QUIET": 3}
    assert json.loads(store.kv_get(initializer.DONE_KEY))["added"]["massive"] == 3
    assert initializer.initialize(store, ["massive"])["added"] == {"massive": 0}  # again: nothing new


def test_backfills_go_in_the_initializers_order(store):
    src = FakeSource()
    store.watch("fake", ["AAA", "BBB", "CCC"], "minute")
    store.kv_set(initializer.ORDER_KEY, json.dumps({"CCC": 0, "AAA": 1}))  # BBB unranked: after them
    order = []
    sync(src, store, TIERS["free"], datetime(2026, 10, 21, 12, 0, tzinfo=ET), log=lambda m: None,
         on_item=order.append)
    assert order == ["CCC", "AAA", "BBB"]


def test_watchlist_rows_of_one_ticker(store):
    store.watch("massive", ["AAPL", "MSFT"], "minute")
    store.watch("alpaca", ["aapl"], "auto")
    assert sorted(w["source"] for w in store.watchlist(ticker="aapl")) == ["alpaca", "massive"]
    assert [w["ticker"] for w in store.watchlist("massive", "MSFT")] == ["MSFT"]


class _PlanClient:
    """Massive as a plan with `years` of history, and second bars / trades / quotes or not."""

    def __init__(self, years: int, second=False, trades=False, quotes=False):
        self.years, self.second, self.trades, self.quotes, self.paths = years, second, trades, quotes, []

    def pages(self, path, params, **kw):
        self.paths.append(path)
        if path.startswith("/v3/trades") and not self.trades:
            raise NotEntitled(403, "not in your plan")
        if "/range/1/second/" in path and not self.second:
            raise NotEntitled(403, "not in your plan")
        if "/range/1/day/" in path and (TODAY - date.fromisoformat(path.split("/")[-2])).days > self.years * 365.25:
            raise NotEntitled(403, "Your plan doesn't include this data timeframe")
        yield [{"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]

    def get(self, path, params=None):
        self.paths.append(path)
        if "/v3/quotes/" in path and not self.quotes:
            raise NotEntitled(403, "not in your plan")
        return {"results": []}


@pytest.mark.parametrize("client, tier", [
    (_PlanClient(2), "free"), (_PlanClient(5, second=True), "starter"),
    (_PlanClient(10, second=True, trades=True), "developer"),
    (_PlanClient(30, second=True, trades=True, quotes=True), "advanced")])
def test_the_plan_is_found_from_what_the_key_may_ask_for(client, tier):
    reach = MassiveSource(client).plan_reach("AAPL", today=TODAY)
    assert tier_for(reach) == tier
    assert len(client.paths) == 7  # one small request per question


def test_a_new_plan_has_its_refusals_asked_again(store):
    log = []
    cli._note_tier(store, load_settings(env={}, env_file=None), log.append)  # the first sync: noted only
    store.set_entitlement("massive", "stock", "trades", False)  # refused on the free plan
    store.mark_grouped_day("massive", TODAY - timedelta(days=900), "denied")
    cli._note_tier(store, load_settings(env={}, env_file=None), log.append)
    assert log == [] and store.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0] == 1
    cli._note_tier(store, load_settings(env={"MASSIVE_TIER": "developer"}, env_file=None), log.append)
    assert store.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0] == 0
    assert store.grouped_days("massive") == {} and "free -> developer" in log[0]


def test_an_unlimited_plan_reaches_back_to_where_its_history_begins():
    from massive_collate.collector import history_floor
    from massive_collate.config import PUBLIC_FEEDS

    assert history_floor(TIERS["advanced"], TODAY) == date(2003, 9, 10)  # all of Massive's stock history
    assert history_floor(TIERS["developer"], TODAY) == date(2016, 10, 25)  # ten years, a day inside
    assert history_floor(PUBLIC_FEEDS["tiingo"][0], TODAY) == date(1962, 1, 1)


def test_the_oldest_daily_bar_dates_the_start_of_a_sources_history():
    class Client:
        def pages(self, path, params, **kw):
            assert "/range/1/day/1970-01-02/" in path and params["sort"] == "asc" and params["limit"] == 1
            yield [{"t": int(datetime(2003, 9, 10, tzinfo=ET).timestamp() * 1000)}]

    assert MassiveSource(Client()).oldest_day("AAPL", today=TODAY) == date(2003, 9, 10)


def test_a_plan_change_finds_each_symbols_resolution_again_but_bulk_stays_at_minutes(store):
    _listings(store)
    store.watch("massive", ["KO"], "minute", exchange="XNYS")  # chosen one by one (resolved on the old plan)
    store.watch("massive", ["ESZ6"], "minute", asset="future")
    initializer.initialize(store, ["massive", "alpaca"])
    assert initializer.load_bulk(store) == ["AAPL", "PENNY", "QUIET"]  # KO was watched before: not bulk
    assert cli.reresolve(store, "massive") == 1
    rows = {w["ticker"]: w["resolution"] for w in store.watchlist("massive")}
    assert rows == {"KO": "auto", "ESZ6": "minute", "AAPL": "minute", "PENNY": "minute", "QUIET": "minute"}
