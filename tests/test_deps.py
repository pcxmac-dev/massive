"""Dependency health checks (docs/DEPENDENCIES.md)."""
from datetime import date

from massive_collate import deps
from massive_collate.config import load_settings


def _by_item(rows):
    return {c.item: c for c in rows}


def test_fresh_install_reports_what_has_never_run(store):
    rows = _by_item(deps.checks(store, load_settings(env={}, env_file=None), "massive"))
    assert rows["API key"].status == "FAIL" and rows["API key"].action
    assert rows["Headlines"].status == "WARN" and "never" in rows["Headlines"].found
    assert rows["KLineChart (charting)"].status == "OK"  # vendored files ship with the app


def test_a_missing_proxy_fund_is_flagged(store):
    from massive_collate.gui.world import PROXIES

    present = [{"T": p, "c": 10.0, "v": 1.0} for p in PROXIES if p != "EWJ"]
    store.save_market_snapshot("massive", date(2026, 10, 21), present)
    rows = _by_item(deps.checks(store, load_settings(env={"MASSIVE_API_KEY": "x"}, env_file=None), "massive"))
    proxy = rows["Proxy funds still trading"]
    assert proxy.status == "WARN" and "EWJ" in proxy.found and proxy.action
