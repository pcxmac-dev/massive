"""Paper trading: a simulated account filled from the stored bars (docs/ARCHITECTURE.md, "Trading").

Orders are matched against the finest bars stored after each was placed, the first that reach it,
by the backtests' rules: a market order at the next bar's open; a limit or stop at its price, or at
the open when a bar opens beyond it. A day order not filled by its session's close expires, once
the stored bars reach that close (a session not stored yet may still fill it). Matching runs
whenever the account is looked at, from the stored bars, so an order placed while the view was
closed fills as the bars since allowed. Its own tables in the main file; a reset starts a new account
and keeps the old one's record.
"""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .brokers import check_order

ET = ZoneInfo("America/New_York")
DEFAULT_CASH = 100_000.0
MATCH_PAGE = 5000  # bars read at a time while looking for an order's fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cash        REAL    NOT NULL,   -- starting cash
    fee_bps     REAL    NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,   -- ms
    closed_at   INTEGER             -- ms: a reset started another (this one's record is kept)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     INTEGER NOT NULL,
    ticker      TEXT    NOT NULL,
    source      TEXT    NOT NULL,   -- the feed its fills are read from ('*': the chart's merged feeds)
    side        TEXT    NOT NULL,   -- buy | sell
    qty         REAL    NOT NULL,
    type        TEXT    NOT NULL,   -- market | limit | stop
    limit_px    REAL,
    stop_px     REAL,
    tif         TEXT    NOT NULL,   -- day | gtc
    placed_at   INTEGER NOT NULL,   -- ms
    status      TEXT    NOT NULL,   -- working | filled | cancelled | expired | rejected
    done_at     INTEGER,            -- ms: when it filled (the bar it filled in), was cancelled or expired
    fill_px     REAL,
    fee         REAL,
    looked_to   INTEGER             -- ms: bars before this have been looked at for it
);
CREATE INDEX IF NOT EXISTS paper_orders_account ON paper_orders (account, id);
"""

# (source, ticker, after_ms, until_ms) -> the finest bars stored in [after, until), oldest first, at
# most MATCH_PAGE: columns t (ms bar start), open, high, low, close, and optionally settled (False
# where only a feed of one exchange's share holds the bar, which a feed of the whole market may
# still replace); and the step of those bars (ms).
BarsAfter = Callable[[str, str, int, int], tuple[pd.DataFrame, int]]
# (source, ticker) -> where the symbol's stored bars end (ms, any session; a feed of the whole
# market's where one holds it), or None: a day order expires once they reach its close.
StoredTo = Callable[[str, str], int | None]


def _now() -> int:
    return int(time.time() * 1000)


def day_end(placed_ms: int, asset: str = "stock") -> int:
    """When a day order placed at `placed_ms` expires: its session's close (US stocks: the regular
    close of that trading day, or of the next one when placed after it); for markets trading round
    the clock, the end of the UTC day."""
    if asset not in ("stock", "index", "etf"):
        d = datetime.fromtimestamp(placed_ms / 1000, timezone.utc).date() + timedelta(days=1)
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    from ..sessions import is_nyse_holiday, nyse_early_close

    at = datetime.fromtimestamp(placed_ms / 1000, ET)
    d: date = at.date()
    for _ in range(14):
        if d.weekday() < 5 and not is_nyse_holiday(d):
            close = datetime(d.year, d.month, d.day, 13 if nyse_early_close(d) else 16, tzinfo=ET)
            if close > at:
                return int(close.timestamp() * 1000)
        d += timedelta(days=1)
    return placed_ms + 86_400_000


def fill_in(order: dict, bars: pd.DataFrame) -> tuple[int, float] | None:
    """The first bar reaching the order, and its fill price (the module docstring's rules)."""
    side, kind = order["side"], order["type"]
    for t, o, h, l, _c in bars[["t", "open", "high", "low", "close"]].itertuples(index=False, name=None):  # noqa: E741
        if kind == "market":
            return int(t), float(o)
        if kind == "limit":
            px = order["limit_px"]
            if side == "buy" and l <= px:
                return int(t), float(min(o, px))
            if side == "sell" and h >= px:
                return int(t), float(max(o, px))
        else:
            px = order["stop_px"]
            if side == "buy" and h >= px:
                return int(t), float(max(o, px))
            if side == "sell" and l <= px:
                return int(t), float(min(o, px))
    return None


class PaperBook:
    """The paper account: a Broker (brokers.py) over its tables and the stored bars."""

    key, label, live = "paper", "Paper (simulated)", False

    def __init__(self, conn: sqlite3.Connection, bars_after: BarsAfter,
                 last_price: Callable[[str, str], float | None], asset: Callable[[str], str] = lambda _t: "stock",
                 stored_to: StoredTo = lambda _s, _t: None):
        self.conn, self.bars_after, self.last_price, self.asset = conn, bars_after, last_price, asset
        self.stored_to = stored_to
        conn.executescript(SCHEMA)
        self._cols = [d[0] for d in conn.execute("SELECT * FROM paper_orders LIMIT 0").description]

    # -- the account -------------------------------------------------------------------------
    def account(self) -> dict:
        row = self.conn.execute("SELECT id, cash, fee_bps, created_at FROM paper_accounts WHERE closed_at IS NULL "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            with self.conn:
                self.conn.execute("INSERT INTO paper_accounts (cash, fee_bps, created_at) VALUES (?, 0, ?)",
                                  (DEFAULT_CASH, _now()))
            return self.account()
        return {"id": row[0], "cash": row[1], "feeBps": row[2], "createdAt": row[3]}

    def reset(self, cash: float = DEFAULT_CASH, fee_bps: float = 0.0, now_ms: int | None = None) -> dict:
        """A new account (the old one's orders and record are kept, closed)."""
        cash, fee_bps = float(cash), float(fee_bps)
        if not (0 < cash < 1e12 and math.isfinite(cash)) or not 0 <= fee_bps <= 1000:
            raise ValueError("starting cash above 0, fee 0 to 1,000 basis points")
        now = now_ms or _now()
        with self.conn:
            self.conn.execute("UPDATE paper_orders SET status='cancelled', done_at=? WHERE status='working' AND account IN "
                              "(SELECT id FROM paper_accounts WHERE closed_at IS NULL)", (now,))
            self.conn.execute("UPDATE paper_accounts SET closed_at=? WHERE closed_at IS NULL", (now,))
            self.conn.execute("INSERT INTO paper_accounts (cash, fee_bps, created_at) VALUES (?, ?, ?)", (cash, fee_bps, now))
        return self.account()

    # -- orders --------------------------------------------------------------------------------
    def place(self, ticker: str, source: str, side: str, qty: float, type: str, limit: float | None = None,
              stop: float | None = None, tif: str = "day", now_ms: int | None = None) -> dict:
        qty, limit, stop = check_order(side, qty, type, limit, stop, tif)
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise ValueError("a symbol is needed")
        now = now_ms or _now()
        acct = self.account()["id"]
        # a series it cannot fill from (the composite, a continuous future) is refused here, not
        # stored to break every later match (ValueError, as the route answers 400)
        self.bars_after(source or "*", ticker, now, now)
        with self.conn:
            oid = self.conn.execute(
                "INSERT INTO paper_orders (account, ticker, source, side, qty, type, limit_px, stop_px, tif, placed_at, "
                "status, looked_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'working', ?)",
                (acct, ticker, source or "*", side, qty, type, limit, stop, tif, now, now)).lastrowid
        self.match(now)
        return self.order(oid)

    def cancel(self, order_id: int, now_ms: int | None = None) -> dict:
        self.match(now_ms)  # a fill the bars already allowed stands
        with self.conn:
            n = self.conn.execute("UPDATE paper_orders SET status='cancelled', done_at=? WHERE id=? AND status='working'",
                                  (now_ms or _now(), int(order_id))).rowcount
        if not n:
            raise LookupError(f"no working paper order {order_id}")
        return self.order(order_id)

    def order(self, order_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM paper_orders WHERE id=?", (int(order_id),)).fetchone()
        if row is None:
            raise LookupError(f"no paper order {order_id}")
        return self._row(row)

    def _row(self, row) -> dict:
        return dict(zip(self._cols, row))

    def match(self, now_ms: int | None = None) -> int:
        """Fill or expire the working orders from the bars stored since each was last looked at.
        Returns orders filled."""
        now = now_ms or _now()
        filled = 0
        working = [self._row(r) for r in self.conn.execute("SELECT * FROM paper_orders WHERE status='working' ORDER BY id")]
        for o in working:
            end = day_end(o["placed_at"], self.asset(o["ticker"])) if o["tif"] == "day" else None
            until = min(now, end) if end is not None else now
            # `at` (looked_to) moves past a bar only when it is over and settled, and only while
            # every bar before it was: a bar not over yet may still reach the order, and one only a
            # feed of one exchange's share holds may be replaced by the whole market's (IEX's
            # session, until the sync stores it). Those are read again next time; the read goes on
            # past them (`read`) for a fill they already allow.
            at = o["looked_to"] or o["placed_at"]
            read, steady, hit = at, True, None
            try:
                while read < until and hit is None:
                    bars, step = self.bars_after(o["source"], o["ticker"], read, until)
                    if bars.empty:
                        break
                    hit = fill_in(o, bars)
                    t = bars["t"].to_numpy(dtype="int64")
                    final = t + step <= now
                    if "settled" in bars:
                        final &= bars["settled"].to_numpy(dtype=bool)
                    lead = len(final) if final.all() else int(np.argmin(final))  # the bars over and settled, from the first
                    if steady and lead:
                        at = int(t[lead - 1]) + step
                    steady = steady and lead == len(final)
                    read = int(t[-1]) + 1
                    if len(bars) < MATCH_PAGE:
                        break
            except ValueError:  # a series it can no longer fill from: this order only, the rest still match
                with self.conn:
                    self.conn.execute("UPDATE paper_orders SET status='rejected', done_at=? WHERE id=? AND status='working'",
                                      (now, o["id"]))
                continue
            stored = self.stored_to(o["source"], o["ticker"]) if end is not None and now >= end else None
            with self.conn:
                if hit is not None:
                    t, px = hit
                    fee = o["qty"] * px * self.account()["feeBps"] / 10_000
                    self.conn.execute("UPDATE paper_orders SET status='filled', done_at=?, fill_px=?, fee=?, looked_to=? "
                                      "WHERE id=? AND status='working'", (t, px, fee, at, o["id"]))
                    filled += 1
                elif end is not None and now >= end and (at >= end or stored is None or stored >= end):
                    # the close has passed and the bars up to it are stored (looked at to the close,
                    # or the series goes on past it): not before, or a fill they allow would be missed;
                    # nothing stored for it at all (a mistyped symbol): no bar can fill it
                    self.conn.execute("UPDATE paper_orders SET status='expired', done_at=?, looked_to=? WHERE id=? "
                                      "AND status='working'", (end, at, o["id"]))
                else:
                    self.conn.execute("UPDATE paper_orders SET looked_to=? WHERE id=?", (at, o["id"]))
        return filled

    # -- what the Trading view shows -----------------------------------------------------------
    def summary(self, now_ms: int | None = None, limit: int = 200) -> dict:
        self.match(now_ms)
        acct = self.account()
        rows = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM paper_orders WHERE account=? ORDER BY id DESC LIMIT ?", (acct["id"], limit))]
        fills = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM paper_orders WHERE account=? AND status='filled' ORDER BY done_at, id", (acct["id"],))]
        book: dict[str, dict] = {}
        cash, realized, fees = acct["cash"], 0.0, 0.0
        for f in fills:
            q = f["qty"] if f["side"] == "buy" else -f["qty"]
            px, fee = f["fill_px"], f["fee"] or 0.0
            p = book.setdefault(f["ticker"], {"ticker": f["ticker"], "source": f["source"], "qty": 0.0, "avg": 0.0,
                                              "realized": 0.0})
            cash -= q * px + fee
            fees += fee
            if p["qty"] == 0 or (p["qty"] > 0) == (q > 0):
                p["avg"] = (p["qty"] * p["avg"] + q * px) / (p["qty"] + q)
                p["qty"] += q
            else:
                closing = min(abs(q), abs(p["qty"]))
                gain = closing * (px - p["avg"]) * (1 if p["qty"] > 0 else -1)
                p["realized"] += gain
                realized += gain
                left = p["qty"] + q
                p["avg"] = p["avg"] if left and (left > 0) == (p["qty"] > 0) else (px if left else 0.0)
                p["qty"] = left
            p["realized"] -= fee
        positions, value, unrealized = [], 0.0, 0.0
        for p in book.values():
            if abs(p["qty"]) < 1e-12:
                continue
            last = self.last_price(p["source"], p["ticker"])
            mv = p["qty"] * last if last is not None else None
            up = p["qty"] * (last - p["avg"]) if last is not None else None
            value += mv or 0.0
            unrealized += up or 0.0
            positions.append({**p, "last": last, "value": mv, "unrealized": up})
        return {
            "account": {**acct, "start": acct["cash"], "cash": cash, "equity": cash + value, "realized": realized - fees,
                        "unrealized": unrealized, "fees": fees},
            "positions": sorted(positions, key=lambda p: p["ticker"]),
            "orders": [{k: o[k] for k in ("id", "ticker", "source", "side", "qty", "type", "limit_px", "stop_px", "tif",
                                          "placed_at", "status", "done_at", "fill_px", "fee")} for o in rows],
        }
