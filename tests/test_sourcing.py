"""The delivery scheme (sourcing.py): each provider reports its subscription, limits and capabilities
in one standard form, and the scheme gives each job's eras to sources by uniqueness, cost and
capability, in the user's order (daily bars, then 1-minute, then 1-second); the fetcher follows it."""
from __future__ import annotations

from datetime import date, datetime

from massive_collate import sourcing
from massive_collate.config import TIERS, load_settings
from massive_collate.coverage import FeedProfile, day_start_ms, plan
from massive_collate.sessions import ET
from massive_collate.sources import capacity, feed_profiles, providers, registry
from massive_collate.sources.registry import Capability, Limits, Provider, Report

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=ET)
KEYS = {"MASSIVE_API_KEY": "k", "MASSIVE_TIER": "advanced", "ALPACA_API_KEY_ID": "i",
        "ALPACA_API_SECRET_KEY": "s", "TIINGO_API_KEY": "t"}


def _settings(**extra):
    return load_settings(env={**KEYS, **extra}, env_file=None)


def test_every_plugin_reports_in_the_same_standard_form():
    s = _settings()
    feeds = feed_profiles(s)
    reports = sourcing.reports(feeds, TODAY, s)
    assert list(reports) == list(feeds)
    shape = None
    for name, r in reports.items():
        assert isinstance(r, Report) and isinstance(r.limits, Limits)
        assert all(isinstance(c, Capability) for c in r.capabilities) and r.capabilities
        d = r.as_dict()
        keys = (set(d), set(d["limits"]), {k for c in d["capabilities"] for k in c})
        assert shape is None or keys == shape  # one form for every provider
        shape = keys
        assert r.lines()[0].startswith(f"{name} ({r.title}): plan {r.plan}")
    tiingo = reports["tiingo"]
    assert [(c.asset, c.resolution, c.per_request, str(c.since)) for c in tiingo.capabilities] == [
        ("stock", "day", None, "1962-01-01")]  # its whole history in one request
    assert tiingo.limits.symbols_a_month == 450 and tiingo.limits.requests_a_day == 900  # 45 an hour is 1,080
    assert {c.resolution for c in reports["alpaca"].capabilities} == {"day", "minute"}  # no 1-second bars
    assert reports["alpaca"].streams == (("stock", providers()["alpaca"].streams["stock"]),)
    massive = {(c.asset, c.resolution) for c in reports["massive"].capabilities}
    assert ("stock", "second") in massive and ("index", "second") not in massive  # indices: the free plan


def test_a_plugin_that_declares_nothing_reports_the_defaults_and_can_amend_its_report():
    from dataclasses import replace

    from massive_collate.config import Tier

    tier = Tier("acme-pro", requests_per_minute=60, history_years=5)
    plain = Provider(name="acme", title="Acme", build=lambda s, b: None, assets=frozenset({"stock", "fx"}))
    profile = FeedProfile("acme", tier, frozenset({"stock"}), 9, venues={"stock": "sip"}, capacity=5000.0)
    r = plain.report(profile, TODAY)
    assert [(c.asset, c.resolution, c.market, c.per_request) for c in r.capabilities] == [
        ("stock", "day", "sip", 1000), ("stock", "minute", "sip", 1000)]  # day and minute, 1,000 bars a request
    assert r.limits.requests_a_day == 5000.0 and r.plan == "acme-pro" and r.keys_set is None
    noted = replace(plain, reporter=lambda report, settings: replace(report, notes=("trial plan: ends 1 Dec",)))
    assert noted.report(profile, TODAY).notes == ("trial plan: ends 1 Dec",)
    assert "    note: trial plan: ends 1 Dec" in noted.report(profile, TODAY).lines()


def test_a_subscription_capacity_is_its_daily_cap_or_its_rate():
    assert capacity((45, 3600.0), 900) == 900.0
    assert capacity((5, 62.0), 2000) == 2000.0
    assert capacity((1, 60.0), 2000) == 1440.0  # the rate allows fewer than the cap
    assert capacity((None, 60.0), 200_000) == 200_000.0
    free = feed_profiles(load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None))["massive"]
    assert "second" not in {c.resolution for c in providers()["massive"].report(free, TODAY).capabilities}


