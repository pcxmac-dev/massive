"""Live trades from real-time streams, recorded as ticks in each feed's own tick file, with
one-second and minute bars built from them every few seconds (kept apart from the feed's own bars
in built_bars, under a derivation record) so charts update live.

Only what is selected is streamed (LIVE_STREAMS=selected, the default; the user, 2026-10-27):
every stream follows the symbols open on a chart, and with none of its symbols open it is not
connected; the sync downloads that time later. LIVE_STREAMS=always keeps the coins streaming and
adds the rail's watchlists to the stocks.

Crypto: Coinbase (the reference exchange) and Bitstamp's public streams (no key), well-known coins
only (LIVE_COINS). Stocks: Alpaca's free real-time stream of IEX's trades (with Alpaca keys), at
most MAX_STOCKS (Alpaca Basic's limit on its one stream connection per account). IEX is one exchange, a few per cent of US volume, so
its trades go in a feed of their own ("iex", one of PARTIAL_FEEDS) that a chart of all feeds shows
only where no feed of the whole market (SIP) has bars yet: the current session, until a sync
downloads it.

A thread per stream keeps one connection and reconnects with backoff; a thread that ends anyway is
started again by the recorder (Recorder._supervise). While it is connected, its
span is entered in the coverage ledger for every symbol it streams: a connected span without trades
had none. The heartbeat (kv `live-recorder`) and each stream's state per symbol (kv `live:status`)
feed the Live view and the rail icon. One recorder runs at a time (a lock file beside the data).
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import killswitch

LIVE_COINS =("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "LINK")
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
# A connected stream's span is entered in the ledger up to the last time it was heard from (a
# message, or the answer to a keepalive) plus this: a connection that died quietly, or a machine
# that slept with it open, adds nothing (a quiet market still answers the keepalive).
HEARD_SLACK_MS = int(RECV_TIMEOUT * 1.5 * 1000)
LOCK_CHECK_SECONDS = 30.0  # how often the recorder makes sure it still holds its locks
# Sanity check for a stream that keeps failing: the wait before connecting again doubles with each
# failure in a row (1 s up to MAX_BACKOFF) and starts over only after a connection that held for
# STABLE_SECONDS. A server that accepts and at once drops the connection (Coinbase: "subscription
# limit reached") is otherwise asked again every second, forever.
# Past that, the kill policy (killswitch.py): a refusal, or FLAP_LIMIT connections dropped within
# STABLE_SECONDS of connecting, kills the stream for 30 min and up; it is not tried while killed.
# A connection that could not be made at all (no network, DNS) is not the server dropping it: it
# only waits longer each time.
MAX_BACKOFF = 300.0
STABLE_SECONDS = 60.0
KILL_CHECK_SECONDS = 30.0  # while a stream is killed: how often it looks whether the kill ended (or was lifted)
# A stream thread that ended while the recorder runs (an error got out of its loop) is started again
# after this, the wait doubling up to MAX_BACKOFF while it keeps ending (run, _supervise).
RESTART_SECONDS = 5.0
REAP_SECONDS = 300.0  # how often the recorder looks for other recorder processes (and ends them)
# The first look comes once the lock check has run twice: of two recorders that took the machine's
# lock at the same moment (a dead one's, taken over), the one that lost it has seen so and stopped,
# rather than ending the one that holds it.
FIRST_REAP_SECONDS = 2 * LOCK_CHECK_SECONDS
# Trades held in memory while the database refuses writes (locked, full disk): past this many
# bytes the oldest go, and the time they cover is no longer claimed as recorded (the sync
# downloads it). A waiting trade is a dict of about TRADE_BYTES (its keys shared): some 110,000.
MAX_BUFFERED_BYTES = 64 * 2**20
TRADE_BYTES = 600
RECORDER_COMMAND = re.compile(r"-m\s+massive_collate\s+live\s*$")  # a recorder (not `live status`, `live stop`)
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


def _settle(trades: list[dict]) -> tuple[list[dict], list[int]]:
    """A batch of one symbol's trades, in the order they came, as it stands at its end: (the trades
    standing, the sequence numbers withdrawn). A withdrawal takes out the trades before it with its
    sequence number (in the batch here, in the store by the caller, before the standing ones are
    stored), never one after it: a correction that keeps its trade's id keeps its sequence number."""
    standing: dict[int, list[dict]] = {}
    gone: list[int] = []
    for t in trades:
        seq = int(t.get("sequence_number") or 0)
        if t.get("withdrawn"):
            standing.pop(seq, None)
            gone.append(seq)
        else:
            standing.setdefault(seq, []).append(t)
    return [t for kept in standing.values() for t in kept], gone


