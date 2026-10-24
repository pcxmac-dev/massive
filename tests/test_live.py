"""Live crypto: the WebSocket client's framing, the exchanges' messages, and the recorder."""
from __future__ import annotations

import json
import socket
import struct
import threading

from massive_collate import live
from massive_collate.storage import Store
from massive_collate.wsclient import OP_PING, OP_PONG, OP_TEXT, WebSocket, encode_frame


def _server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n))
    return head + payload


class _Sock:
    def __init__(self, data: bytes):
        self.data, self.sent = data, b""

    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass


def _client(data: bytes) -> WebSocket:
    ws = WebSocket.__new__(WebSocket)
    ws.sock, ws._buf = _Sock(data), b""
    return ws


def test_frames_reassemble_and_pings_are_answered():
    text = "x" * 300  # past the 125-byte short length
    data = (_server_frame(OP_PING, b"hi") + _server_frame(OP_TEXT, text[:100].encode(), fin=False)
            + _server_frame(0x0, text[100:].encode()))
    ws = _client(data)
    assert ws.recv() == text
    assert ws.sock.sent[:1] == bytes([0x80 | OP_PONG])  # the ping was answered
    frame = encode_frame(OP_TEXT, b"abc", mask=b"\x01\x02\x03\x04")
    assert frame[:2] == bytes([0x81, 0x80 | 3]) and frame[2:6] == b"\x01\x02\x03\x04"
    assert bytes(b ^ frame[2:6][i % 4] for i, b in enumerate(frame[6:])) == b"abc"  # masked as required


def test_exchange_messages_become_trades():
    cb = live.CoinbaseStream().parse({"type": "match", "trade_id": 7, "product_id": "ETH-USD", "price": "2500.10",
                                      "size": "0.5", "time": "2026-10-24T07:00:00.123456Z"})
    assert cb == [("ETHUSD", {"sip_timestamp": 1_792_825_200_123_456_000, "sequence_number": 7, "price": 2500.1,
                              "size": 0.5, "exchange": None, "conditions": [], "id": "7", "tape": None})]
    assert live.CoinbaseStream().parse({"type": "heartbeat"}) == []
    bs = live.BitstampStream().parse({"event": "trade", "channel": "live_trades_btcusd", "data": {
        "id": 9, "microtimestamp": "1792825200123456", "price": 81000, "price_str": "81000.5", "amount": 0.1,
        "amount_str": "0.1"}})
    assert bs[0][0] == "BTCUSD" and bs[0][1]["sip_timestamp"] == 1_792_825_200_123_456_000 and bs[0][1]["price"] == 81000.5
    try:
        live.BitstampStream().parse({"event": "bts:request_reconnect"})
    except live.Reconnect:
        pass
    else:
        raise AssertionError("a reconnect request must end the connection")
    subs = [json.loads(m) for m in live.BitstampStream().subscribe(("BTC", "ETH"))]
    assert [s["data"]["channel"] for s in subs] == ["live_trades_btcusd", "live_trades_ethusd"]


def test_the_recorder_stores_ticks_builds_bars_and_beats(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    t0 = 1_792_825_200_000  # a whole minute, ms
    rec._since["coinbase"] = t0
    with rec._lock:
        for i in range(4):
            rec._buffer[("coinbase", "BTCUSD")].append(live._trade((t0 + i * 400) * 1_000_000, i, 81000 + i, 0.1, i))
    with Store(tmp_path / "m.db") as store:
        assert rec.flush(store, now_ms=t0 + 5_000) == 4
        assert store.trade_edges("coinbase", "BTCUSD") == (t0 * 1_000_000, (t0 + 1200) * 1_000_000)
        assert store.coverage("BTCUSD", "trades")["coinbase"] == [(t0, t0 + 5_000)]  # connected span
        conn = store._bars("coinbase")
        assert conn.execute("SELECT timespan, COUNT(*) FROM built_bars GROUP BY timespan").fetchall() == [
            ("minute", 1), ("second", 2)]
        assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0  # raw bars untouched
        (d,) = store.derivations(kind="bars-from-ticks")
        assert d["settings"]["live"] is True and d["rows"] == 3
        status = json.loads(store.kv_get(live.STATUS_KEY))
        assert status["streams"]["coinbase"]["pairs"]["BTCUSD"]["trades"] == 4 and store.kv_get(live.RECORDER_KEY)


def test_a_stream_reconnects_after_it_drops(tmp_path):
    calls = []

    class FakeWS:
        def __init__(self, msgs):
            self.msgs, self.sent = msgs, []

        def send(self, m):
            self.sent.append(m)

        def recv(self):
            if self.msgs:
                return self.msgs.pop(0)
            raise ConnectionError("dropped")

        def close(self):
            pass

    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None)

    def connect(url):
        calls.append(url)
        if len(calls) >= 3:
            rec.stop.set()  # the third connection ends the test (stopping, it reads nothing)
        return FakeWS([json.dumps({"type": "match", "trade_id": len(calls), "product_id": "BTC-USD",
                                   "price": "1", "size": "1", "time": "2026-10-24T07:00:00Z"})])

    rec.connect = connect
    waits = []
    rec.stop.wait = lambda timeout=None: waits.append(timeout) or rec.stop.is_set()  # no real backoff sleeps
    rec._run_stream(rec.streams[0])
    assert len(calls) == 3 and len(rec._buffer[("coinbase", "BTCUSD")]) == 2  # one trade per connection that ran
    assert rec._status["coinbase"]["connected"] is True  # reconnected after each drop (stopped while connected)
    # Each connection dropped at once: the waits grow, they do not start over at a second each time.
    assert waits == [1.0, 2.0] and rec._status["coinbase"]["failures"] == 2


def test_a_stream_that_held_starts_its_backoff_over(tmp_path, monkeypatch):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None)
    clock = [1000.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    calls, waits = [], []

    class Drop:
        def send(self, m):
            pass

        def recv(self):
            clock[0] += live.STABLE_SECONDS if len(calls) == 3 else 1  # only the third connection holds a while
            raise ConnectionError("dropped")

        def close(self):
            pass

    def connect(url):
        calls.append(url)
        if len(calls) >= 5:
            rec.stop.set()
        return Drop()

    rec.connect = connect
    rec.stop.wait = lambda timeout=None: waits.append(timeout) or rec.stop.is_set()
    rec._run_stream(rec.streams[0])
    assert waits == [1.0, 2.0, 1.0, 2.0]  # after the connection that held: from a second again


def test_only_one_recorder_runs_on_a_machine_whatever_database_it_uses(tmp_path):
    import os

    first, second = tmp_path / "a" / "m.db", tmp_path / "b" / "m.db"
    first.parent.mkdir()
    second.parent.mkdir()
    assert not live.running(first) and not live.running(second)
    locks = live.take_locks(first)
    assert live.running(first) and live.running(second)  # another database's recorder must not start
    assert live.machine_lock().read_text() == str(os.getpid())
    live.release_locks(locks)
    assert not live.running(second) and not live.machine_lock().exists()
    live.machine_lock().write_text("999999999")  # a recorder that died without cleaning up
    assert not live.running(second)
