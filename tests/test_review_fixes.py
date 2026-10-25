"""From a review of the whole system: a backlog bounded while writes fail, a message's total
size limited, every database compacted, one build of a series at a time, and retention for the
logs and the backups."""
from __future__ import annotations

import os
import sqlite3

import pytest

from massive_collate import cli, derive, live, wsclient
from massive_collate.storage import Store
from massive_collate.wsclient import OP_TEXT, WebSocketClosed

from .test_live import _client, _server_frame


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


def _trade(ms: int, seq: int) -> dict:
    return {"sip_timestamp": ms * 1_000_000, "sequence_number": seq, "price": 1.0, "size": 1.0}


def test_a_backlog_kept_while_writes_fail_is_bounded_and_not_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "MAX_BUFFERED", 10)
    logged = []
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=logged.append)
    rec._since["coinbase"] = 1_000
    rec._buffer[("coinbase", "BTCUSD")] = [_trade(2_000 + i, i) for i in range(12)]
    rec._buffer[("coinbase", "ETHUSD")] = [_trade(2_000 + i, 100 + i) for i in range(6)]

    class Refusing:
        def upsert_trades(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        rec.flush(Refusing(), now_ms=10_000)
    kept = rec._buffer[("coinbase", "BTCUSD")] + rec._buffer[("coinbase", "ETHUSD")]
    assert len(kept) <= 10  # the oldest went, from each pair
    assert rec._buffer[("coinbase", "BTCUSD")][-1]["sequence_number"] == 11  # the newest stay
    dropped_to = max(t["sip_timestamp"] for t in [_trade(2_000 + i, i) for i in range(12)]
                     if t not in rec._buffer[("coinbase", "BTCUSD")]) // 1_000_000
    assert rec._since["coinbase"] > dropped_to  # the ledger claims none of the dropped time
    assert "trades dropped" in logged[-1]


def test_a_message_in_fragments_has_a_total_size_limit(monkeypatch):
    monkeypatch.setattr(wsclient, "MAX_MESSAGE", 250)
    data = _server_frame(OP_TEXT, b"x" * 100, fin=False) + _server_frame(0x0, b"x" * 100, fin=False) \
        + _server_frame(0x0, b"x" * 100)
    with pytest.raises(WebSocketClosed, match="in fragments"):
        _client(data).recv()


def test_vacuum_compacts_every_file(tmp_path):
    with Store(tmp_path / "m.db") as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
        s.upsert_trades("massive", "SPCX", [{"sip_timestamp": 1, "sequence_number": 1, "price": 1.0, "size": 1}])
        (tmp_path / "datasets").mkdir(exist_ok=True)
        sqlite3.connect(tmp_path / "datasets" / "french.db").close()
        done = s.vacuum(log=lambda *_: None)
    names = {os.path.basename(f) for f in done}
    assert {"m.db", "massive.db", "massive-ticks.db", "french.db"} <= names


def test_backups_keep_the_newest_and_never_touch_other_files(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for d in range(1, 10):
        (folder / f"massive-202610{d:02d}-120000.db").write_bytes(b"x")
        (folder / f"massive-202610{d:02d}-120000-feeds").mkdir()
    (folder / "pre-split-20261023-215938.db").write_bytes(b"x")
    removed = cli.prune_backups(folder, 7)
    assert removed == ["massive-20261001-120000.db", "massive-20261002-120000.db"]
    assert not (folder / "massive-20261001-120000-feeds").exists() and (folder / "massive-20261009-120000-feeds").exists()
    assert (folder / "pre-split-20261023-215938.db").exists()
    assert cli.prune_backups(folder, 0) == []  # 0 keeps all


def test_the_log_is_rotated_and_old_ones_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_MAX_BYTES", 100)
    log = cli.Logger(tmp_path / "massive-collate.log")
    for i in range(60):
        log("x" * 40 + str(i))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "massive-collate.log" in files and "massive-collate.log.1" in files
    assert len(files) <= 1 + cli.LOG_KEEP


def test_one_build_of_a_series_at_a_time(store):
    other = os.getppid()  # a live process that is not this one
    store.kv_set(derive.LEASE_KEY.format("massive", "SPCX"),
                 f'{{"pid": {other}, "start": 0, "until": 9999999999999}}')
    logged = []
    assert derive.build_symbol(store, "massive", "SPCX", log=logged.append) == 0
    assert "another process is building it" in logged[0]
    store.kv_set(derive.LEASE_KEY.format("massive", "SPCX"), '{"pid": 999999999, "start": 0, "until": 9999999999999}')
    assert derive.take_lease(store, "massive", "SPCX")  # an ended process's lease is taken over
    derive.drop_lease(store, "massive", "SPCX")
    assert store.kv_json(derive.LEASE_KEY.format("massive", "SPCX"), {}) == {}