@dataclass
class CoinbaseStream:
    """Coinbase Exchange's public feed: the matches channel (every trade) and a heartbeat a second.
    `dynamic`: its coins are those open on a chart (`change` while connected), not all LIVE_COINS."""
    source: str = "coinbase"
    url: str = "wss://ws-feed.exchange.coinbase.com"
    keepalive: str | None = None
    dynamic: bool = False
    asset: str = "crypto"
    authed: bool = True  # nothing to authenticate: symbols can change as soon as it is connected
    confirmed: frozenset = frozenset()  # the pairs the server says it streams (dynamic)

    def subscribe(self, coins) -> list[str]:
        self.confirmed = frozenset()
        return [json.dumps({"type": "subscribe", "product_ids": [f"{c}-USD" for c in coins],
                            "channels": ["matches", "heartbeat"]})] if coins else []

    def change(self, add, drop) -> list[str]:
        """Messages taking pairs (BTCUSD) off and onto the connection."""
        return [json.dumps({"type": kind, "product_ids": [f"{t[:-3]}-USD" for t in sorted(pairs)],
                            "channels": ["matches", "heartbeat"]})
                for kind, pairs in (("unsubscribe", drop), ("subscribe", add)) if pairs]

    def parse(self, msg: dict) -> list[tuple[str, dict]]:
        kind = msg.get("type")
        if kind == "error":  # e.g. "Failed to subscribe" "subscription limit reached": a refusal
            raise ConnectionError(f"coinbase: {msg.get('message')} {msg.get('reason') or ''}".strip())
        if kind == "subscriptions":  # the whole list after each change
            self.confirmed = frozenset(p.replace("-", "") for ch in msg.get("channels") or []
                                       if isinstance(ch, dict) and ch.get("name") == "matches"
                                       for p in ch.get("product_ids") or [])
            return []
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
    dynamic: bool = False  # as CoinbaseStream
    asset: str = "crypto"
    authed: bool = True
    confirmed: frozenset = frozenset()

    def subscribe(self, coins) -> list[str]:
        self.confirmed = frozenset()
        return [json.dumps({"event": "bts:subscribe", "data": {"channel": f"live_trades_{c.lower()}usd"}})
                for c in coins]

    def change(self, add, drop) -> list[str]:
        return [json.dumps({"event": f"bts:{kind}", "data": {"channel": f"live_trades_{t.lower()}"}})
                for kind, pairs in (("unsubscribe", drop), ("subscribe", add)) for t in sorted(pairs)]

    def parse(self, msg: dict) -> list[tuple[str, dict]]:
        event = msg.get("event")
        if event == "bts:request_reconnect":
            raise Reconnect("bitstamp asked for a new connection")
        if event == "bts:error":
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            raise ConnectionError(f"bitstamp: {data.get('message') or msg}")
        if event in ("bts:subscription_succeeded", "bts:unsubscription_succeeded"):
            pair = str(msg.get("channel") or "").removeprefix("live_trades_").upper()
            self.confirmed = (self.confirmed | {pair}) if event == "bts:subscription_succeeded" else (self.confirmed - {pair})
            return []
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
    dynamic: bool = True  # symbols from the charts (and watchlists: wanted_stocks), not LIVE_COINS
    asset: str = "stock"
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


