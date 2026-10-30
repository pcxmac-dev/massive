"""Each provider plugin refreshes its own plan info (the user, 2026-10-27): its capability scan may name
the plan its answers show (registry.Scanned), applied wherever the user set none
(config.with_detected); scans run on each chart app start, weekly in the sync and from the Plugins
view (rail icon: a plug). Tiingo's plans cannot be told apart, so TIINGO_PLAN is the user's choice."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from massive_collate import cli, collector, sourcing
from massive_collate.config import Settings, TIERS, load_settings, with_detected
from massive_collate.errors import NotEntitled
from massive_collate.sources import build_source, feed_profiles, providers
from massive_collate.sources.alpaca import AlpacaSource
from massive_collate.sources.registry import Scanned
from massive_collate.storage import Store

KEYS = {"MASSIVE_API_KEY": "k", "ALPACA_API_KEY_ID": "i", "ALPACA_API_SECRET_KEY": "s"}


def _keep(store: Store, name: str, plan: dict, results=(("x", True, ""),)) -> None:
    store.kv_set(sourcing.SCAN_KEY.format(name), json.dumps({"at": "2026-10-27T15:00:00-07:00",
                                                             "results": [list(r) for r in results], "plan": plan}))


def test_a_scan_answers_with_rows_or_with_the_plan_they_show_and_sets_only_its_own_variables():
    alpaca = providers()["alpaca"]
    source = object()
    original = alpaca.scan
    try:
        object.__setattr__(alpaca, "scan", lambda src: [("x", 1, None)])
        found = alpaca.run_scan(source)
        assert found.results[:-1] == (("x", True, "None"),) and found.plan == {}
        assert found.results[-1][0] == "its server's rate limit"  # its limits verified with every scan
        object.__setattr__(alpaca, "scan", lambda src: Scanned((("x", True, ""),),
                                                               {"ALPACA_PLAN": "algo_trader_plus", "MASSIVE_TIER": "advanced"}))
        assert alpaca.run_scan(source).plan == {"ALPACA_PLAN": "algo_trader_plus"}  # never another plugin's
    finally:
        object.__setattr__(alpaca, "scan", original)


def test_a_detected_plan_applies_only_where_the_user_set_none():
    s = load_settings(env=KEYS, env_file=None)
    assert s.alpaca.tier.name == "alpaca-basic" and s.variables == KEYS
    plus = with_detected(s, {"ALPACA_PLAN": "algo_trader_plus"})
    assert plus.alpaca.tier.name == "alpaca-plus" and plus.detected == {"ALPACA_PLAN": "algo_trader_plus"}
    assert plus.variables == KEYS and plus.api_key == "k"  # the rest as it was
    assert with_detected(plus, {"ALPACA_PLAN": "algo_trader_plus"}) is plus  # nothing new: the same settings
    assert with_detected(plus, {}).alpaca.tier.name == "alpaca-basic"  # a later scan found none: back to yours
    mine = load_settings(env={**KEYS, "ALPACA_PLAN": "basic"}, env_file=None)
    assert with_detected(mine, {"ALPACA_PLAN": "algo_trader_plus"}).alpaca.tier.name == "alpaca-basic"  # yours wins
    assert with_detected(s, {"ALPACA_PLAN": "platinum"}) is s  # a value its settings refuse: not applied
    direct = Settings(api_key="k", tier=TIERS["free"], db_path=Path("x.db"))
    assert with_detected(direct, {"MASSIVE_TIER": "advanced"}) is direct  # made directly: nothing to rebuild from


def test_the_plans_found_are_read_from_the_database_by_every_command(tmp_path):
    db = tmp_path / "m.db"
    s = load_settings(env={**KEYS, "MASSIVE_DB": str(db)}, env_file=None)
    assert sourcing.scans_at(db) == {} and sourcing.apply_detected(s) is s  # no database yet
    with Store(db) as store:
        _keep(store, "alpaca", {"ALPACA_PLAN": "algo_trader_plus"})
        _keep(store, "tiingo", {"ALPACA_PLAN": "basic"})  # not Tiingo's variable: ignored
        store.kv_set("capabilities:broken", "{not json")
    assert set(sourcing.scans_at(db)) == {"alpaca", "tiingo"}
    assert sourcing.detected_plans(sourcing.scans_at(db)) == {"ALPACA_PLAN": "algo_trader_plus"}
    applied = sourcing.apply_detected(s)
    assert applied.alpaca.tier.name == "alpaca-plus" and applied.for_source("alpaca").tier.requests_per_minute == 9500
    state = providers()["alpaca"].plan_state(applied)
    assert state == [{"name": "ALPACA_PLAN", "value": "algo_trader_plus", "origin": "scan",
                      "choices": ["basic", "algo_trader_plus"]}]
    assert providers()["alpaca"].plan_state(s)[0] | {} == {"name": "ALPACA_PLAN", "value": "basic", "origin": "default",
                                                           "choices": ["basic", "algo_trader_plus"]}


class _AlpacaClient:
    has_keys = True

    def __init__(self, refuse_recent: bool):
        self.refuse_recent, self.asked = refuse_recent, []

    def get(self, path, params):
        self.asked.append((path, params))
        if self.refuse_recent:
            raise NotEntitled(403, "subscription does not permit querying recent SIP data")
        return {"bars": {}}


def test_alpaca_tells_its_plans_apart_by_the_latest_fifteen_minutes():
    from massive_collate.sources.plugins import alpaca

    now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    for refuse, plan in ((True, "basic"), (False, "algo_trader_plus")):
        source = AlpacaSource(_AlpacaClient(refuse), now=lambda: now)
        assert source.recent_allowed() is (not refuse)
        (path, params), = source.client.asked
        assert path == "/v2/stocks/bars" and params["feed"] == "sip" and params["end"].startswith("2026-10-27T15:00")
        source.is_entitled = lambda *a, **k: True
        found = alpaca.scan(source)
        assert isinstance(found, Scanned) and found.plan == {"ALPACA_PLAN": plan}
    keyless = AlpacaSource(_AlpacaClient(False), now=lambda: now)
    keyless.is_entitled = lambda res, ticker, asset="stock": asset == "crypto"
    assert alpaca.scan(keyless) == [("stock minute bars, every exchange (SIP), from 2016", False, "refused (keys or plan)"),
                                    ("crypto minute bars, no key", True, "")]  # no stocks: nothing tells the plan


def test_massive_finds_its_stocks_plan_and_leaves_the_other_classes_to_you():
    from massive_collate.sources.plugins import massive

    reach = {"day_3y": True, "day_6y": True, "day_11y": False, "day_21y": False, "second": True, "trades": True,
             "quotes": False}
    source = type("Source", (), {"plan_reach": lambda self, ticker: reach})()
    found = providers()["massive"].run_scan(source)
    assert found.plan == {"MASSIVE_TIER": "developer"}
    assert found.results[-2] == ("stocks plan: developer", True, "10 years of history")  # then its limits row
    assert "MASSIVE_TIER_FUTURES" in massive.PLAN_NOTE and "5 requests a minute" in massive.PLAN_NOTE


def test_tiingos_plan_is_your_choice_and_sets_its_limits():
    from massive_collate.sources.plugins import tiingo

    keys = {**KEYS, "TIINGO_API_KEY": "t"}
    starter, power = (load_settings(env={**keys, **extra}, env_file=None) for extra in ({}, {"TIINGO_PLAN": "power"}))
    assert (starter.for_source("tiingo").max_requests_per_day, starter.for_source("tiingo").max_requests_per_run) == (900, 45)
    assert (power.for_source("tiingo").max_requests_per_day, power.for_source("tiingo").max_requests_per_run) == (95_000, 9_500)
    assert providers()["tiingo"].rate_window(starter) == (45, 3600.0)
    assert providers()["tiingo"].rate_window(power) == (9_500, 3600.0)
    assert power.for_source("tiingo").tier.name == "tiingo-power"
    commercial = load_settings(env={**keys, "TIINGO_PLAN": "commercial"}, env_file=None)
    view = commercial.for_source("tiingo")  # a business's: 20,000 an hour, 150,000 a day, kept inside
    assert (view.tier.name, view.max_requests_per_run, view.max_requests_per_day) == ("tiingo-commercial", 19_000, 142_500)
    assert providers()["tiingo"].rate_window(commercial) == (19_000, 3600.0)
    assert collector.monthly_cap(build_source("tiingo", commercial)) is None
    for env, plan in (({"TIINGO_PLAN": "free"}, "starter"), ({"TIINGO_PLAN": "Business"}, "commercial"),
                      ({"TIINGO_PLAN": "gold"}, "starter")):
        assert tiingo.plan_of(load_settings(env={**keys, **env}, env_file=None)) == plan  # unknown: never more than Starter
    assert providers()["tiingo"].plan_choices == {"TIINGO_PLAN": ("starter", "power", "commercial")}
    published = {"starter": (50, 1_000), "power": (10_000, 100_000), "commercial": (20_000, 150_000)}
    for name, (_, per_hour, per_day, _) in tiingo.PLANS.items():  # each a little inside what Tiingo publishes
        assert per_hour <= published[name][0] * 0.95 and per_day <= published[name][1] * 0.95
    assert collector.monthly_cap(build_source("tiingo", starter)) == 450
    assert collector.monthly_cap(build_source("tiingo", power)) is None
    reports = sourcing.reports(feed_profiles(power), date(2026, 10, 27), power)
    assert reports["tiingo"].limits.symbols_a_month is None and reports["tiingo"].plan == "tiingo-power"
    assert sourcing.reports(feed_profiles(starter), date(2026, 10, 27), starter)["tiingo"].limits.symbols_a_month == 450
    assert "no account endpoint, no rate-limit headers" in tiingo.PLAN_NOTE


class _TiingoClient:
    def __init__(self, news):
        self.news, self.paths = news, []

    def get(self, path, params):
        self.paths.append(path)
        if path == "/tiingo/news":
            if isinstance(self.news, Exception):
                raise self.news
            return [{"title": "x"}] if self.news is None else self.news
        if path.startswith(("/boats/", "/tiingo/fundamentals/")):
            raise NotEntitled(403, "not on your plan")
        return [{"close": 1}]


def test_tiingos_scan_tells_starter_from_a_paid_plan_by_the_news_api(store):
    from massive_collate.errors import SourceError
    from massive_collate.sources.plugins import tiingo

    tiingo_p = providers()["tiingo"]
    paid = tiingo_p.run_scan(type("S", (), {"client": _TiingoClient(news=None)})())
    assert paid.plan == {"TIINGO_PLAN": "power"} and paid.alike == {"TIINGO_PLAN": ("commercial",)}
    news = next(r for r in paid.results if r[0] == tiingo.NEWS)
    assert news == (tiingo.NEWS, True, "served: a paid plan, Power or Commercial (the same data)")
    fundamentals = next(r for r in paid.results if r[0] == tiingo.FUNDAMENTALS)
    assert fundamentals[1] is False and "ROKU" in tiingo.FUNDAMENTALS  # outside the Dow 30, free to evaluate
    refused = tiingo_p.run_scan(type("S", (), {"client": _TiingoClient(NotEntitled(403, "no news on Starter"))})())
    assert refused.plan == {"TIINGO_PLAN": "starter"} and refused.alike == {}
    failed = tiingo_p.run_scan(type("S", (), {"client": _TiingoClient(SourceError(500, "server error"))})())
    assert failed.plan == {}  # neither served nor refused: nothing told
    for quiet in ({"detail": "Error: news is for the paid plans"}, []):  # a 200 without articles: never a paid plan
        said = tiingo_p.run_scan(type("S", (), {"client": _TiingoClient(quiet)})())
        assert said.plan == {} and next(r for r in said.results if r[0] == tiingo.NEWS)[1] is False
    client = _TiingoClient(news=None)
    tiingo_p_source = type("S", (), {"client": client})()
    assert client.paths == [] and len(tiingo.probes(date(2026, 10, 27))) == 7

    lines = []
    cli._scan("tiingo", tiingo_p_source, store, lines.append)
    kept = store.kv_json(sourcing.SCAN_KEY.format("tiingo"))
    assert kept["plan"] == {"TIINGO_PLAN": "power"} and kept["alike"] == {"TIINGO_PLAN": ["commercial"]}
    assert any("plan found: TIINGO_PLAN=power (or commercial: the same answers)" in l for l in lines)


def test_a_paid_tiingo_key_is_power_unless_you_say_commercial(tmp_path):
    from massive_collate.gui import plugins
    from massive_collate.sources.plugins import tiingo

    scans = {"tiingo": {"at": "x", "results": [], "plan": {"TIINGO_PLAN": "power"},
                        "alike": {"TIINGO_PLAN": ["commercial"]}}}
    keys = {**KEYS, "TIINGO_API_KEY": "t"}
    found = with_detected(load_settings(env=keys, env_file=None), sourcing.detected_plans(scans))
    assert tiingo.plan_of(found) == "power" and found.for_source("tiingo").max_requests_per_day == 95_000
    with Store(tmp_path / "m.db") as store:
        for env, differs in (({"TIINGO_PLAN": "commercial"}, []), ({"TIINGO_PLAN": "starter"}, ["starter"])):
            s = load_settings(env={**keys, **env}, env_file=None)
            feeds = feed_profiles(s)
            view = plugins.view(store, s, feeds, sourcing.standing(store, feeds), scans)
            t = next(p for p in view["plugins"] if p["name"] == "tiingo")
            assert [d["yours"] for d in t["plan"]["differs"]] == differs  # commercial: the same answers as power
            assert t["plan"]["alike"] == {"TIINGO_PLAN": ["commercial"]}
    assert "cannot tell Power from Commercial" in tiingo.PLAN_NOTE


def test_a_scan_keeps_the_plan_it_found(store):
    alpaca = providers()["alpaca"]
    original = alpaca.scan
    lines = []
    try:
        object.__setattr__(alpaca, "scan", lambda src: Scanned((("x", True, ""),), {"ALPACA_PLAN": "basic"}))
        cli._scan("alpaca", object(), store, lines.append)
    finally:
        object.__setattr__(alpaca, "scan", original)
    kept = store.kv_json(sourcing.SCAN_KEY.format("alpaca"))
    assert kept["plan"] == {"ALPACA_PLAN": "basic"} and kept["results"][:-1] == [["x", True, ""]]
    assert kept["limits"] == {"seen": {}, "perMinute": None, "kept": None}  # no settings given: no window to set beside
    assert "alpaca: plan found: ALPACA_PLAN=basic (used wherever you have not set ALPACA_PLAN)" in lines


def test_capabilities_skips_a_provider_whose_keys_are_not_set(tmp_path, capsys):
    s = load_settings(env={"MASSIVE_DB": str(tmp_path / "m.db"), "MASSIVE_OFFLINE": "1"}, env_file=None)
    args = cli.build_parser().parse_args(["capabilities", "tiingo"])
    assert cli.cmd_capabilities(args, s) == 0
    assert "tiingo: not set up here (TIINGO_API_KEY); not scanned" in capsys.readouterr().out


def test_a_scan_runs_beside_a_sync_holding_the_run_lock(tmp_path, monkeypatch):
    s = load_settings(env={**KEYS, "MASSIVE_DB": str(tmp_path / "m.db")}, env_file=None)
    taken = []
    monkeypatch.setattr(cli, "run_lock", lambda path: taken.append(path) or (_ for _ in ()).throw(SystemExit("busy")))
    monkeypatch.setattr(cli, "build_source", lambda name, settings, request_budget=None: type(
        "Src", (), {"requests_made": 0, "client": None})())
    args = type("Args", (), {"source": "alpaca"})()
    with cli.api_run(args, s, "capabilities", lock=False) as (source, store, log):
        assert source is not None
    assert taken == []


def _app(tmp_path, offline=False):
    from massive_collate.gui.server import ChartApp

    tmp_path.mkdir(parents=True, exist_ok=True)
    env = {**KEYS, "MASSIVE_DB": str(tmp_path / "m.db"), **({"MASSIVE_OFFLINE": "1"} if offline else {})}
    store = Store(tmp_path / "m.db", check_same_thread=False)
    return store, ChartApp(store, settings=load_settings(env=env, env_file=None),
                           reload_settings=lambda: load_settings(env=env, env_file=None))


class _Proc:
    pid = 4242

    def __init__(self):
        self.done = False

    def poll(self):
        return 0 if self.done else None


def test_the_chart_app_rescans_in_a_process_of_its_own_one_at_a_time(tmp_path):
    store, app = _app(tmp_path)
    spawned = []
    app._spawn = lambda args: spawned.append(args) or _Proc()
    try:
        out = app.start_rescan("tiingo")
        assert out["started"] and spawned[0][-3:] == ["massive_collate", "capabilities", "tiingo"]
        with pytest.raises(RuntimeError, match="already running"):
            app.start_rescan()
        assert app.plugins()["running"]["name"] == "tiingo"
        assert next(p for p in app.plugins()["plugins"] if p["name"] == "tiingo")["scan"]["running"]
        app._rescan[0].done = True
        app.start_rescan()
        assert spawned[1][-1] == "capabilities"  # every plugin set up here
        with pytest.raises(LookupError):
            app.start_rescan("bitstamp")  # a public feed with one fixed plan: no scan
    finally:
        store.close()
    store, offline = _app(tmp_path / "o", offline=True)
    try:
        with pytest.raises(RuntimeError, match="offline"):
            offline.start_rescan()
    finally:
        store.close()


def test_the_chart_app_takes_up_a_plan_a_scan_found(tmp_path):
    store, app = _app(tmp_path)
    try:
        assert app.settings.alpaca.tier.name == "alpaca-basic"
        _keep(store, "alpaca", {"ALPACA_PLAN": "algo_trader_plus"})
        app._settings_at = 0.0  # SETTINGS_TTL_S passed
        assert app.settings.alpaca.tier.name == "alpaca-plus"
        alpaca = next(p for p in app.plugins()["plugins"] if p["name"] == "alpaca")
        assert alpaca["plan"]["vars"][0] == {"name": "ALPACA_PLAN", "value": "algo_trader_plus", "origin": "scan",
                                             "choices": ["basic", "algo_trader_plus"]}
        assert alpaca["plan"]["found"] == {"ALPACA_PLAN": "algo_trader_plus"} and alpaca["scan"]["at"]
    finally:
        store.close()


def test_the_plugins_view_shows_each_plugin_its_plan_keys_and_part(tmp_path):
    from massive_collate.symbols import resolve

    store, app = _app(tmp_path)
    try:
        store.watch_spec("alpaca", resolve("AAPL::XNAS"))
        out = app.plugins()
        by = {p["name"]: p for p in out["plugins"]}
        assert list(by) == list(providers()) and out["offline"] is False and out["errors"] == []
        assert by["massive"]["group"] == "Feeds with keys" and by["bitstamp"]["group"] == "Public feeds"
        assert by["alpaca"]["keys"] == [{"name": "ALPACA_API_KEY_ID", "label": "key ID", "set": True},
                                        {"name": "ALPACA_API_SECRET_KEY", "label": "secret key", "set": True}]
        assert all(set(k) == {"name", "label", "set"} for p in by.values() for k in p["keys"])  # never a key's value
        assert by["tiingo"]["state"] == "no key" and by["tiingo"]["plan"]["vars"][0]["value"] == "starter"
        assert by["tiingo"]["plan"]["vars"][0]["choices"] == ["starter", "power", "commercial"]
        assert by["tiingo"]["plan"]["note"].startswith("Tiingo's API does not say")
        assert by["alpaca"]["watched"] == 1 and by["alpaca"]["limits"]["rate"] == [190, 60.0]
        assert {e["job"] for e in by["alpaca"]["delivery"]} >= {"minute"} and by["alpaca"]["load"] >= 0
        assert by["bitstamp"]["scan"]["has"] is False and by["massive"]["scan"]["has"] is True
    finally:
        store.close()


def test_a_plan_you_set_that_the_scan_disagrees_with_is_shown(tmp_path):
    from massive_collate.gui import plugins
    from massive_collate.sources import feed_profiles

    s = load_settings(env={**KEYS, "ALPACA_PLAN": "algo_trader_plus"}, env_file=None)
    with Store(tmp_path / "m.db") as store:
        feeds = feed_profiles(s)
        view = plugins.view(store, s, feeds, sourcing.standing(store, feeds),
                            {"alpaca": {"at": "x", "results": [], "plan": {"ALPACA_PLAN": "basic"}}})
    alpaca = next(p for p in view["plugins"] if p["name"] == "alpaca")
    assert alpaca["plan"]["differs"] == [{"name": "ALPACA_PLAN", "yours": "algo_trader_plus", "found": "basic"}]
    assert alpaca["plan"]["vars"][0]["origin"] == "you"


def test_the_page_has_its_plug_and_view():
    static = Path(__file__).resolve().parents[1] / "src" / "massive_collate" / "gui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="railPlugins"' in html and 'id="viewPlugins"' in html and 'src="plugins.js"' in html
    assert "plug:" in (static / "icons.js").read_text(encoding="utf-8")
    panels = (static / "panels.js").read_text(encoding="utf-8")
    assert "plugins: 'plugins'" in panels and "icon('plug', RAIL_ICON)" in panels
    js = (static / "plugins.js").read_text(encoding="utf-8")
    assert "/api/plugins/rescan" in js and "'X-Massive-Charts': '1'" in js
    css = (static / "style.css").read_text(encoding="utf-8")
    # its grid (and the Exchanges view's) only while shown: a display with !important outranked
    # `main > .view[hidden]` and showed the Exchanges view beside every other one
    assert "main > .view.ex-view:not([hidden]) { display: grid; }" in css and "display: grid !important" not in css


def test_the_key_script_offers_each_plan_and_a_rescan():
    src = (Path(__file__).resolve().parents[1] / "tools" / "set-key.ps1").read_text(encoding="utf-8")
    assert "function Set-Plan" in src and "-m massive_collate capabilities $p.name" in src
    assert "[switch]$Rescan" in src and "$choices -notcontains $pick" in src


class _Registry:
    """A stand-in for winreg: the user Environment key as a dict."""
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        assert (root, path, access) == ("HKCU", "Environment", 2)
        return self._Key()

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_a_chosen_plan_is_saved_where_the_key_script_saves_it():
    import os

    from massive_collate.config import save_user_variable

    reg = _Registry()
    save_user_variable("TIINGO_PLAN", "commercial", registry=reg)
    assert reg.values == {"TIINGO_PLAN": "commercial"} and os.environ["TIINGO_PLAN"] == "commercial"  # this process too
    save_user_variable("TIINGO_PLAN", None, registry=reg)
    assert reg.values == {} and "TIINGO_PLAN" not in os.environ
    save_user_variable("TIINGO_PLAN", None, registry=reg)  # removing one not set: nothing to do


def _choosing_app(tmp_path, monkeypatch, offline=False):
    from massive_collate import config
    from massive_collate.gui.server import ChartApp

    env = {**KEYS, "TIINGO_API_KEY": "t", "MASSIVE_DB": str(tmp_path / "m.db"), **({"MASSIVE_OFFLINE": "1"} if offline else {})}
    saved: dict[str, str] = {}
    monkeypatch.setattr(config, "saved_user_keys", lambda: dict(saved))
    store = Store(tmp_path / "m.db", check_same_thread=False)
    app = ChartApp(store, settings=load_settings(env=env, env_file=None),
                   reload_settings=lambda: load_settings(env={**env, **saved}, env_file=None))
    wrote = []

    def save(name, value):
        wrote.append((name, value))
        saved.pop(name, None) if value is None else saved.__setitem__(name, value)

    app._save_variable = save  # never the real registry in a test
    return store, app, wrote


def test_the_plugins_view_chooses_a_plan_the_scan_cannot_tell(tmp_path, monkeypatch):
    from massive_collate.sources.plugins import tiingo

    store, app, wrote = _choosing_app(tmp_path, monkeypatch)
    try:
        assert tiingo.plan_of(app.settings) == "starter"
        assert app.set_plan("tiingo", "TIINGO_PLAN", " Commercial ") == {"plugin": "tiingo", "variable": "TIINGO_PLAN",
                                                                         "value": "commercial"}
        assert wrote == [("TIINGO_PLAN", "commercial")]
        assert tiingo.plan_of(app.settings) == "commercial"  # at the app's next look, not after SETTINGS_TTL_S
        assert app.settings.for_source("tiingo").max_requests_per_day == 142_500
        t = next(p for p in app.plugins()["plugins"] if p["name"] == "tiingo")
        assert t["plan"]["vars"][0] | {} == {"name": "TIINGO_PLAN", "value": "commercial", "origin": "you",
                                             "choices": ["starter", "power", "commercial"]}
        app.set_plan("tiingo", "TIINGO_PLAN", None)  # automatic: the scan or the default again
        assert wrote[-1] == ("TIINGO_PLAN", None) and tiingo.plan_of(app.settings) == "starter"
        with pytest.raises(ValueError):
            app.set_plan("tiingo", "TIINGO_PLAN", "gold")
        for plugin, variable in (("tiingo", "TIINGO_API_KEY"), ("bitstamp", "TIINGO_PLAN"), ("nowhere", "X"),
                                 ("massive", "MASSIVE_MAX_REQUESTS_PER_DAY")):
            with pytest.raises(LookupError):
                app.set_plan(plugin, variable, "x")  # never a key, nor a variable without choices
        assert len(wrote) == 2
    finally:
        store.close()


def test_an_offline_copy_saves_no_plan(tmp_path, monkeypatch):
    store, app, wrote = _choosing_app(tmp_path, monkeypatch, offline=True)
    try:
        with pytest.raises(RuntimeError, match="saves no settings"):
            app.set_plan("tiingo", "TIINGO_PLAN", "commercial")  # it shares your real user variables
        assert wrote == []
    finally:
        store.close()


def test_the_plan_route_takes_only_this_apps_pages(tmp_path, monkeypatch):
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer

    from massive_collate.gui.server import make_handler

    store, app, wrote = _choosing_app(tmp_path, monkeypatch)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(body, header=True):
        c = HTTPConnection("127.0.0.1", port)
        headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **({"X-Massive-Charts": "1"} if header else {})}
        c.request("POST", "/api/plugins/plan", body=json.dumps(body), headers=headers)
        r = c.getresponse()
        raw = r.read()
        return r.status, json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw

    try:
        assert post({"plugin": "tiingo", "variable": "TIINGO_PLAN", "value": "power"}, header=False)[0] == 403
        assert wrote == []
        status, out = post({"plugin": "tiingo", "variable": "TIINGO_PLAN", "value": "power"})
        assert status == 200 and out["value"] == "power" and wrote == [("TIINGO_PLAN", "power")]
        assert post({"plugin": "tiingo", "variable": "TIINGO_PLAN", "value": None})[1]["value"] is None
        assert post({"plugin": "tiingo", "variable": "TIINGO_PLAN", "value": "gold"})[0] == 400
        assert post({"plugin": "tiingo", "variable": "TIINGO_API_KEY", "value": "k"})[0] == 404
    finally:
        httpd.shutdown()
        store.close()


def test_the_page_offers_each_plan_as_a_choice():
    static = Path(__file__).resolve().parents[1] / "src" / "massive_collate" / "gui" / "static"
    js = (static / "plugins.js").read_text(encoding="utf-8")
    assert "/api/plugins/plan" in js and "select.pl-choose" in js and "window.confirm(ask)" in js
    assert "This offline copy of the app saves no settings" in js and "headers: WRITE_HEADERS" in js
