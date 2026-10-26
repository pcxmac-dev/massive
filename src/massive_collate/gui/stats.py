"""Key statistics for the side panel, computed from stored daily bars (no API calls)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

PERIODS = (("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 252))
# Crypto trades every day: its windows count calendar days, and a year is 365 sessions, not 252 (the
# 52-week range, the last period and the volatility's annualising).
EVERY_DAY = (("1W", 7), ("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365))
UTC_DAYS = ("crypto", "fx")  # their days are UTC days: so is their year's start (YTD)

DETAIL_FIELDS = (
    "name", "description", "homepage_url", "market_cap", "share_class_shares_outstanding",
    "weighted_shares_outstanding", "total_employees", "sic_description", "sic_code", "primary_exchange",
    "list_date", "address", "phone_number", "currency_name", "type", "cik", "composite_figi", "locale",
    "market", "unavailable",
)


def _ms(ts: pd.Timestamp) -> int:
    return int(pd.DatetimeIndex([ts]).as_unit("ms").asi8[0])


def _pct(a: float, b: float) -> float | None:
    return (a / b - 1) * 100 if b else None


def key_stats(daily: pd.DataFrame, asset: str = "stock") -> dict:
    """`daily`: one row per session (index = session time), columns open/high/low/close/volume.
    `asset`: crypto counts its windows in calendar days (EVERY_DAY); crypto and forex start their
    year at UTC midnight, as their days."""
    if daily is None or daily.empty:
        return {}
    periods = EVERY_DAY if asset == "crypto" else PERIODS
    per_year = periods[-1][1]
    d = daily.sort_index()
    close = d["close"].to_numpy(dtype=float)
    last = d.iloc[-1]
    out: dict = {
        "asOf": _ms(d.index[-1]), "sessions": int(len(d)),
        "last": float(last["close"]),
        "day": {"open": float(last["open"]), "high": float(last["high"]), "low": float(last["low"]),
                "close": float(last["close"]), "volume": float(last["volume"])},
    }
    if len(d) > 1:
        prev = close[-2]
        out["prevClose"] = float(prev)
        out["change"] = float(close[-1] - prev)
        out["changePct"] = _pct(close[-1], prev)
    year = d.tail(per_year)
    hi_i, lo_i = int(np.argmax(year["high"].to_numpy())), int(np.argmin(year["low"].to_numpy()))
    out["range52w"] = {"high": float(year["high"].iloc[hi_i]), "highTime": _ms(year.index[hi_i]),
                       "low": float(year["low"].iloc[lo_i]), "lowTime": _ms(year.index[lo_i]),
                       "sessions": int(len(year))}
    all_hi, all_lo = int(np.argmax(d["high"].to_numpy())), int(np.argmin(d["low"].to_numpy()))
    out["rangeAll"] = {"high": float(d["high"].iloc[all_hi]), "highTime": _ms(d.index[all_hi]),
                       "low": float(d["low"].iloc[all_lo]), "lowTime": _ms(d.index[all_lo])}
    out["avgVolume30"] = float(d["volume"].tail(30).mean())
    rets = np.diff(np.log(close[-31:])) if len(close) > 2 else np.array([])
    out["volatility30"] = float(np.std(rets, ddof=1) * math.sqrt(per_year) * 100) if len(rets) > 1 else None

    perf: dict = {}
    for label, n in periods:
        perf[label] = _pct(close[-1], close[-1 - n]) if len(close) > n else None
    # a UTC day's midnight is 19:00 or 20:00 the evening before in New York: January 1st's bar fell in the old year
    years = (d.index.tz_convert("UTC") if asset in UTC_DAYS and d.index.tz is not None else d.index).year
    before = d[years < years[-1]]
    perf["YTD"] = _pct(close[-1], float(before["close"].iloc[-1])) if not before.empty else None
    perf["All"] = _pct(close[-1], float(d["open"].iloc[0]))  # since the first session's open
    out["performance"] = perf
    return out


def details_subset(details: dict | None) -> dict:
    if not details:
        return {}
    return {k: details[k] for k in DETAIL_FIELDS if details.get(k) not in (None, "", {})}