def open_symbols(store, now_ms: int) -> list[str]:
    """The symbols open on a chart page now (noted within OPEN_FRESH_MS), most recently noted first."""
    opened = store.kv_json(OPEN_KEY, {})
    opened = sorted(((at, t) for t, at in opened.items()
                     if isinstance(at, (int, float)) and now_ms - at <= OPEN_FRESH_MS), reverse=True) \
        if isinstance(opened, dict) else []
    return [t.upper() for _, t in opened if isinstance(t, str) and 0 < len(t) <= 32]


def wanted_stocks(store, now_ms: int, watchlists: bool = True) -> tuple[str, ...]:
    """The stock symbols to stream, at most MAX_STOCKS: those open on a chart (most recently
    noted first), then, with `watchlists` (LIVE_STREAMS=always), the rail's watchlists in their
    order. Only symbols collected as stocks."""
    out = open_symbols(store, now_ms)
    for lst in store.kv_json(WATCHLISTS_KEY, []) if watchlists else ():
        out += (lst.get("tickers") or []) if isinstance(lst, dict) else []
    out = [t.upper() for t in out if isinstance(t, str) and 0 < len(t) <= 32]
    stocks = (t for t in dict.fromkeys(out) if any(w["asset"] == "stock" for w in store.watchlist(ticker=t)))
    return tuple(t for _, t in zip(range(MAX_STOCKS), stocks))


def wanted_coins(store, now_ms: int, coins) -> tuple[str, ...]:
    """The pairs to stream (BTCUSD) when the streams follow the charts: the coins of `coins`
    (LIVE_CRYPTO) open on a chart now."""
    pairs = {f"{c}USD" for c in coins}
    return tuple(t for t in dict.fromkeys(open_symbols(store, now_ms)) if t in pairs)


