"""Brokers, each a plugin behind one interface (docs/ARCHITECTURE.md, "Trading").

A broker plugin gives its account (cash, equity, profit and loss), positions and orders, and places
and cancels orders. Paper (paper.py) is simulated here. A live broker (Interactive Brokers first,
planned) would send orders to a real account: none is built, and live trading stays off until it is
decided otherwise (LIVE_TRADING); even then each order is the user's own, placed in this app.
"""
from __future__ import annotations

from typing import Protocol

SIDES = ("buy", "sell")
TYPES = ("market", "limit", "stop")
TIFS = ("day", "gtc")


class LiveTradingOff(RuntimeError):
    """Live trading is off: no order goes to a real account."""


class Broker(Protocol):
    key: str
    label: str
    live: bool  # orders go to a real account

    def summary(self, now_ms: int | None = None) -> dict: ...  # account, positions, orders
    def place(self, ticker: str, source: str, side: str, qty: float, type: str, limit: float | None = None,
              stop: float | None = None, tif: str = "day", now_ms: int | None = None) -> dict: ...
    def cancel(self, order_id: int, now_ms: int | None = None) -> dict: ...


# Every broker the Trading view lists: built ones, and those planned (shown, not usable).
PLANNED = [
    {"key": "ibkr", "label": "Interactive Brokers", "live": True,
     "status": "planned: not connected (live trading is off)"},
]


def check_order(side: str, qty, type: str, limit, stop, tif: str) -> tuple[float, float | None, float | None]:
    """An order's fields, checked: (qty, limit, stop)."""
    if side not in SIDES:
        raise ValueError(f"side: {' or '.join(SIDES)}")
    if type not in TYPES:
        raise ValueError(f"type: one of {', '.join(TYPES)}")
    if tif not in TIFS:
        raise ValueError(f"time in force: {' or '.join(TIFS)}")
    qty = float(qty)
    if not 0 < qty < 1e12:
        raise ValueError("quantity: more than 0")
    limit = None if limit in (None, "") else float(limit)
    stop = None if stop in (None, "") else float(stop)
    if type == "limit" and not (limit and limit > 0):
        raise ValueError("a limit order needs a limit price above 0")
    if type == "stop" and not (stop and stop > 0):
        raise ValueError("a stop order needs a stop price above 0")
    return qty, limit if type == "limit" else None, stop if type == "stop" else None
