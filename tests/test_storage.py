from __future__ import annotations

from datetime import date

from massive_collate.storage import Store, feeds_backup_dir


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


def test_series_caches_refresh_on_own_and_other_writes(tmp_path, monkeypatch):
    from massive_collate.storage import feeds

    a = Store(tmp_path / "m.db")
    b = Store(tmp_path / "m.db")  # e.g. the collector, writing while the chart app reads
    assert a.stored_series() == []
    a.upsert_trades("s", "X", [_trade(1)])
    assert a.stored_series() == [("s", "X", "trades")]  # own write seen at once
    fp = a.series_fingerprint("s", "X")
    b.upsert_bars("s", "Y", "minute", 1, [{"t": 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    b.upsert_trades("s", "X", [_trade(2)])
    assert a.series_fingerprint("s", "X") != fp  # a change to a series shows at once
    # another connection's new series: listed within STORED_SERIES_S (a listing reads every series' index)
    assert ("s", "Y", "minute") not in a.stored_series()
    monkeypatch.setattr(feeds, "STORED_SERIES_S", 0.0)
    assert ("s", "Y", "minute") in a.stored_series()
    a.close(); b.close()


def test_grouped_days_and_denied(store):
    store.save_grouped_day("s", date(2026, 1, 2), [{"T": "AAA", "t": 1, "c": 1}])
    store.mark_grouped_day("s", date(2024, 1, 2), "denied")
    assert store.grouped_days("s") == {date(2026, 1, 2): "ok", date(2024, 1, 2): "denied"}
    assert store.clear_denied("s") == 1


def test_backup_and_integrity(tmp_path):
    with Store(tmp_path / "db" / "m.db") as s:
        s.upsert_trades("s", "X", [_trade(1)])
        s.upsert_bars("s", "X", "minute", 1, [{"t": 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
        assert s.integrity_check() == "ok"
        assert (tmp_path / "db" / "feeds" / "s-ticks.db").exists()  # ticks in a file of their own
        plain = s.backup(tmp_path / "b" / "plain.db")
        dest = s.backup(tmp_path / "b" / "copy.db", ticks=True)
    with Store(plain, feeds_dir=feeds_backup_dir(plain)) as copy:
        assert len(copy.load_trades("X")) == 0  # bars only by default: no ticks
        assert len(copy.load_bars(["X"], "minute")) == 1
    with Store(dest, feeds_dir=feeds_backup_dir(dest)) as copy:
        assert len(copy.load_trades("X")) == 1 and len(copy.load_bars(["X"], "minute")) == 1


def test_history_written_newest_first_does_not_leave_pages_half_empty():
    # feeds send history newest first; written in that order a WITHOUT ROWID table split its
    # pages half full (1.76 times the disk): each write is stored oldest first
    from massive_collate.storage import Store

    def pages(descending: bool) -> int:
        s = Store(":memory:")
        t0 = 1_700_000_000_000
        for page in range(10):
            bars = [{"t": t0 + (page * 5000 + i) * 60_000, "o": 1.5, "h": 2.5, "l": 0.5, "c": 1.25, "v": 1234.0}
                    for i in range(5000)]
            s.upsert_bars("massive", "AAPL", "minute", 1, bars[::-1] if descending else bars)
        n = s._bars("massive").execute("PRAGMA page_count").fetchone()[0]
        s.close()
        return n

    assert pages(descending=True) <= pages(descending=False) * 1.05



def test_another_instruments_rows_are_moved_to_the_archive_never_deleted(tmp_path):
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db", ticks=True) as s:
        old, new = 1_600_000_000_000, 1_700_000_000_000
        bar = lambda t: {"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        s.upsert_bars("massive", "AAPL", "minute", 1, [bar(old), bar(new)])
        s.upsert_bars("massive", "AAPL", "day", 1, [bar(old), bar(new)])
        s.upsert_bars("massive", "MSFT", "minute", 1, [bar(old)])
        s.upsert_trades("massive", "AAPL", [{"sip_timestamp": old * 10**6, "sequence_number": 1, "price": 1.0,
                                             "size": 1.0, "conditions": [], "id": "1", "tape": 3}])
        conn = s._bars("massive")
        before = conn.total_changes
        assert s.archive_before("massive", "AAPL", (old - 1) * 10**6) == 0
        assert conn.total_changes == before  # nothing older: no write at all
        # the old minute, the old day and the old trade: out of the series, kept in the archive
        assert s.archive_before("massive", "AAPL", (old + 1) * 10**6, reason="before its listing") == 3
        assert s.archived_rows("massive", "AAPL") == 3 and s.archived_rows("massive", "MSFT") == 0
        assert conn.execute("SELECT reason FROM archived_bars").fetchall() == [("before its listing",)] * 2
        assert s.bar_edges("massive", "AAPL") == (new, new) and s.bar_edges("massive", "MSFT") == (old, old)


def test_write_ahead_logs_are_cut_back_once_checkpointed(tmp_path):
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db") as s:
        s.upsert_bars("massive", "AAPL", "minute", 1, [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
        assert s.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 2**20
        assert s._bars("massive").execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 2**20
