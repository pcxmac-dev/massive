"""Paper trading (trading/paper.py) and the Trading view's routes: orders filled from the stored
bars by the backtests' rules, day orders expiring, positions at average cost, live trading off."""
from __future__ import annotations

import json
import sqlite3
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pandas as pd
import pytest

from massive_collate import derive
from massive_collate.gui.server import WRITE_HEADER, ChartApp, make_handler
from massive_collate.storage import Store
from massive_collate.trading import paper
from massive_collate.trading.paper import PaperBook, day_end

ET = "America/New_York"
MIN = 60_000


def _ms(t: str) -> int:
    return int(pd.Timestamp(t, tz=ET).timestamp() * 1000)


T0 = _ms("2026-10-14 09:30")


class Tape:
    """Minute bars a test lays down; the book reads them as it would the store's."""

    def __init__(self):
        self.bars: list[tuple] = []

    def add(self, i: int, o: float, h: float, l: float, c: float):  # noqa: E741
        self.bars.append((T0 + i * MIN, o, h, l, c))

    def after(self, source, ticker, at, until):
        rows = [b for b in self.bars if at <= b[0] < until]
        return pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"]), MIN

    def last(self, source, ticker):
        return self.bars[-1][4] if self.bars else None

    def stored_to(self, source, ticker):
        return max(b[0] for b in self.bars) + MIN if self.bars else None


@pytest.fixture
def book():
    tape = Tape()
    b = PaperBook(sqlite3.connect(":memory:"), tape.after, tape.last, stored_to=tape.stored_to)
    b.tape = tape
    return b


def test_a_market_order_fills_at_the_next_bars_open(book):
    o = book.place("spcx", "*", "buy", 10, "market", now_ms=T0 + 30_000)  # mid-bar: the next bar's open
    assert o["status"] == "working" and o["ticker"] == "SPCX"
    book.tape.add(0, 10.0, 10.2, 9.9, 10.1)  # began before the order
    book.tape.add(1, 10.1, 10.3, 10.0, 10.2)
    book.match(T0 + 2 * MIN)
    o = book.order(o["id"])
    assert (o["status"], o["done_at"], o["fill_px"]) == ("filled", T0 + MIN, 10.1)


def test_limits_and_stops_fill_at_their_price_or_a_better_open(book):
    for i, (o, h, l, c) in enumerate([(10, 10.2, 9.9, 10.0), (10, 10.1, 9.6, 9.7), (9.0, 9.2, 8.8, 9.1), (9.5, 11, 9.4, 10.9)]):
        book.tape.add(i, o, h, l, c)
    buy = book.place("SPCX", "*", "buy", 5, "limit", limit=9.75, now_ms=T0)
    gap = book.place("SPCX", "*", "sell", 5, "stop", stop=9.5, now_ms=T0 + MIN + 1)  # bar 2 opens at 9.0, below it
    up = book.place("SPCX", "*", "buy", 1, "stop", stop=10.5, now_ms=T0)
    book.match(T0 + 4 * MIN)
    assert (book.order(buy["id"])["fill_px"], book.order(buy["id"])["done_at"]) == (9.75, T0 + MIN)
    assert (book.order(gap["id"])["fill_px"], book.order(gap["id"])["done_at"]) == (9.0, T0 + 2 * MIN)
    assert book.order(up["id"])["fill_px"] == 10.5


def test_a_bar_still_forming_is_looked_at_again(book):
    o = book.place("SPCX", "*", "sell", 1, "limit", limit=10.5, now_ms=T0)
    book.tape.add(0, 10.0, 10.2, 9.9, 10.1)
    book.match(T0 + 30_000)  # the minute is not over: its high may still reach the limit
    book.tape.bars[-1] = (T0, 10.0, 10.6, 9.9, 10.4)
    book.match(T0 + MIN)
    assert book.order(o["id"])["status"] == "filled" and book.order(o["id"])["done_at"] == T0


def test_a_day_order_expires_at_the_close_and_a_good_till_cancelled_one_does_not(book):
    assert day_end(T0) == _ms("2026-10-14 16:00")
    assert day_end(_ms("2026-10-14 17:00")) == _ms("2026-10-15 16:00")  # after the close: the next session's
    assert day_end(_ms("2026-11-26 10:00")) == _ms("2026-11-27 13:00")  # Thanksgiving: the half day after it
    day = book.place("SPCX", "*", "buy", 1, "limit", limit=1.0, now_ms=T0)
    gtc = book.place("SPCX", "*", "buy", 1, "limit", limit=1.0, tif="gtc", now_ms=T0)
    book.tape.add(0, 10, 10, 10, 10)
    book.match(_ms("2026-10-15 10:00"))
    assert book.order(day["id"])["status"] == "working"  # the rest of its session is not stored yet: it may fill
    book.tape.bars.append((_ms("2026-10-14 16:30"), 10, 10, 10, 10))  # after hours: the session is stored
    book.match(_ms("2026-10-15 10:00"))
    assert book.order(day["id"])["status"] == "expired" and book.order(day["id"])["done_at"] == _ms("2026-10-14 16:00")
    assert book.order(gtc["id"])["status"] == "working"
    assert book.cancel(gtc["id"], _ms("2026-10-15 10:01"))["status"] == "cancelled"
    with pytest.raises(LookupError):
        book.cancel(gtc["id"])


