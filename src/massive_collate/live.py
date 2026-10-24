"""Live trades from real-time streams, recorded as ticks in each feed's own tick file, with
one-second and minute bars built from them every few seconds (kept apart from the feed's own bars
in built_bars, under a derivation record) so charts update live.

Crypto: Coinbase (the reference exchange) and Bitstamp's public streams (no key), well-known coins
only (LIVE_COINS). Stocks: Alpaca's free real-time stream of IEX's trades (with Alpaca keys) for
the symbols open on a chart and on the rail's watchlists, at most MAX_STOCKS (Alpaca Basic's limit
on its one stream connection per account). IEX is one exchange, a few per cent of US volume, so
its trades go in a feed of their own ("iex", one of PARTIAL_FEEDS) that a chart of all feeds shows
only where no feed of the whole market (SIP) has bars yet: the current session, until a sync
downloads it.

A thread per stream keeps one connection and reconnects with backoff. While it is connected, its
span is entered in the coverage ledger for every symbol it streams: a connected span without trades
had none. The heartbeat (kv `live-recorder`) and each stream's state per symbol (kv `live:status`)
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
OPEN_KEY = "live:open"  # {ticker: ms}: the symbols chart pages have open (gui/server.py notes them)
WATCHLISTS_KEY = "ui:watchlists"  # the rail's lists of symbols (gui/server.py)
IEX_FEED = "iex"
PARTIAL_FEEDS = frozenset({IEX_FEED})  # one exchange's share of a market other feeds hold whole
MAX_STOCKS = 30  # Alpaca Basic: symbols on its one real-time connection
OPEN_FRESH_MS = 180_000  # a symbol is streamed this long after a page last said it has it open
WANT_SECONDS = 5.0  # how often the stock symbols to stream are looked up
FLUSH_SECONDS = 2.0
DERIVE_SECONDS = 60.0  # the time frames above a minute are rebuilt this often for the symbols streamed
RULES_SECONDS = 3600.0  # trade condition rules (stocks) are reread this often
RECV_TIMEOUT = 20.0
# Sanity check for a stream that keeps failing: the wait before connecting again doubles with each
# failure in a row (1 s up to MAX_BACKOFF) and starts over only after a connection that held for
# STABLE_SECONDS. A server that accepts and at once drops the connection (Coinbase: "subscription
# limit reached") is otherwise asked again every second, forever.
MAX_BACKOFF = 300.0
STABLE_SECONDS = 60.0
DAY_MS = 86_400_000
PING = "ping"  # keepalive by WebSocket ping frames rather than a message


class Reconnect(ConnectionError):
    """The exchange asked for a new connection."""


class Refused(ConnectionError):
    """The server ended the session (keys, plan, or another connection on the account): tried
    again after `wait` seconds at the earliest."""

    def __init__(self, message: str, wait: float):
        super().__init__(message)
        self.wait = wait


class Notice(Exception):
    """The server turned down a request but kept the connection (shown, not retried)."""


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

# Alpaca stream errors that end the session, and the seconds to wait before connecting again:
# 401 not authenticated, 402 auth failed, 404 auth timeout, 406 connection limit exceeded (another
# connection on the account), 407 slow client, 409 insufficient subscription, 500 internal error.
ALPACA_REFUSALS = {401: 60, 402: 600, 404: 60, 406: 120, 407: 60, 409: 3600, 500: 60}


@dataclass
class AlpacaIexStream:
    """Alpaca's free real-time stock stream (v2/iex): every trade on IEX, with cancels and
    corrections (the trades they withdraw are marked `withdrawn`). The keys go in the auth
    message only, never in a log. Its symbols change while it is connected (`change`), once the
    server has accepted the keys; `confirmed` holds those the server says it streams."""
    key_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    source: str = IEX_FEED
    url: str = "wss://stream.data.alpaca.markets/v2/iex"
    keepalive: str | None = PING
    dynamic: bool = True  # symbols from the charts and watchlists (wanted_stocks), not LIVE_COINS
    tape_rules: bool = True  # bars by the consolidated tape's rules per trade condition (ticks.py)
    method: str = ("IEX's trades from Alpaca's real-time stream: one exchange, a few per cent of US volume; "
                   "bars rebuilt as trades arrive (a forming bar changes until it closes), by the consolidated "
                   "tape's rules per trade condition; a feed of the whole market (SIP) ranks above them")
    authed: bool = False
    confirmed: frozenset = frozenset()

    def subscribe(self, symbols) -> list[str]:
        """The first messages on a new connection: the keys (symbols follow once accepted)."""
        self.authed, self.confirmed = False, frozenset()
        return [json.dumps({"action": "auth", "key": self.key_id, "secret": self.secret_key})]

    def change(self, add, drop) -> list[str]:
        out = []
        if drop:
            out.append(json.dumps({"action": "unsubscribe", "trades": sorted(drop)}))
        if add:
            out.append(json.dumps({"action": "subscribe", "trades": sorted(add)}))
        return out

    def parse(self, msg) -> list[tuple[str, dict]]:
        from .sources.alpaca import normalize_trade

        out = []
        for m in msg if isinstance(msg, list) else [msg]:
            kind = m.get("T")
            if kind == "t":
                t = normalize_trade(m)
                if t:
                    out.append((m["S"], t))
            elif kind == "x":  # a trade withdrawn: canceled, or an error
                out.append((m["S"], {**normalize_trade(m), "withdrawn": True}))
            elif kind == "c":  # a correction: the original trade withdrawn, the corrected one in its place
                base = {"x": m["x"], "t": m["t"], "z": m.get("z")}
                was = normalize_trade({**base, "i": m["oi"], "p": m["op"], "s": m["os"], "c": m.get("oc")})
                out.append((m["S"], {**was, "withdrawn": True}))
                out.append((m["S"], normalize_trade({**base, "i": m["ci"], "p": m["cp"], "s": m["cs"], "c": m.get("cc")})))
            elif kind == "subscription":
                self.confirmed = frozenset(m.get("trades") or [])
            elif kind == "success" and m.get("msg") == "authenticated":
                self.authed = True
            elif kind == "error":
                code = int(m.get("code") or 0)
                text = f"alpaca {code}: {m.get('msg')}"
                if code in ALPACA_REFUSALS:
                    raise Refused(text, ALPACA_REFUSALS[code])
                raise Notice(text)
        return out


def stock_keys(settings) -> tuple[str, str] | None:
    """Alpaca's keys for the IEX stream: None without keys, with LIVE_STOCKS=off, or offline."""
    a = settings.alpaca
    if settings.offline or not settings.live_stocks or not (a and a.has_keys):
        return None
    return a.key_id, a.secret_key


