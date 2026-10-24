"""Small shared pieces from the appraisal: JSON in the kv table read one way and changed in one
transaction (the chart app, the sync and the recorder write the same keys), the Fetcher's state
order the same on page and server, and the initializer's order passed in rather than hidden."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from massive_collate import collector
from massive_collate.gui import activity
from massive_collate.storage import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.mark.parametrize("raw", [None, "", "{not json", "[1, 2]"])
def test_a_kv_value_that_is_missing_or_not_the_kind_kept_reads_as_the_default(store, raw):
    if raw is not None:
        store.kv_set("k", raw)
    assert store.kv_json("k", {}) == {}


def test_a_kv_value_reads_back_as_written(store):
    store.kv_set("k", '{"a": 1}')
    assert store.kv_json("k", {}) == {"a": 1} and store.kv_json("k") == {"a": 1}
    assert store.kv_json("missing") is None


def test_changes_from_two_connections_at_once_are_all_kept(tmp_path):
    Store(tmp_path / "m.db").close()  # the schema, once

    def bump(n: int) -> None:
        with Store(tmp_path / "m.db", check_same_thread=False) as s:
            for _ in range(n):
                s.kv_update("count", lambda v: {"n": v.get("n", 0) + 1}, {})

    threads = [threading.Thread(target=bump, args=(40,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with Store(tmp_path / "m.db") as s:
        assert s.kv_json("count", {}) == {"n": 120}  # a plain read-then-write loses some


def test_a_monthly_symbol_place_is_taken_once(store, monkeypatch):
    monkeypatch.setitem(collector.SYMBOLS_PER_MONTH, "tiingo", 2)
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert [collector._monthly_symbol(store, "tiingo", t, now) for t in ("A", "B", "A", "C")] == [True, True, True, False]
    assert store.kv_json("symbols:tiingo:2026-10", []) == ["A", "B"]


def test_a_pause_that_cannot_be_read_is_no_pause(store):
    for raw in ("", "[]", '{"at": "x"}', '{"until": "not a time"}'):
        store.kv_set(collector.PAUSE_KEY, raw)
        assert collector.pause_state(store) is None
    collector.set_pause(store, True, minutes=5)
    assert collector.pause_state(store)["until"]


def test_priority_requests_keep_each_other(store):
    collector.request_priority(store, "aapl")
    collector.request_priority(store, "MSFT")
    collector.priority_started(store, "AAPL")
    asked = collector.priority_requests(store)
    assert set(asked) == {"AAPL", "MSFT"} and asked["AAPL"]["runs"] == 1


def test_the_fetcher_page_orders_states_as_the_server_does():
    js = (Path(activity.__file__).parent / "static" / "activity.js").read_text(encoding="utf-8")
    body = re.search(r"const STATE_ORDER = \{([^}]*)\}", js).group(1)
    page = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", body)}
    assert page == activity.STATE_RANK


def test_rows_rank_by_state_then_the_order_given():
    rows = [{"ticker": t, "state": s} for t, s in (("A", "current"), ("B", "current"), ("C", "queued"))]
    assert activity.ranked(rows, {"B": 0, "A": 1}) == [2, 1, 0]
    assert activity.ranked(rows) == [2, 0, 1]  # no order: by ticker
