"""Data providers are plugins (sources/registry.py): the built-in ones keep every fact the app had
about them, and a provider from an installed package is found, built, keyed and listed like them."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from massive_collate import cli, composite, config
from massive_collate.config import PUBLIC_FEEDS, Tier, load_settings
from massive_collate.sources import (build_source, feed_profiles, is_archive, markets, provider, providers,
                                     rate_window, registry, supports)
from massive_collate.sources.registry import KeyVar, Provider
from massive_collate.symbols import resolve

from .conftest import FakeSource

BUILT_IN = ["massive", "alpaca", "bitstamp", "coinbase", "bitfinex", "cboe", "tiingo"]


class AcmeSource(FakeSource):
    name = "acme"


ACME = Provider(
    name="acme", title="Acme Data", build=lambda settings, budget: AcmeSource(budget=budget),
    assets=frozenset({"stock"}), about="stock minute bars", keys=(KeyVar("ACME_API_KEY"),), signup="acme.example",
    plan_vars=("ACME_PLAN",), tier=Tier("acme-free", requests_per_minute=30, history_years=3), per_run=100,
    per_day=1000, limits="30 requests per minute", kind="exchange", symbols_per_month=None,
    carries=lambda spec: spec.ticker != "ZZZZ",
)


@pytest.fixture
def installed(monkeypatch):
    """Packages' entry points, as `importlib.metadata` would list them; the registry looks again."""
    def install(*entries):
        eps = [SimpleNamespace(name=name, value=value, load=load) for name, value, load in entries]
        monkeypatch.setattr(registry, "_entry_points", lambda: eps)
        return registry.reload()

    yield install
    registry._providers = None  # found again (built-ins only) once the entry points are restored


def test_the_built_in_providers_load_in_their_order_without_errors():
    assert list(providers()) == BUILT_IN
    assert registry.errors() == []
    assert all(p.origin == "built in" and callable(p.build) for p in providers().values())
    assert {n for n in BUILT_IN if is_archive(n)} == {"cboe", "tiingo"}


def test_each_built_in_keeps_its_plan_rate_caps_and_markets():
    s = load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None)
    assert rate_window("massive", s) == (5, 62.0)
    assert rate_window("alpaca", s) == (190, 60.0)
    assert rate_window("coinbase", s) == (8, 1.0)
    assert rate_window("tiingo", s) == (45, 3600.0)
    assert rate_window("bitfinex", s) == (20, 60.0)
    for name in ("bitstamp", "coinbase", "bitfinex", "cboe", "tiingo"):
        tier, per_run, per_day = PUBLIC_FEEDS[name]
        view = s.for_source(name)
        assert (view.tier, view.max_requests_per_run, view.max_requests_per_day) == (tier, per_run, per_day)
    assert s.for_source("alpaca").tier.name == "alpaca-basic" and s.for_source("alpaca").max_requests_per_day == 30000
    assert s.for_source("massive") is s
    limited = load_settings(env={"CRYPTO_VENUE_DAYS": "30"}, env_file=None)
    assert limited.for_source("coinbase").tier.history_days == 30
    assert limited.for_source("bitstamp").tier.history_days is None
    assert markets("massive", s)["stock"] == "sip" and markets("alpaca", s) == {"stock": "sip", "crypto": "kraken"}
    assert markets("tiingo", s) == {"stock": "sip"} and markets("cboe", s) == {"index": "index"}
    assert markets("bitstamp", s) == {"crypto": "bitstamp"}


def test_what_each_built_in_serves_follows_its_keys():
    none = feed_profiles(load_settings(env={}, env_file=None))
    assert "massive" not in none  # not set up at all without its key
    assert none["alpaca"].assets == {"crypto"}  # crypto needs no key
    assert none["tiingo"].assets == frozenset()  # listed, serving nothing
    assert none["coinbase"].assets == {"crypto"}
    keyed = feed_profiles(load_settings(env={"MASSIVE_API_KEY": "k", "ALPACA_API_KEY_ID": "i",
                                             "ALPACA_API_SECRET_KEY": "s", "TIINGO_API_KEY": "t"}, env_file=None))
    assert keyed["alpaca"].assets == {"stock", "crypto"} and keyed["tiingo"].assets == {"stock"}
    assert "future" in keyed["massive"].assets
    assert supports("coinbase", resolve("BTCUSD")) and not supports("coinbase", resolve("AAPL"))
    assert supports("cboe", resolve("I:VIX")) and not supports("bitstamp", resolve("BTCUSD").__class__(
        **{**resolve("BTCUSD").__dict__, "ticker": "NOPEUSD"}))


def test_composite_and_audits_read_the_plugins_facts():
    from massive_collate import audit, initializer

    assert composite.reference("crypto") == "coinbase" and composite.reference("stock") is None
    assert composite.feed_kind("alpaca", "stock") == "consolidated"
    assert composite.feed_kind("alpaca", "crypto") == "exchange"
    assert composite.feed_kind("cboe", "index") == "official" and composite.feed_kind("composite", "crypto") == "consolidated"
    assert audit.venue_note("alpaca", "crypto") == "one exchange's book (Kraken)" and audit.venue_note("tiingo", "stock") is None
    assert initializer.bulk_resolution("massive") == "minute" and initializer.bulk_resolution("tiingo") == "auto"