def wanted_stocks(store, now_ms: int) -> tuple[str, ...]:
    """The stock symbols to stream, at most MAX_STOCKS: those open on a chart (most recently
    noted first), then the rail's watchlists in their order. Only symbols collected as stocks."""
    def kv(key, empty):
        try:
            value = json.loads(store.kv_get(key) or "null")
        except ValueError:
            return empty
        return value if isinstance(value, type(empty)) else empty
    opened = sorted(((at, t) for t, at in kv(OPEN_KEY, {}).items()
                     if isinstance(at, (int, float)) and now_ms - at <= OPEN_FRESH_MS), reverse=True)
    out = [t for _, t in opened]
    for lst in kv(WATCHLISTS_KEY, []):
        out += (lst.get("tickers") or []) if isinstance(lst, dict) else []
    out = [t.upper() for t in out if isinstance(t, str) and 0 < len(t) <= 32]
    stocks = (t for t in dict.fromkeys(out) if any(w["asset"] == "stock" for w in store.watchlist(ticker=t)))
    return tuple(t for _, t in zip(range(MAX_STOCKS), stocks))


@dataclass
class Recorder:
    db_path: Path
    coins: tuple[str, ...] = LIVE_COINS
    sources: tuple[str, ...] = ("coinbase", "bitstamp")
    stock_keys: tuple[str, str] | None = field(default=None, repr=False)  # Alpaca's: the IEX stream
    log: object = print
    connect: object = None  # WebSocket factory (tests pass a fake)
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.streams = [STREAMS[s]() for s in self.sources] if self.coins else []
        if self.stock_keys:
            self.streams.append(AlpacaIexStream(*self.stock_keys))
        self._lock = threading.Lock()
        self._buffer: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._since: dict[str, int] = {}  # source -> connected since (ms)
        self._status: dict[str, dict] = {s.source: {"connected": False, "pairs": {}} for s in self.streams}
        self._wanted: tuple[str, ...] = ()  # stock symbols to stream (wanted_stocks)
        self._subs: dict[str, dict[str, int]] = {s.source: {} for s in self.streams}  # symbol -> asked for since (ms)
        self._rules: tuple[float, dict] = (0.0, {})
        self._derivations: dict[tuple[str, str, int], int] = {}
        self._built: dict[int, int] = defaultdict(int)
        if self.connect is None:
            from .wsclient import WebSocket

            self.connect = lambda url: WebSocket(url, timeout=RECV_TIMEOUT)

    # -- streams ---------------------------------------------------------------------------
    def _follow(self, stream, ws) -> bool:
        """Bring a stream's symbols in line with those wanted (once the server took the keys).
        False when none are wanted any more."""
        with self._lock:
            want, have = set(self._wanted), self._subs[stream.source]
            add, drop = want - set(have), set(have) - want
        if not want:
            return False
        if stream.authed and (add or drop):
            for m in stream.change(add, drop):
                ws.send(m)
            now = int(time.time() * 1000)
            with self._lock:
                for t in drop:
                    have.pop(t, None)
                have.update(dict.fromkeys(add, now))
        return True

    def _run_stream(self, stream) -> None:
        backoff, failures = 1.0, 0
        dynamic = getattr(stream, "dynamic", False)
        while not self.stop.is_set():
            if dynamic and not self._wanted:  # nothing to stream: no connection
                self.stop.wait(WANT_SECONDS)
                continue
            ws, connected_at = None, None
            try:
                ws = self.connect(stream.url)
                for m in stream.subscribe(() if dynamic else self.coins):
                    ws.send(m)
                with self._lock:
                    self._since[stream.source] = int(time.time() * 1000)
                    self._status[stream.source].update(connected=True, since=datetime.now(timezone.utc).isoformat(),
                                                       error=None)
                self.log(f"live: {stream.source} connected ({'stocks' if dynamic else f'{len(self.coins)} coins'})")
                connected_at, misses, pongs = time.monotonic(), 0, 0
                while not self.stop.is_set():
                    if dynamic and not self._follow(stream, ws):
                        with self._lock:  # none wanted any more: the connection is closed
                            self._since.pop(stream.source, None)
                            self._subs[stream.source] = {}
                            self._status[stream.source].update(connected=False, error=None)
                        break
                    try:
                        raw = ws.recv()
                    except socket.timeout:
                        if getattr(ws, "pongs", 0) != pongs:  # the server answered the last ping
                            pongs, misses = ws.pongs, 0
                        misses += 1
                        if not stream.keepalive or misses > 2:
                            raise ConnectionError("no messages")
                        ws.ping() if stream.keepalive == PING else ws.send(stream.keepalive)
                        continue
                    misses = 0
                    try:
                        trades = stream.parse(json.loads(raw))
                    except Notice as e:
                        self.log(f"live: {stream.source}: {e}")
                        with self._lock:
                            self._status[stream.source]["error"] = str(e)[:200]
                        continue
                    if trades:
                        with self._lock:
                            for ticker, t in trades:
                                self._buffer[(stream.source, ticker)].append(t)
            except (OSError, ConnectionError, ValueError, KeyError) as e:
                if connected_at is not None and time.monotonic() - connected_at >= STABLE_SECONDS:
                    backoff, failures = 1.0, 0  # it had been working: a new failure, not more of the same
                failures += 1
                with self._lock:
                    self._since.pop(stream.source, None)
                    self._subs[stream.source] = {}
                    self._status[stream.source].update(connected=False, error=str(e)[:200], failures=failures)
                if not self.stop.is_set():
                    wait = max(backoff, getattr(e, "wait", 0))
                    self.log(f"live: {stream.source} disconnected ({e}); again in {wait:.0f} s"
                             + (f" ({failures} failures in a row)" if failures > 1 else ""))
                    self.stop.wait(wait)
                    backoff = min(MAX_BACKOFF, backoff * 2)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except OSError:
                        pass

    def set_wanted(self, tickers: tuple[str, ...]) -> None:
        with self._lock:
            self._wanted = tuple(tickers)[:MAX_STOCKS]

    # -- storing ---------------------------------------------------------------------------
    def _derivation(self, store, stream, ticker: str, day: int, rules: dict) -> int:
        from . import provenance

        key = (stream.source, ticker, day)
        if key not in self._derivations:
            settings = {"steps": ["second", "minute"], "live": True, "flush_seconds": FLUSH_SECONDS,
                        "method": getattr(stream, "method", "trades from the exchange's public stream; bars rebuilt "
                                          "as trades arrive (a forming bar changes until it closes)")}
            if getattr(stream, "tape_rules", False):
                settings |= {"rules": provenance.digest(sorted(rules.items())), "rule_count": len(rules)}
            self._derivations[key] = provenance.record(store, "bars-from-ticks", f"{stream.source}:{ticker}", settings,
                                                       {"stream": stream.url, "ticks": f"{stream.source}:{ticker}"},
                                                       (day * DAY_MS, (day + 1) * DAY_MS))
        return self._derivations[key]

    def _tape_rules(self, store) -> dict:
        """The trade condition rules (ticks.py) stocks' bars are built by, reread hourly."""
        at, rules = self._rules
        if not at or time.monotonic() - at > RULES_SECONDS:
            self._rules = (time.monotonic(), store.condition_rules())
        return self._rules[1]

    def _streamed(self, stream, since_ms: int, subs: dict[str, int]) -> dict[str, int]:
        """{symbol: streamed since (ms)} of a connected stream."""
        if getattr(stream, "dynamic", False):
            return {t: max(since_ms, at) for t, at in subs.items() if t in stream.confirmed}
        return {f"{c}USD": since_ms for c in self.coins}

    def flush(self, store, now_ms: int | None = None) -> int:
        """Store the trades received since the last flush (and remove those withdrawn), enter the
        connected spans in the ledger, rebuild the affected bars, and beat the heartbeat. Returns
        trades stored."""
        from .ticks import build_bars

        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            batch, self._buffer = self._buffer, defaultdict(list)
            since = dict(self._since)
            subs = {k: dict(v) for k, v in self._subs.items()}
        stored = 0
        streams = {s.source: s for s in self.streams}
        for (source, ticker), trades in batch.items():
            kept = [t for t in trades if not t.get("withdrawn")]
            if kept:
                stored += store.upsert_trades(source, ticker, kept)
        for source, start in since.items():
            for ticker, t0 in self._streamed(streams[source], start, subs.get(source, {})).items():
                store.add_coverage(source, ticker, "trades", t0, now_ms)
        for (source, ticker), trades in batch.items():
            stream = streams[source]
            lo_ns = min(t["sip_timestamp"] for t in trades)
            gone = [t["sequence_number"] for t in trades if t.get("withdrawn")]
            if gone:
                lo_ns = min([lo_ns, *store.withdraw_trades(source, ticker, gone, lo_ns - DAY_MS * 1_000_000)])
            lo = lo_ns // 1_000_000 // 60_000 * 60_000  # the minute it starts in
            if gone:  # a bar whose only trades were withdrawn must go
                store.drop_built_between(source, ticker, ("second", "minute"), lo, now_ms)
            ticks = store.trades_between(source, ticker, lo * 1_000_000, now_ms * 1_000_000)
            rules = self._tape_rules(store) if getattr(stream, "tape_rules", False) else {}
            did = self._derivation(store, stream, ticker, now_ms // DAY_MS, rules)
            for step, timespan in ((1_000_000_000, "second"), (60_000_000_000, "minute")):
                bars, _ = build_bars(ticks, step, rules)
                self._built[did] += store.upsert_built_bars(source, ticker, timespan, 1, bars, did)
                store.add_coverage(source, ticker, timespan, lo, now_ms, origin="ticks")
            store.set_derivation_rows(did, self._built[did])
            last = next((t for t in reversed(trades) if not t.get("withdrawn")), None)
            with self._lock:
                pair = self._status[source]["pairs"].setdefault(ticker, {"trades": 0})
                pair["trades"] += len(trades) - 2 * len(gone)
                if last:
                    pair["last"] = last["sip_timestamp"] // 1_000_000
                    pair["price"] = last["price"]
        with self._lock:
            for s in self.streams:
                if getattr(s, "dynamic", False):
                    self._status[s.source]["symbols"] = sorted(set(subs.get(s.source, {})) & s.confirmed)
            status = json.loads(json.dumps(self._status))
            wanted = list(self._wanted)
        store.kv_set(RECORDER_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.kv_set(STATUS_KEY, json.dumps({"at": now_ms, "coins": list(self.coins), "stocks": wanted,
                                             "streams": status}))
        return stored

    def run(self) -> None:
        from .storage import Store

        threads = [threading.Thread(target=self._run_stream, args=(s,), daemon=True, name=f"live-{s.source}")
                   for s in self.streams]
        for t in threads:
            t.start()
        stocks = any(getattr(s, "dynamic", False) for s in self.streams)
        with Store(self.db_path) as store:
            total, last_log, last_derive, last_want = 0, time.time(), time.time(), 0.0
            while True:
                if stocks and time.time() - last_want > WANT_SECONDS:
                    self.set_wanted(wanted_stocks(store, int(time.time() * 1000)))
                    last_want = time.time()
                if self.stop.wait(FLUSH_SECONDS):
                    break
                total += self.flush(store)
                if time.time() - last_derive > DERIVE_SECONDS:
                    from . import derive

                    derive.run(store, sources={s.source for s in self.streams},
                               tickers={f"{c}USD" for c in self.coins} | set(self._wanted), log=lambda *_: None)
                    last_derive = time.time()
                if time.time() - last_log > 600:
                    self.log(f"live: {total:,} trades recorded in the last 10 min")
                    total, last_log = 0, time.time()
            self.flush(store)


# -- one recorder at a time, on the database and on the machine --------------------------------
# Every recorder opens the same exchange streams from this machine's IP address: a second one,
# even on another database (a test copy), doubles the connections, which the exchanges refuse
# ("subscription limit reached") and can answer by banning the address. So besides its database's
# lock, a recorder holds a machine-wide one (MASSIVE_LIVE_LOCK to move it, as the tests do).

def lock_path(db_path: Path) -> Path:
    return db_path.with_name("massive-live.lock")


def machine_lock() -> Path:
    return Path(os.environ.get("MASSIVE_LIVE_LOCK") or Path.home() / ".massive-collate" / "live.lock")


def _holder(path: Path) -> int | None:
    """The live process holding a lock file, if any."""
    from .cli import pid_alive

    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def running(db_path: Path) -> bool:
    """Whether a recorder runs for this database, or any recorder on this machine."""
    return any(_holder(p) for p in (lock_path(db_path), machine_lock()))


def take_locks(db_path: Path) -> list[Path]:
    """Hold both locks for this process (the caller checked `running` first). Returns them."""
    locks = [lock_path(db_path), machine_lock()]
    for path in locks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
    return locks


def release_locks(locks: list[Path]) -> None:
    """Remove the locks this process holds (not one another recorder took since)."""
    for path in locks:
        try:
            if int(path.read_text().strip()) == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass


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