def test_eras_are_unique_or_common_and_go_to_who_finishes_soonest():
    feeds = feed_profiles(_settings())
    counts = {("massive", "stock"): 100, ("alpaca", "stock"): 100, ("tiingo", "stock"): 100}
    scheme = sourcing.build(feeds, counts, TODAY)
    days = [e for e in scheme.eras if e.asset == "stock" and e.resolution == "day"]
    assert [(str(e.start), str(e.end) if e.end else None, e.unique) for e in days] == [
        ("1962-01-01", "2003-09-10", True), ("2003-09-10", "2016-01-01", False), ("2016-01-01", None, False)]
    assert days[0].sources == ["tiingo"]  # only Tiingo reaches before 2003: it must deliver it
    assert days[1].sources[0] == "massive"  # Tiingo's 450 symbols a month are kept for what only it has
    # Massive at its default 2,000 requests a day: with the daily bars to 2016 on it, Alpaca's idle
    # limits finish those since 2016 sooner; and the minutes since 2016, with Massive's unique ones
    # before 2016 on it. The 1-second bars (job #3) push nothing of the jobs before them.
    assert days[2].sources[0] == "alpaca"
    minutes = [e for e in scheme.eras if e.asset == "stock" and e.resolution == "minute"]
    assert [(e.unique, e.sources[0]) for e in minutes] == [(True, "massive"), (False, "alpaca")]
    seconds = [e for e in scheme.eras if e.resolution == "second"]
    assert [(e.unique, e.sources) for e in seconds] == [(True, ["massive"])]
    assert [e.resolution for e in scheme.eras][:1] == ["day"] and scheme.eras[-1].resolution == "second"
    assert scheme.load["tiingo"] == 100 / 15  # 450 symbols a month: 15 a day

    # The same at Massive's raised cap (200,000 a day, this machine's): 50,000 bars a request make it
    # the cheapest for all the common eras; Alpaca stands by (it takes over where Massive found nothing)
    big = sourcing.build(feed_profiles(_settings(MASSIVE_MAX_REQUESTS_PER_DAY="200000")), counts, TODAY)
    assert [(e.resolution, e.sources[0]) for e in big.eras if e.asset == "stock"] == [
        ("day", "tiingo"), ("day", "massive"), ("day", "massive"), ("minute", "massive"), ("minute", "massive"),
        ("second", "massive")]


def test_cheaper_capacity_takes_the_common_work_when_nobody_is_loaded():
    feeds = {"a": FeedProfile("a", TIERS["advanced"], frozenset({"stock"}), 0, venues={"stock": "sip"}, capacity=1000.0),
             "b": FeedProfile("b", TIERS["advanced"], frozenset({"stock"}), 1, venues={"stock": "sip"}, capacity=10.0)}
    plugin = lambda name: Provider(name=name, title=name.upper(), build=lambda s, b: None,  # noqa: E731
                                   assets=frozenset({"stock"}), provides={"stock": ("day",)}, per_request={"day": 100})
    original = registry._providers
    registry._providers = {**(original or registry.providers()), "a": plugin("a"), "b": plugin("b")}
    try:
        scheme = sourcing.build(feeds, {("a", "stock"): 10, ("b", "stock"): 10}, TODAY)
        refused = sourcing.build(feeds, {}, TODAY, entitled=lambda s, a, r: False if s == "a" else None)
    finally:
        registry._providers = original
    (era,) = scheme.eras
    assert not era.unique and era.sources == ["a", "b"] and era.days["a"] < era.days["b"]
    (only,) = refused.eras  # a plan probe that refused it takes the offer away
    assert only.unique and only.sources == ["b"]


def test_the_report_lists_the_jobs_in_the_users_order():
    s = _settings()
    feeds = feed_profiles(s)
    scheme = sourcing.build(feeds, {("massive", "stock"): 5}, TODAY)
    text = sourcing.report(scheme, sourcing.reports(feeds, TODAY, s))
    assert text.index("#1 daily bars") < text.index("#2 1-minute bars") < text.index("#3 1-second bars")
    assert "tiingo only (unique)" in text and "(common)" in text and "load:" in text
    assert "Current session" in text and "stream: real time" in text
    d = scheme.as_dict()
    assert d["jobs"] == ["day", "minute", "second"] and d["eras"][0]["resolution"] == "day"


def test_the_fetcher_follows_the_scheme_and_takes_back_what_the_other_found_empty(store):
    feeds = feed_profiles(_settings())
    for feed in ("massive", "alpaca"):
        store.watch(feed, ["AAPL"], "minute")
    # the scheme: Alpaca delivers the daily bars since 2016, Massive those before (Tiingo before 2003)
    eras = sourcing.standing(store, feeds, NOW).order("stock", "day", "sip")
    assert [o[0] for *_, o in eras] == ["tiingo", "massive", "alpaca"]
    cut = day_start_ms(date(2016, 1, 1))
    mine = plan(store, feeds["massive"], feeds, "AAPL", "stock", "day", date(2003, 9, 10), NOW)
    assert mine and max(b for _, b in mine) <= cut  # Massive leaves 2016 on to Alpaca
    theirs = plan(store, feeds["alpaca"], feeds, "AAPL", "stock", "day", date(2016, 1, 1), NOW)
    assert theirs and min(a for a, _ in theirs) == cut
    # where Alpaca was asked and has nothing, Massive takes it back
    store.add_coverage("alpaca", "AAPL", "day", cut, day_start_ms(date(2017, 1, 1)), origin="none")
    mine = plan(store, feeds["massive"], feeds, "AAPL", "stock", "day", date(2003, 9, 10), NOW)
    assert any(a < day_start_ms(date(2017, 1, 1)) and b > cut for a, b in mine)