@dataclass
class Recorder:
    db_path: Path
    coins: tuple[str, ...] = LIVE_COINS
    sources: tuple[str, ...] = ("coinbase", "bitstamp")
    stock_keys: tuple[str, str] | None = field(default=None, repr=False)  # Alpaca's: the IEX stream
    locks: list = field(default_factory=list)  # the lock files it holds (cli.cmd_live): lost, it stops
    log: object = print
    connect: object = None  # WebSocket factory (tests pass a fake)
    stop: threading.Event = field(default_factory=threading.Event)
    # LIVE_STREAMS=selected (the app's default): every stream follows the symbols open on a chart,
    # and with none open nothing is connected. False (always): the coins of `coins` all the time,
    # and stocks open on a chart or on the rail's watchlists.
    selected_only: bool = False

    def __post_init__(self):
        self.streams = [STREAMS[s](dynamic=self.selected_only) for s in self.sources] if self.coins else []
        if self.stock_keys:
            self.streams.append(AlpacaIexStream(*self.stock_keys))
        self._lock = threading.Lock()
        self._buffer: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._since: dict[str, int] = {}  # source -> connected since (ms)
        self._heard: dict[str, int] = {}  # source -> last heard from (ms): a message or a keepalive answer
        self._status: dict[str, dict] = {s.source: {"connected": False, "pairs": {}} for s in self.streams}
        self._wanted: tuple[str, ...] = ()  # stock symbols to stream (wanted_stocks)
        self._wanted_coins: tuple[str, ...] = ()  # pairs to stream when following the charts (wanted_coins)
        self._subs: dict[str, dict[str, int]] = {s.source: {} for s in self.streams}  # symbol -> asked for since (ms)
        self._rules: dict[str, tuple[float, dict, str]] = {}  # feed -> (read at, its condition rules, their digest)
        self._derivations: dict[tuple[str, str, int, str | None], int] = {}  # (feed, symbol, day, rules digest) -> id
        self._built: dict[int, int] = defaultdict(int)
        self._touched: set[str] = set()  # symbols with trades stored since the last derive_recorded
        if self.connect is None:
            from .wsclient import WebSocket

            self.connect = lambda url: WebSocket(url, timeout=RECV_TIMEOUT)

    # -- streams ---------------------------------------------------------------------------
    def _follow(self, stream, ws) -> bool:
        """Bring a stream's symbols in line with those wanted (once the server took the keys).
        False when none are wanted any more."""
        with self._lock:
            want, have = set(self._wanted_for(stream)), self._subs[stream.source]
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
        from .storage import Store

        try:
            with Store(self.db_path) as store:  # kills are read and written from this thread: its own connection
                self._stream_loop(stream, store)
        except Exception as e:  # noqa: BLE001 - shown down, not left "connected"; run() starts it again
            with self._lock:
                self._since.pop(stream.source, None)
                self._subs[stream.source] = {}
                self._status[stream.source].update(connected=False,
                                                   error=f"stream stopped: {type(e).__name__}: {e}"[:200])
            self.log(f"live: the {stream.source} stream stopped ({type(e).__name__}: {e})")

    def _start_stream(self, stream) -> threading.Thread:
        t = threading.Thread(target=self._run_stream, args=(stream,), daemon=True, name=f"live-{stream.source}")
        t.start()
        return t

    def _supervise(self, threads: dict, restarts: dict, now: float) -> None:
        """Start again each stream thread that ended while the recorder runs (an error got out of
        its loop: it had stopped recording while its status still said connected). Not at once:
        RESTART_SECONDS after it is found ended, doubling up to MAX_BACKOFF while it keeps ending,
        and from RESTART_SECONDS again after one that ran STABLE_SECONDS: a thread that ends as it
        starts must not become a reconnect storm. `restarts`: {source: {wait, at, started}}."""
        for s in self.streams:
            if self.stop.is_set() or threads[s.source].is_alive():
                continue
            r = restarts.setdefault(s.source, {"wait": RESTART_SECONDS, "at": None, "started": now})
            if r["at"] is None:  # just found ended
                if now - r["started"] >= STABLE_SECONDS:
                    r["wait"] = RESTART_SECONDS  # it had run a while: a new failure, not more of the same
                r["at"] = now + r["wait"]
                self.log(f"live: the {s.source} stream ended; started again in {r['wait']:.0f} s")
            elif now >= r["at"]:
                threads[s.source] = self._start_stream(s)
                r.update(at=None, started=now, wait=min(MAX_BACKOFF, r["wait"] * 2))

    def _record(self, write, *args, **kwargs):
        """A stream thread's write (an outcome, a kill): a database that refuses it (locked, full)
        is logged and the stream goes on; it must not end the thread. Returns what the write
        returned, or None when it failed."""
        try:
            return write(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.log(f"live: not recorded ({type(e).__name__}: {e})")
            return None

    def _killed(self, stream, store) -> bool:
        """Whether the stream is killed (killswitch): then it is shown so and waited on, a while.
        A kill that cannot be read (the database refused) counts as one until it can be."""
        try:
            k = killswitch.killed(store, stream.source, "live")
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._status[stream.source].update(connected=False, error=f"kill state not read: {e}"[:200])
            self.log(f"live: {stream.source}: its kill state could not be read ({type(e).__name__}: {e}); not connected")
            self.stop.wait(KILL_CHECK_SECONDS)
            return True
        with self._lock:
            self._status[stream.source]["killed"] = k
            if k:
                self._status[stream.source].update(
                    connected=False, error=f"killed until {k['until'][11:16]} UTC: {k['why']}"[:200])
        if k:
            left = (datetime.fromisoformat(k["until"]) - datetime.now(timezone.utc)).total_seconds()
            self.stop.wait(max(1.0, min(KILL_CHECK_SECONDS, left)))
        return bool(k)

    def _stream_loop(self, stream, store) -> None:
        backoff, failures = 1.0, 0
        flaps = killswitch.Flapping()
        dynamic = getattr(stream, "dynamic", False)
        while not self.stop.is_set():
            if self._killed(stream, store):
                continue
            if dynamic and not self._wanted_for(stream):  # nothing to stream: no connection
                with self._lock:
                    self._status[stream.source].update(connected=False, error=None, idle=True)
                self.stop.wait(WANT_SECONDS)
                continue
            ws, connected_at = None, None
            try:
                ws = self.connect(stream.url)
                connected_at = time.monotonic()  # the server took the connection: a drop from here on can flap
                for m in stream.subscribe(() if dynamic else self.coins):
                    ws.send(m)
                with self._lock:
                    self._since[stream.source] = int(time.time() * 1000)
                    self._status[stream.source].update(connected=True, since=datetime.now(timezone.utc).isoformat(),
                                                       error=None, idle=False)
                what = "stocks" if stream.asset == "stock" else "coins open on a chart" if dynamic else f"{len(self.coins)} coins"
                self.log(f"live: {stream.source} connected ({what})")
                self._record(store.feed_event, stream.source, "live", "connected")
                misses, pongs = 0, 0
                self._heard[stream.source] = int(time.time() * 1000)
                while not self.stop.is_set():
                    if dynamic and not self._follow(stream, ws):
                        with self._lock:  # none wanted any more: the connection is closed
                            self._since.pop(stream.source, None)
                            self._subs[stream.source] = {}
                            self._status[stream.source].update(connected=False, error=None, idle=True)
                        self.log(f"live: {stream.source} closed: nothing of it open on a chart")
                        break
                    try:
                        raw = ws.recv()
                    except socket.timeout:
                        if getattr(ws, "pongs", 0) != pongs:  # the server answered the last ping
                            pongs, misses = ws.pongs, 0
                            self._heard[stream.source] = int(time.time() * 1000)
                        misses += 1
                        if not stream.keepalive or misses > 2:
                            raise ConnectionError("no messages")
                        ws.ping() if stream.keepalive == PING else ws.send(stream.keepalive)
                        continue
                    misses = 0
                    self._heard[stream.source] = int(time.time() * 1000)
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
            except Exception as e:  # noqa: BLE001 - any error: down, logged, tried again (a dead thread stayed "connected")
                if not isinstance(e, (OSError, ConnectionError, ValueError, KeyError)):
                    e = ConnectionError(f"{type(e).__name__}: {e}")
                held = connected_at is not None and time.monotonic() - connected_at >= STABLE_SECONDS
                if held:
                    backoff, failures = 1.0, 0  # it had been working: a new failure, not more of the same
                failures += 1
                refused = killswitch.is_refusal(e)
                if connected_at is not None or refused:
                    self._record(store.feed_event, stream.source, "live", "refused" if refused else "dropped",
                                 seconds=round(time.monotonic() - connected_at, 1) if connected_at is not None else None,
                                 detail=str(e))
                # A flap is a connection the server took and then dropped; one never made (no network,
                # DNS) is not the server saying no: it only waits longer each time (backoff below).
                flapped = connected_at is not None and not held and flaps.drop(time.monotonic())
                if not self.stop.is_set() and (refused or flapped):
                    why = str(e) if refused else (f"{killswitch.FLAP_LIMIT} connections dropped within "
                                                  f"{STABLE_SECONDS:.0f} s of connecting, the last: {e}")
                    kill = self._record(killswitch.strike, store, stream.source, "live", why, log=self.log)
                    with self._lock:
                        self._since.pop(stream.source, None)
                        self._subs[stream.source] = {}
                    flaps.clear()
                    backoff, failures = 1.0, 0
                    if kill is None:  # the kill could not be written: this stream stays off all the same
                        with self._lock:
                            self._status[stream.source].update(connected=False, error=f"off (kill not recorded): {why}"[:200])
                        self.stop.wait(killswitch.KILL_MINUTES[0] * 60)
                    continue  # the top of the loop waits the kill out
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

    def set_wanted(self, tickers: tuple[str, ...], coins: tuple[str, ...] | None = None) -> None:
        """The stocks to stream, and (following the charts) the pairs."""
        with self._lock:
            self._wanted = tuple(tickers)[:MAX_STOCKS]
            if coins is not None:
                self._wanted_coins = tuple(coins)

    def _wanted_for(self, stream) -> tuple[str, ...]:
        return self._wanted if stream.asset == "stock" else self._wanted_coins

    # -- storing ---------------------------------------------------------------------------
    def _derivation(self, store, stream, ticker: str, day: int, rules: dict, digest: str | None) -> int:
        """The derivation record of a symbol's bars built on one UTC day (`day`: the bars' own, not
        the flush's) by one set of condition rules (`digest`; None for a stream without them)."""
        from . import provenance

        key = (stream.source, ticker, day, digest)
        if key not in self._derivations:
            settings = {"steps": ["second", "minute"], "live": True, "flush_seconds": FLUSH_SECONDS,
                        "method": getattr(stream, "method", "trades from the exchange's public stream; bars rebuilt "
                                          "as trades arrive (a forming bar changes until it closes)")}
            if digest is not None:
                settings |= {"rules": digest, "rule_count": len(rules)}
            self._derivations[key] = provenance.record(store, "bars-from-ticks", f"{stream.source}:{ticker}", settings,
                                                       {"stream": stream.url, "ticks": f"{stream.source}:{ticker}"},
                                                       (day * DAY_MS, (day + 1) * DAY_MS))
        return self._derivations[key]

    def _tape_rules(self, store, feed: str) -> tuple[dict, str]:
        """The trade condition rules (ticks.py) a feed's stock bars are built by, reread hourly, and
        their digest (what a derivation records of them)."""
        from . import provenance

        at, rules, digest = self._rules.get(feed, (0.0, {}, ""))
        if not at or time.monotonic() - at > RULES_SECONDS:
            rules = store.condition_rules(feed)
            self._rules[feed] = (time.monotonic(), rules, provenance.digest(sorted(rules.items())))
        return self._rules[feed][1], self._rules[feed][2]

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
            heard = dict(self._heard)
            subs = {k: dict(v) for k, v in self._subs.items()}
        try:
            return self._store(store, batch, since, heard, subs, now_ms)
        except Exception:
            with self._lock:  # kept for the next flush (a locked database, say), ahead of what came since
                for key, trades in batch.items():
                    self._buffer[key][:0] = trades
                self._shed()
            raise

    def _shed(self) -> int:
        """Past MAX_BUFFERED_BYTES of trades waiting (writes failing flush after flush), drop the oldest of
        each pair in proportion, and move each stream's recorded-since past what was dropped: the
        ledger then claims none of that time, and the sync downloads it. Called holding the lock.
        Returns trades dropped."""
        total, cap = sum(len(v) for v in self._buffer.values()), max(1, MAX_BUFFERED_BYTES // TRADE_BYTES)
        if total <= cap:
            return 0
        excess, dropped, newest = total - cap, 0, {}
        for (source, ticker), trades in sorted(self._buffer.items(), key=lambda kv: -len(kv[1])):
            n = min(len(trades), -(-excess * len(trades) // total))  # this pair's share, rounded up
            if n:
                cut = trades[:n]
                newest[source] = max(newest.get(source, 0), max(t["sip_timestamp"] for t in cut) // 1_000_000 + 1)
                self._buffer[(source, ticker)] = trades[n:]
                dropped += n
        for source, ms in newest.items():
            if source in self._since:
                self._since[source] = max(self._since[source], ms)
            self._status[source]["dropped"] = self._status[source].get("dropped", 0) + dropped
        self.log(f"live: {dropped:,} trades dropped: the database refused writes for a while "
                 f"(at most {cap:,}, {MAX_BUFFERED_BYTES // 2**20} MB, are held); that time is left for the sync to download")
        return dropped

    def _store(self, store, batch, since, heard, subs, now_ms) -> int:
        from .ticks import build_bars

        stored = 0
        streams = {s.source: s for s in self.streams}
        withdrawn: dict[tuple[str, str], list[int]] = {}  # the times (ns) of the stored trades taken out
        for (source, ticker), trades in batch.items():
            standing, gone = _settle(trades)
            if gone:  # first: a correction that keeps its trade's id must not go with the original
                since_ns = min(t["sip_timestamp"] for t in trades) - DAY_MS * 1_000_000
                withdrawn[(source, ticker)] = store.withdraw_trades(source, ticker, gone, since_ns)
            if standing:
                stored += store.upsert_trades(source, ticker, standing)
        for source, start in since.items():
            end = min(now_ms, heard.get(source, start) + HEARD_SLACK_MS)  # not past the last time it was heard
            for ticker, t0 in self._streamed(streams[source], start, subs.get(source, {})).items():
                if t0 < end:
                    store.add_coverage(source, ticker, "trades", t0, end)
        for (source, ticker), trades in batch.items():
            stream = streams[source]
            lo_ns = min([t["sip_timestamp"] for t in trades] + withdrawn.get((source, ticker), []))
            gone = sum(1 for t in trades if t.get("withdrawn"))
            lo = lo_ns // 1_000_000 // 60_000 * 60_000  # the minute it starts in
            if gone:  # a bar whose only trades were withdrawn must go
                store.drop_built_between(source, ticker, ("second", "minute"), lo, now_ms)
            # up to the newest trade of the batch too: an exchange clock ahead of this one's must not
            # leave a trade out of its minute (the next flush starts at its own first trade's minute)
            hi_ns = max(now_ms * 1_000_000, max(t["sip_timestamp"] for t in trades) + 1)
            ticks = store.trades_between(source, ticker, lo * 1_000_000, hi_ns)
            rules, digest = self._tape_rules(store, source) if getattr(stream, "tape_rules", False) else ({}, None)
            prior = store.close_before(source, ticker, "minute", lo)  # an odd-lot-only first minute keeps it
            dids = set()
            for step, timespan in ((1_000_000_000, "second"), (60_000_000_000, "minute")):
                bars, _ = build_bars(ticks, step, rules, prior_close=prior)
                days: dict[int, list[dict]] = defaultdict(list)  # a batch over midnight (UTC): each day's record
                for b in bars:
                    days[b["t"] // DAY_MS].append(b)
                for day, part in days.items():
                    did = self._derivation(store, stream, ticker, day, rules, digest)
                    self._built[did] += store.upsert_built_bars(source, ticker, timespan, 1, part, did)
                    dids.add(did)
                store.add_coverage(source, ticker, timespan, lo, now_ms, origin="ticks")
            for did in dids:
                store.set_derivation_rows(did, self._built[did])
            self._touched.add(ticker)
            last = next((t for t in reversed(trades) if not t.get("withdrawn")), None)
            with self._lock:
                pair = self._status[source]["pairs"].setdefault(ticker, {"trades": 0})
                pair["trades"] += len(trades) - 2 * gone
                if last:
                    pair["last"] = last["sip_timestamp"] // 1_000_000
                    pair["price"] = last["price"]
        with self._lock:
            for s in self.streams:
                if getattr(s, "dynamic", False):
                    self._status[s.source]["symbols"] = sorted(set(subs.get(s.source, {})) & s.confirmed)
            status = json.loads(json.dumps(self._status))
            wanted = list(self._wanted)
            coins = [t[:-3] for t in self._wanted_coins] if self.selected_only else list(self.coins)
        store.kv_set(RECORDER_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.kv_set(STATUS_KEY, json.dumps({"at": now_ms, "coins": coins, "stocks": wanted, "streams": status,
                                             "selectedOnly": self.selected_only}))
        return stored

    def run(self) -> None:
        from .storage import Store

        threads = {s.source: self._start_stream(s) for s in self.streams}
        restarts = {s.source: {"wait": RESTART_SECONDS, "at": None, "started": time.monotonic()} for s in self.streams}
        follows = any(getattr(s, "dynamic", False) for s in self.streams)  # streams whose symbols change
        # the one process that opens tick files in bars-only mode: it records the streams' trades
        with Store(self.db_path, ticks=True) as store:
            total, last_log, last_derive, last_want, last_lock = 0, time.time(), time.time(), 0.0, time.time()
            next_reap = time.time() + FIRST_REAP_SECONDS
            while True:
                if self.locks and time.time() >= next_reap:  # the lock holder: one recorder, strictly
                    next_reap = time.time() + REAP_SECONDS
                    if self.holds_locks():  # one that lost its lock ends nobody: it stops (below)
                        reap_strays(self.log)
                if self.locks and time.time() - last_lock > LOCK_CHECK_SECONDS:
                    last_lock = time.time()
                    if not self.holds_locks():
                        self.log("live: another recorder holds this one's lock now; stopping")
                        self.stop.set()
                try:
                    self._supervise(threads, restarts, time.monotonic())  # a stream thread that ended: again
                    if follows and time.time() - last_want > WANT_SECONDS:
                        now_ms = int(time.time() * 1000)
                        self.set_wanted(wanted_stocks(store, now_ms, watchlists=not self.selected_only),
                                        wanted_coins(store, now_ms, self.coins) if self.selected_only else None)
                        last_want = time.time()
                    if self.stop.wait(FLUSH_SECONDS):
                        break
                    total += self.flush(store)
                    if time.time() - last_derive > DERIVE_SECONDS:
                        self.derive_recorded(store)
                        last_derive = time.time()
                except Exception as e:  # noqa: BLE001 - a locked database, say: logged, the trades kept, tried again
                    self.log(f"live: {type(e).__name__}: {e}; trying again")
                    self.stop.wait(FLUSH_SECONDS)
                if time.time() - last_log > 600:
                    self.log(f"live: {total:,} trades recorded in the last 10 min")
                    total, last_log = 0, time.time()
            try:
                self.flush(store)
            except Exception as e:  # noqa: BLE001
                self.log(f"live: the last flush failed: {e}")

    def derive_recorded(self, store) -> int:
        """Build the frames above a minute for the symbols this recorder stored trades of since it
        last did: nothing streamed, nothing read. (It rebuilt every coin's each minute, and so read
        what the sync's backfill changed, 0.5 to 1 GB a minute with no stream connected; the sync
        builds what it downloads itself.) Returns bars built."""
        touched, self._touched = self._touched, set()
        if not touched:
            return 0
        from . import derive

        try:
            return derive.run(store, sources={s.source for s in self.streams}, tickers=touched, log=lambda *_: None)
        except Exception:
            self._touched |= touched  # tried again with the next
            raise

    def holds_locks(self) -> bool:
        """Whether every lock this recorder took still names it."""
        from .proc import owns

        return all(owns(p) for p in self.locks)


# -- one recorder at a time, on the database and on the machine --------------------------------
# Every recorder opens the same exchange streams from this machine's IP address: a second one,
# even on another database (a test copy), doubles the connections, which the exchanges refuse
# ("subscription limit reached") and can answer by banning the address. So besides its database's
# lock, a recorder holds a machine-wide one (MASSIVE_LIVE_LOCK to move it, as the tests do).

def lock_path(db_path: Path) -> Path:
    return db_path.with_name("massive-live.lock")


def machine_lock() -> Path:
    return Path(os.environ.get("MASSIVE_LIVE_LOCK") or Path.home() / ".massive-collate" / "live.lock")


def running(db_path: Path) -> bool:
    """Whether a recorder runs for this database, or any recorder on this machine."""
    from .proc import holder

    return any(holder(p) for p in (lock_path(db_path), machine_lock()))


def take_locks(db_path: Path) -> list[Path] | None:
    """Take both locks for this process, atomically (proc.acquire): None, holding neither, when
    another recorder holds one."""
    from .proc import acquire

    taken = []
    for path in (lock_path(db_path), machine_lock()):
        if not acquire(path):
            release_locks(taken)
            return None
        taken.append(path)
    return taken


def release_locks(locks: list[Path]) -> None:
    """Remove the locks this process holds (not one another recorder took since)."""
    from .proc import release

    for path in locks:
        release(path)


def recorders() -> list[int]:
    """Recorder processes on this machine, other than this process and its own launcher."""
    from .proc import ancestors, processes

    rows = processes()
    mine = ancestors(os.getpid(), rows)
    return [pid for pid, _, cmd in rows if pid not in mine and RECORDER_COMMAND.search(cmd.strip().strip('"'))]


def reap_strays(log=print, others=None, terminate=None) -> int:
    """End every other recorder on this machine (run by the one holding the machine lock): a
    duplicate doubles the connections to the exchanges. Returns how many were ended."""
    from .proc import terminate as end

    strays = recorders() if others is None else others
    ended = sum(1 for pid in strays if (terminate or end)(pid))
    if ended:
        log(f"live: ended {ended} other recorder process{'es' if ended > 1 else ''} (one recorder per machine)")
    return ended


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