def test_a_provider_from_an_installed_package_is_found_built_and_keyed(installed):
    found = installed(("acme", "acme_feed:PLUGIN", lambda: ACME))
    assert list(found) == BUILT_IN + ["acme"]  # after the built-ins (order 100)
    assert provider("acme").origin == "package: acme_feed:PLUGIN"
    assert "ACME_API_KEY" in config.saved_names() and "ACME_PLAN" in config.saved_names()

    without = load_settings(env={}, env_file=None)
    assert feed_profiles(without)["acme"].assets == frozenset()  # its key is not set
    s = load_settings(env={"ACME_API_KEY": "secret-acme", "ACME_PLAN": "pro"}, env_file=None)
    assert s.env == {"ACME_API_KEY": "secret-acme", "ACME_PLAN": "pro"} and "secret-acme" not in repr(s)
    profile = feed_profiles(s)["acme"]
    assert profile.assets == {"stock"} and profile.venues == {"stock": "acme"}
    view = s.for_source("acme")
    assert (view.tier.name, view.max_requests_per_run, view.max_requests_per_day) == ("acme-free", 100, 1000)
    assert rate_window("acme", s) == (30, 60.0)
    source = build_source("acme", s, request_budget=7)
    assert isinstance(source, AcmeSource) and source.budget == 7
    assert supports("acme", resolve("AAPL")) and not supports("acme", resolve("ZZZZ"))
    assert not supports("acme", resolve("BTCUSD"))  # not its asset class
    assert composite.feed_kind("acme", "stock") == "exchange"
    menu = {r["name"]: r for r in registry.menu(s)}["acme"]
    assert menu["keysSet"] is True and menu["keys"] == [{"name": "ACME_API_KEY", "label": "API key", "aliases": []}]
    assert "secret-acme" not in json.dumps(registry.menu(s))


def test_a_plugin_that_fails_to_load_is_left_out_and_reported(installed):
    def broken():
        raise ImportError("no module named acme_feed")

    found = installed(("broken", "acme_feed:PLUGIN", broken), ("empty", "empty_mod", lambda: SimpleNamespace()),
                      ("twice", "other:PLUGIN", lambda: Provider(name="tiingo", title="Not Tiingo",
                                                                 build=lambda s, b: None, assets=frozenset())))
    assert list(found) == BUILT_IN  # the app goes on with the others
    assert provider("tiingo").title == "Tiingo"
    why = dict(registry.errors())
    assert "ImportError" in why["broken (acme_feed:PLUGIN)"]
    assert "defines no PLUGIN" in why["empty (empty_mod)"]
    assert any("a second provider named 'tiingo'" in w for w in why.values())


def test_a_module_may_give_several_providers(installed):
    other = Provider(name="acme2", title="Acme Two", build=lambda s, b: None, assets=frozenset({"crypto"}), order=5)
    found = installed(("acme", "acme_feed", lambda: SimpleNamespace(PLUGINS=[ACME, other])))
    assert list(found)[:2] == ["massive", "acme2"] and "acme" in found


def test_unknown_providers_are_refused_by_name():
    with pytest.raises(ValueError, match="unknown source 'nope'; available: massive, alpaca"):
        build_source("nope", load_settings(env={}, env_file=None))


def test_the_plugins_command_lists_keys_set_never_their_values(monkeypatch, capsys):
    monkeypatch.setenv("MASSIVE_API_KEY", "secret-massive")
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    monkeypatch.setattr(config, "read_env_file", lambda path: {})
    assert cli.main(["plugins", "--json"]) == 0
    out = capsys.readouterr().out
    assert "secret-massive" not in out
    rows = {r["name"]: r for r in json.loads(out)["providers"]}
    assert list(rows) == BUILT_IN
    assert rows["massive"]["keysSet"] is True and rows["tiingo"]["keysSet"] is False
    assert rows["bitstamp"]["keysSet"] is None and rows["bitstamp"]["keys"] == []
    assert [k["name"] for k in rows["alpaca"]["keys"]] == ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"]
    assert rows["alpaca"]["keys"][0]["aliases"] == ["APCA_API_KEY_ID"] and rows["alpaca"]["keyless"] == ["crypto"]

    assert cli.main(["plugins"]) == 0
    text = capsys.readouterr().out
    assert "MASSIVE_API_KEY (set)" in text and "TIINGO_API_KEY (not set)" in text and "secret-massive" not in text


def test_the_providers_are_listed_even_with_a_wrong_setting(monkeypatch, capsys):
    monkeypatch.setenv("MASSIVE_TIER", "platinum")
    monkeypatch.setattr(config, "read_env_file", lambda path: {})
    assert cli.main(["plugins", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["providers"]
    assert [r["name"] for r in rows] == BUILT_IN and "keysSet" not in rows[0]
    assert cli.main(["status"]) == 2  # every other command still stops at it


def test_the_fetcher_tabs_carry_each_plugins_key_hints(tmp_path):
    from massive_collate.gui.server import ChartApp
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db") as store:
        app = ChartApp(store, settings=load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None))
        feeds = {f["name"]: f for f in app._feeds()}
    assert feeds["alpaca"]["keys"] is False and feeds["alpaca"]["keysChip"] == "stocks need keys"
    assert "crypto needs no key" in feeds["alpaca"]["keysNote"]
    assert feeds["tiingo"]["keysNote"] == "key not set (TIINGO_API_KEY)" and feeds["tiingo"]["keysChip"] == "no key"
    assert feeds["massive"]["keys"] is True and feeds["coinbase"]["keys"] is None
    assert feeds["cboe"]["archive"] and feeds["coinbase"]["title"] == "Coinbase"


def test_plugin_tiers_keep_their_history_start():
    from massive_collate.collector import history_floor

    tiingo = load_settings(env={}, env_file=None).for_source("tiingo").tier
    assert history_floor(tiingo, date(2026, 10, 27)) == date(1962, 1, 1)
