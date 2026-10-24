"""Live crypto: trades from exchanges' public WebSocket streams (no key), recorded as ticks in each
exchange's own tick file, with one-second and minute bars built from them every few seconds (kept
apart from the exchange's own bars in built_bars, under a derivation record) so charts update live.

Exchanges: Coinbase (the reference exchange) and Bitstamp; well-known coins only (LIVE_COINS). A
thread per exchange keeps one connection and reconnects with backoff. While it is connected, its
span is entered in the coverage ledger for every coin it streams: a connected span without trades
had none. The heartbeat (kv `live-recorder`) and each stream's state per pair (kv `live:status`)
feed the Live view and the rail icon. One recorder runs at a time (a lock file beside the data).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LIVE_COINS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "LINK")
RECORDER_KEY = "live-recorder"
STATUS_KEY = "live:status"
FLUSH_SECONDS = 2.0
DERIVE_SECONDS = 60.0  # the time frames above a minute are rebuilt this often for the coins streamed
RECV_TIMEOUT = 20.0
DAY_MS = 86_400_000


class Reconnect(ConnectionError):
    """The exchange asked for a new connection."""


def _ns(iso: str) -> int:
    """An RFC 3339 time with up to nanosecond digits as ns since the epoch, exactly."""
    whole, _, frac = iso.rstrip("Z").partition(".")
    return int(datetime.fromisoformat(whole + "+00:00").timestamp()) * 10**9 + int((frac + "000000000")[:9])


def _trade(ts_ns: int, seq: int, price, size, trade_id) -> dict:
    return {"sip_timestamp": ts_ns, "sequence_number": seq, "price": float(price), "size": float(size),
            "exchange": None, "conditions": [], "id": str(trade_id), "tape": None}


@dataclass
class CoinbaseStream:
    """Coinbase Exchange's public feed: the matches channel (every trade) and a heartbeat a second."""
    source: str = "coinbase"
    url: str = "wss://ws-feed.exchange.coinbase.com"
    keepalive: str | None = None

    def subscribe(self, coins) -> list[str]:
        return [json.dumps({"type": "subscribe", "product_ids": [f"{c}-USD" for c in coins],
                            "channels": ["matches", "heartbeat"]})]

    def parse(self, msg: dict) -> list[tuple[str, dict]]:
        kind = msg.get("type")
        if kind == "error":
            raise ConnectionError(f"coinbase: {msg.get('message')} {msg.get('reason') or ''}".strip())
        if kind not in ("match", "last_match"):
            return []
        ticker = msg["product_id"].replace("-", "")
        tid = int(msg["trade_id"])
        return [(ticker, _trade(_ns(msg["time"]), tid, msg["price"], msg["size"], tid))]


@dataclass
class BitstampStream:
    """Bitstamp's public v2 feed: a live_trades channel per pair."""
    source: str = "bitstamp"
    url: str = "wss://ws.bitstamp.net"
    keepalive: str | None = json.dumps({"event": "bts:heartbeat"})

    def subscribe(self, coins) -> list[str]:
        return [json.dumps({"event": "bts:subscribe", "data": {"channel": f"live_trades_{c.lower()}usd"}})
                for c in coins]

    def parse(self, msg: dict) -> list[tuple[str, dict]]:
        event = msg.get("event")
        if event == "bts:request_reconnect":
            raise Reconnect("bitstamp asked for a new connection")
        if event != "trade":
            return []
        d = msg["data"]
        ticker = msg["channel"].removeprefix("live_trades_").upper()
        tid = int(d["id"])
        return [(ticker, _trade(int(d["microtimestamp"]) * 1000, tid, d.get("price_str", d["price"]),
                                d.get("amount_str", d["amount"]), tid))]


STREAMS = {"coinbase": CoinbaseStream, "bitstamp": BitstampStream}


