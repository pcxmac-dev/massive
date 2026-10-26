"""A key saved while the app runs (tools/set-key.ps1) counts at once: the chart app reads the user's
saved keys again (ChartApp.settings, every SETTINGS_TTL_S) and the Fetcher shows each feed as it is
now; a sync reads them again before each feed."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from massive_collate import cli, config
from massive_collate.config import load_settings
from massive_collate.gui import activity
from massive_collate.gui.server import ChartApp

NOW = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)


def _tiingo(app) -> dict:
    return next(r for r in activity.health(app.store, app.settings, NOW, [], None) if r["source"] == "tiingo")


def test_the_fetcher_shows_a_key_saved_after_the_app_started(store, monkeypatch):
    saved = {"MASSIVE_API_KEY": "k"}
    monkeypatch.setattr(config, "saved_user_keys", lambda: dict(saved))
    base = {"MASSIVE_API_KEY": "k"}
    app = ChartApp(store, settings=load_settings(env=base, env_file=None),
                   reload_settings=lambda: load_settings(env={**base, **saved}, env_file=None))
    monkeypatch.setattr(app, "SETTINGS_TTL_S", 0.0)
    assert _tiingo(app)["state"] == "off" and "TIINGO_API_KEY" in _tiingo(app)["why"]
    app._feeds_summary = (0.0, ["from the old keys"])
    saved["TIINGO_API_KEY"] = "t" * 40  # saved by the user now
    assert app.settings.tiingo_api_key == "t" * 40 and _tiingo(app)["state"] != "off"
    assert app._feeds_summary is None  # what was worked out from the old keys is worked out again


def test_without_a_reload_the_settings_stay_as_given(store, monkeypatch):
    monkeypatch.setattr(config, "saved_user_keys", lambda: {"TIINGO_API_KEY": "t"})
    app = ChartApp(store, settings=load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None))
    monkeypatch.setattr(app, "SETTINGS_TTL_S", 0.0)
    assert app.settings.tiingo_api_key is None  # a test copy, or settings handed in: never read again


def test_a_sync_reads_saved_keys_again_before_each_feed(tmp_path, monkeypatch):
    settings = load_settings(env={"MASSIVE_API_KEY": "k", "MASSIVE_DB": str(tmp_path / "m.db")}, env_file=None)
    saved = [{}]
    monkeypatch.setattr(config, "saved_user_keys", lambda: dict(saved[0]))
    reloaded = []
    monkeypatch.setattr(cli, "load_settings", lambda: reloaded.append(1) or settings)
    seen = []

    @contextmanager
    def api_run(args, s, what):
        seen.append(args.source)
        if args.source == "massive":
            saved[0] = {"TIINGO_API_KEY": "t"}  # saved while the first feed runs
        yield None, None, print

    monkeypatch.setattr(cli, "api_run", api_run)
    args = SimpleNamespace(all_sources=False, source="massive", tickers=None)
    cli._sync(args, settings)
    assert seen == ["massive"] and not reloaded  # one feed: nothing to read again
    args = SimpleNamespace(all_sources=True, source=None, tickers=None)
    monkeypatch.setattr(cli, "_feed_closed", lambda s, name: name not in ("massive", "tiingo"))
    saved[0] = {}
    cli._sync(args, settings)
    assert seen[1:] == ["massive", "tiingo"] and reloaded == [1]  # read again once, before tiingo
    assert "saved keys or plans changed; read again" in (tmp_path / "logs" / "massive-collate.log").read_text()
