"""The updates pause (collector.updates_pause): for its days, a symbol the user has not picked (opened
in the chart, or on one of their watchlists) gets no new-data updates unless it is more than
UPDATES_GAP_DAYS behind; history goes on and the picked symbols are updated as usual. And a symbol
opened in the chart goes first: the daily-first sweep stops for it, the Fetcher lists it as next."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from massive_collate import collector
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings
from massive_collate.gui import activity
from massive_collate.live import WATCHLISTS_KEY

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def _caught_up(store, tickers, synced_ago: timedelta):
    """Symbols whose history is in, last synced `synced_ago`."""
    for t in tickers:
        store.watch("fake", [t], "minute")
        store.set_backfilled("fake", t, date(2020, 1, 1))
        store.set_synced("fake", t, NOW - synced_ago)
        store.add_coverage("fake", t, "day", _ms(date(2020, 1, 1)), _ms(NOW.date()))


def test_picked_symbols_are_the_ones_opened_and_on_the_watchlists(store):
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["msft", "NVDA"]}]))
    collector.request_priority(store, "AAPL", NOW)
    assert collector.picked_symbols(store, NOW) == {"MSFT", "NVDA", "AAPL"}


def test_while_paused_only_picked_symbols_and_ones_far_behind_are_updated(store):
    _caught_up(store, ["PICK", "IDLE"], timedelta(days=2))
    _caught_up(store, ["STALE"], timedelta(days=9))  # missing a substantial amount: still updated
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["PICK"]}]))
    assert collector.set_updates_pause(store, 7, NOW)["until"].startswith("2026-10-28")
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    asked = {c[0] for c in getattr(src, "bar_calls", [])}
    assert "PICK" in asked and "STALE" in asked and "IDLE" not in asked
    assert any("1 symbols' updates paused until 2026-10-28" in m for m in logged)
    collector.set_updates_pause(store, None, NOW)  # resumed: IDLE is updated again
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    sync(src, store, FREE, NOW, log=lambda *_: None)
    assert "IDLE" in {c[0] for c in getattr(src, "bar_calls", [])}


def test_a_pause_ends_on_its_own(store):
    collector.set_updates_pause(store, 7, NOW)
    assert collector.updates_pause(store, NOW + timedelta(days=6)) is not None
    assert collector.updates_pause(store, NOW + timedelta(days=7, seconds=1)) is None


def test_the_daily_first_sweep_stops_for_a_symbol_opened_in_the_chart(store):
    store.watch("fake", ["AAA", "BBB", "OPEN"], "minute")
    store.set_synced("fake", "OPEN", NOW - timedelta(hours=1))
    store.kv_set("watchlist:order", json.dumps({"AAA": 0, "BBB": 1, "OPEN": 2}))
    collector.request_priority(store, "OPEN", NOW - timedelta(minutes=5))  # opened after its last sync
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    calls = [(c[0], c[3]) for c in src.bar_calls]
    assert calls[0][0] == "OPEN"  # before the sweep's first stock
    assert any("stopped for OPEN (opened in the chart; goes on after it)" in m for m in logged)
    # and once it is served, the run goes back to daily bars first before its own backfill
    after = calls[next(i for i, c in enumerate(calls) if c[0] != "OPEN"):]
    assert after[:2] == [("AAA", "day"), ("BBB", "day")]


def test_the_fetcher_lists_an_opened_symbol_as_next_and_says_why_others_wait(store):
    _caught_up(store, ["OPEN", "IDLE"], timedelta(days=2))
    collector.request_priority(store, "OPEN", NOW.astimezone(timezone.utc))
    collector.set_updates_pause(store, 7, NOW)
    settings = load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None)
    rows = {r["ticker"]: r for r in activity.symbol_rows(store, settings, "fake", NOW)}
    assert rows["OPEN"]["state"] == "next" and "opened in the chart" in rows["OPEN"]["note"]
    assert "updates paused until 2026-10-28" in rows["IDLE"].get("note", "")
    order = [r["ticker"] for r in (list(rows.values())[i] for i in activity.ranked(list(rows.values())))]
    assert order[0] == "OPEN"


def test_the_updates_command_pauses_and_resumes(tmp_path, monkeypatch, capsys):
    from massive_collate import cli

    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    assert cli.main(["updates", "pause", "--days", "7"]) == 0
    assert "updates paused until" in capsys.readouterr().out
    assert cli.main(["updates", "resume"]) == 0
    assert "not paused" in capsys.readouterr().out


def test_an_opened_symbol_found_up_to_date_has_its_request_met(store):
    # the run takes it, finds nothing new published since its last sync: the check is recorded,
    # so it is not "next" for ever (nor stops the next run's daily sweep)
    store.watch("fake", ["OPEN"], "minute")
    store.set_backfilled("fake", "OPEN", date(2020, 1, 1))
    store.set_synced("fake", "OPEN", NOW - timedelta(hours=1))
    store.add_coverage("fake", "OPEN", "day", _ms(date(2020, 1, 1)), _ms(NOW.date()))
    collector.request_priority(store, "OPEN", NOW - timedelta(minutes=5))
    assert collector._unserved_request(store, "fake", NOW) == "OPEN"
    sync(FakeSource(entitled=("minute",), bars={"day": [], "minute": []}), store, FREE, NOW, log=lambda *_: None)
    assert collector._unserved_request(store, "fake", NOW) is None