def test_positions_at_average_cost_with_realized_and_unrealized(book):
    book.reset(10_000, fee_bps=10, now_ms=T0 - MIN)
    prices = [100, 110, 90, 95]
    for i, p in enumerate(prices):
        book.tape.add(i, p, p, p, p)
    book.place("SPCX", "*", "buy", 10, "market", now_ms=T0)  # 10 @ 100
    book.place("SPCX", "*", "sell", 4, "market", now_ms=T0 + 1)  # all market orders fill at bar 0's open ...
    book.conn.execute("UPDATE paper_orders SET status='working', done_at=NULL, fill_px=NULL, looked_to=? WHERE id=2",
                      (T0 + MIN,))  # ... so the sale is moved to bar 1: 4 @ 110
    book.place("SPCX", "*", "sell", 10, "market", now_ms=T0 + 2 * MIN)  # 10 @ 90: flat, then 4 short at 90
    s = book.summary(T0 + 4 * MIN)
    (p,) = s["positions"]
    fees = (10 * 100 + 4 * 110 + 10 * 90) * 10 / 10_000
    assert (p["qty"], p["avg"], p["last"]) == (-4, 90, 95)
    assert p["unrealized"] == pytest.approx(-4 * 5)
    assert s["account"]["realized"] == pytest.approx(4 * 10 + 6 * -10 - fees)
    assert s["account"]["cash"] == pytest.approx(10_000 - 1000 + 440 + 900 - fees)
    assert s["account"]["equity"] == pytest.approx(s["account"]["cash"] - 4 * 95)


def test_a_reset_starts_a_new_account_and_keeps_the_old_record(book):
    first = book.account()["id"]
    o = book.place("SPCX", "*", "buy", 1, "limit", limit=1.0, tif="gtc", now_ms=T0)
    new = book.reset(50_000, now_ms=T0 + MIN)
    assert new["id"] != first and new["cash"] == 50_000
    assert book.order(o["id"])["status"] == "cancelled"  # kept, closed with its account
    assert book.conn.execute("SELECT closed_at FROM paper_accounts WHERE id=?", (first,)).fetchone()[0] == T0 + MIN
    assert book.summary(T0 + MIN)["orders"] == []
    with pytest.raises(ValueError):
        book.reset(0)


def test_orders_are_checked(book):
    for bad in ({"side": "hold"}, {"qty": 0}, {"type": "limit"}, {"type": "stop", "stop": -1}, {"tif": "ioc"}):
        args = {"side": "buy", "qty": 1, "type": "market", "limit": None, "stop": None, "tif": "day", **bad}
        with pytest.raises(ValueError):
            book.place("SPCX", "*", args["side"], args["qty"], args["type"], args["limit"], args["stop"], args["tif"])
    with pytest.raises(ValueError):
        book.place(" ", "*", "buy", 1, "market")


@pytest.fixture
def server(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        rows = [{"t": _ms("2026-10-14 09:30") + i * MIN, "o": 10 + i / 100, "h": 10.05 + i / 100, "l": 9.95 + i / 100,
                 "c": 10.01 + i / 100, "v": 100.0, "n": 3} for i in range(390)]
        s.upsert_bars("massive", "SPCX", "minute", 1, rows)
        s.watch("massive", ["SPCX"], "minute")
        derive.run(s, log=lambda *_: None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ChartApp(s), [0]))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield httpd.server_address[1]
        httpd.shutdown()


def _call(port, method, path, body=None, header=True):
    c = HTTPConnection("127.0.0.1", port)
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **({WRITE_HEADER: "1"} if header else {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"null") if r.getheader("Content-Type") == "application/json" else None


def test_the_trading_routes_trade_paper_only(server, monkeypatch):
    clock = {"now": _ms("2026-10-14 10:00") + 30_000}  # half a minute into 10:00
    monkeypatch.setattr(paper, "_now", lambda: clock["now"])
    status, view = _call(server, "GET", "/api/trading")
    assert status == 200 and view["liveTrading"] is False and view["account"]["cash"] == paper.DEFAULT_CASH
    assert [b["key"] for b in view["brokers"]] == ["paper", "ibkr"] and view["brokers"][1]["live"]
    status, order = _call(server, "POST", "/api/trading/orders", {"ticker": "SPCX", "side": "buy", "qty": 100, "type": "market"})
    assert status == 201 and order["status"] == "working"  # 10:01 has not begun
    clock["now"] += 5 * MIN
    status, view = _call(server, "GET", "/api/trading")
    filled = next(o for o in view["orders"] if o["id"] == order["id"])
    assert filled["status"] == "filled" and filled["fill_px"] == pytest.approx(10.31)  # 10:01's open
    status, _ = _call(server, "POST", "/api/trading/orders", {"ticker": "SPCX", "side": "buy", "qty": 1}, header=False)
    assert status == 403  # another web page cannot trade
    status, limit = _call(server, "POST", "/api/trading/orders",
                          {"ticker": "SPCX", "side": "sell", "qty": 50, "type": "limit", "limit": 99, "tif": "gtc"})
    assert status == 201 and limit["status"] == "working"
    status, gone = _call(server, "DELETE", f"/api/trading/orders/{limit['id']}")
    assert status == 200 and gone["status"] == "cancelled"
    status, view = _call(server, "GET", "/api/trading")
    (pos,) = view["positions"]
    assert pos["qty"] == 100 and pos["last"] == pytest.approx(10 + 389 / 100 + 0.01)
    status, err = _call(server, "POST", "/api/trading/orders", {"ticker": "SPCX", "side": "buy", "qty": -1})
    assert status == 400
    status, acct = _call(server, "POST", "/api/trading/reset", {"cash": 25_000})
    assert status == 200 and acct["cash"] == 25_000