@dataclass
class Recorder:
    db_path: Path
    coins: tuple[str, ...] = LIVE_COINS
    sources: tuple[str, ...] = ("coinbase", "bitstamp")
    log: object = print
    connect: object = None  # WebSocket factory (tests pass a fake)
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.streams = [STREAMS[s]() for s in self.sources]
        self._lock = threading.Lock()
        self._buffer: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._since: dict[str, int] = {}  # source -> connected since (ms)
        self._status: dict[str, dict] = {s.source: {"connected": False, "pairs": {}} for s in self.streams}
        self._derivations: dict[tuple[str, str, int], int] = {}
        self._built: dict[int, int] = defaultdict(int)
        if self.connect is None:
            from .wsclient import WebSocket

            self.connect = lambda url: WebSocket(url, timeout=RECV_TIMEOUT)

    # -- streams ---------------------------------------------------------------------------
    def _run_stream(self, stream) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            ws = None
            try:
                ws = self.connect(stream.url)
                for m in stream.subscribe(self.coins):
                    ws.send(m)
                with self._lock:
                    self._since[stream.source] = int(time.time() * 1000)
                    self._status[stream.source].update(connected=True, since=datetime.now(timezone.utc).isoformat(),
                                                       error=None)
                self.log(f"live: {stream.source} connected ({len(self.coins)} coins)")
                backoff, misses = 1.0, 0
                while not self.stop.is_set():
                    try:
                        raw = ws.recv()
                    except socket.timeout:
                        misses += 1
                        if not stream.keepalive or misses > 2:
                            raise ConnectionError("no messages")
                        ws.send(stream.keepalive)
                        continue
                    misses = 0
                    trades = stream.parse(json.loads(raw))
                    if trades:
                        with self._lock:
                            for ticker, t in trades:
                                self._buffer[(stream.source, ticker)].append(t)
            except (OSError, ConnectionError, ValueError, KeyError) as e:
                with self._lock:
                    self._since.pop(stream.source, None)
                    self._status[stream.source].update(connected=False, error=str(e)[:200])
                if not self.stop.is_set():
                    self.log(f"live: {stream.source} disconnected ({e}); again in {backoff:.0f} s")
                    self.stop.wait(backoff)
                    backoff = min(60.0, backoff * 2)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except OSError:
                        pass

    # -- storing ---------------------------------------------------------------------------
    def _derivation(self, store, source: str, ticker: str, day: int, url: str) -> int:
        from . import provenance

        key = (source, ticker, day)
        if key not in self._derivations:
            self._derivations[key] = provenance.record(store, "bars-from-ticks", f"{source}:{ticker}", {
                "steps": ["second", "minute"], "live": True, "flush_seconds": FLUSH_SECONDS,
                "method": "trades from the exchange's public stream; bars rebuilt as trades arrive "
                          "(a forming bar changes until it closes)"},
                {"stream": url, "ticks": f"{source}:{ticker}"}, (day * DAY_MS, (day + 1) * DAY_MS))
        return self._derivations[key]

    def flush(self, store, now_ms: int | None = None) -> int:
        """Store the trades received since the last flush, enter the connected spans in the ledger,
        rebuild the affected bars, and beat the heartbeat. Returns trades stored."""
        from .ticks import build_bars

        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            batch, self._buffer = self._buffer, defaultdict(list)
            since = dict(self._since)
        stored = 0
        urls = {s.source: s.url for s in self.streams}
        for (source, ticker), trades in batch.items():
            stored += store.upsert_trades(source, ticker, trades)
        for source, start in since.items():
            for coin in self.coins:
                store.add_coverage(source, f"{coin}USD", "trades", start, now_ms)
        for (source, ticker), trades in batch.items():
            lo = min(t["sip_timestamp"] for t in trades) // 1_000_000 // 60_000 * 60_000  # the minute it starts in
            ticks = store.trades_between(source, ticker, lo * 1_000_000, now_ms * 1_000_000)
            day = now_ms // DAY_MS
            did = self._derivation(store, source, ticker, day, urls[source])
            for step, timespan in ((1_000_000_000, "second"), (60_000_000_000, "minute")):
                bars, _ = build_bars(ticks, step, {})
                self._built[did] += store.upsert_built_bars(source, ticker, timespan, 1, bars, did)
                store.add_coverage(source, ticker, timespan, lo, now_ms, origin="ticks")
            store.set_derivation_rows(did, self._built[did])
            last = trades[-1]
            with self._lock:
                pair = self._status[source]["pairs"].setdefault(ticker, {"trades": 0})
                pair["trades"] += len(trades)
                pair["last"] = last["sip_timestamp"] // 1_000_000
                pair["price"] = last["price"]
        with self._lock:
            status = json.loads(json.dumps(self._status))
        store.kv_set(RECORDER_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.kv_set(STATUS_KEY, json.dumps({"at": now_ms, "coins": list(self.coins), "streams": status}))
        return stored

    def run(self) -> None:
        from .storage import Store

        threads = [threading.Thread(target=self._run_stream, args=(s,), daemon=True, name=f"live-{s.source}")
                   for s in self.streams]
        for t in threads:
            t.start()
        with Store(self.db_path) as store:
            total, last_log, last_derive = 0, time.time(), time.time()
            while not self.stop.wait(FLUSH_SECONDS):
                total += self.flush(store)
                if time.time() - last_derive > DERIVE_SECONDS:
                    from . import derive

                    derive.run(store, sources={s.source for s in self.streams},
                               tickers={f"{c}USD" for c in self.coins}, log=lambda *_: None)
                    last_derive = time.time()
                if time.time() - last_log > 600:
                    self.log(f"live: {total:,} trades recorded in the last 10 min")
                    total, last_log = 0, time.time()
            self.flush(store)


# -- one recorder at a time ---------------------------------------------------------------

def lock_path(db_path: Path) -> Path:
    return db_path.with_name("massive-live.lock")


def running(db_path: Path) -> bool:
    """Whether a recorder holds the lock (its process alive)."""
    from .cli import pid_alive

    try:
        pid = int(lock_path(db_path).read_text().strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def start_detached(db_path: Path) -> bool:
    """Start a recorder in the background (no window) unless one is running. Returns whether started."""
    import subprocess
    import sys

    if running(db_path):
        return False
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe") if os.name == "nt" and exe.with_name("pythonw.exe").exists() else exe
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(pythonw), "-m", "massive_collate", "live"], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
