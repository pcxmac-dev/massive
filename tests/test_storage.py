from __future__ import annotations

from datetime import date

from massive_collate.storage import Store


def _trade(ns: int, seq: int = 1, price: float = 10.0) -> dict:
    return {"sip_timestamp": ns, "sequence_number": seq, "price": price, "size": 5, "exchange": 4,
            "conditions": [12, 37]}


def test_trades_upsert_is_idempotent(store):
    store.upsert_trades("s", "spcx", [_trade(1_000), _trade(2_000)])
    store.upsert_trades("s", "SPCX", [_trade(2_000, price=11.0)])
    df = store.load_trades("SPCX", "s")
    assert len(df) == 2 and df["price"].tolist() == [10.0, 11.0]
    assert df["conditions"].iloc[0] == "12,37"
    assert store.trade_range("s", "SPCX") == (1_000, 2_000)


def test_tick_timestamps_keep_nanoseconds(store):
    ns = 1_792_503_000_123_456_789
    store.upsert_trades("s", "X", [_trade(ns)])
    t = store.load_trades("X", "s")["time"].iloc[0]
    assert t.as_unit("ns").value == ns


def test_sources_are_kept_apart(store):
    store.upsert_trades("a", "X", [_trade(1)])
    store.upsert_trades("b", "X", [_trade(1)])
    assert len(store.load_trades("X")) == 2
    assert len(store.load_trades("X", "a")) == 1


def test_bars_upsert_and_range(store):
    bars = [{"t": 1_700_000_000_000 + i * 60_000, "o": 1, "h": 2, "l": 0, "c": 1, "v": 3} for i in range(3)]
    assert store.upsert_bars("s", "X", "minute", 1, bars) == 3
    store.upsert_bars("s", "X", "minute", 1, bars[:1])
    assert store.bar_range("s", "X", "minute") == (bars[0]["t"], bars[-1]["t"])
    assert len(store.load_bars(["X"], "minute", source="s")) == 3
    assert store.bar_range("s", "X", "second") is None


def test_changing_resolution_restarts_backfill(store):
    store.watch("s", ["X"], "trades")
    store.set_backfilled("s", "X", date(2024, 1, 1))
    store.watch("s", ["X"], "trades")
    assert store.watchlist("s")[0]["backfilled_to"] == date(2024, 1, 1)
    store.watch("s", ["X"], "minute")
    assert store.watchlist("s")[0]["backfilled_to"] is None
    store.unwatch("s", ["x"])
    assert store.watchlist("s") == []


def test_run_log_counts_requests_per_source(store):
    a = store.start_run("a", "sync")
    store.finish_run(a, requests=7, rows=100, status="ok")
    b = store.start_run("b", "sync")
    store.finish_run(b, requests=3, rows=0, status="ok")
    assert store.requests_today("a") == 7 and store.requests_today("b") == 3
    assert store.recent_runs(1)[0][1] == "b"


def test_interrupted_run_still_counts_its_requests(store):
    run = store.start_run("a", "sync")
    store.set_run_requests(run, 42)  # recorded per request; then the process dies
    assert store.requests_today("a") == 42
    assert store.close_abandoned_runs() == 1
    assert store.recent_runs(1)[0][7] == "abandoned" and store.requests_today("a") == 42


def test_client_reports_every_request():
    from massive_collate.sources.massive import MassiveClient

    from .conftest import FakeResponse, FakeSession

    seen = []
    client = MassiveClient("K", session=FakeSession([FakeResponse(200, {})] * 3), sleep=lambda s: None)
    client.on_request = seen.append
    for _ in range(3):
        client.get("/x")
    assert seen == [1, 2, 3]


def test_grouped_days_and_denied(store):
    store.save_grouped_day("s", date(2026, 1, 2), [{"T": "AAA", "t": 1, "c": 1}])
    store.mark_grouped_day("s", date(2024, 1, 2), "denied")
    assert store.grouped_days("s") == {date(2026, 1, 2): "ok", date(2024, 1, 2): "denied"}
    assert store.clear_denied("s") == 1


def test_backup_and_integrity(tmp_path):
    with Store(tmp_path / "db" / "m.db") as s:
        s.upsert_trades("s", "X", [_trade(1)])
        assert s.integrity_check() == "ok"
        dest = s.backup(tmp_path / "b" / "copy.db")
    with Store(dest) as copy:
        assert len(copy.load_trades("X")) == 1
