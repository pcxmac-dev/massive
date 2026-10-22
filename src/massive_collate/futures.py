"""Continuous futures (NQ1!, CL1!, ...) built from individual contracts.

Sources such as Massive have no continuous series, so it is built here:

1. Each bar belongs to a trading *session*. CME sessions open at 18:00 ET and
   settle at 17:00 ET the next day, so a bar's session is its US/Eastern date
   after adding 6 hours.
2. The roll schedule holds the most-traded contract: it switches only to a
   later-expiring contract that trades more volume in a session, never back,
   and moves on when the held contract expires. (Rank 2 is the next contract
   after the rank-1 contract, e.g. NQ2!.)
3. The continuous series joins each contract's own bars over the sessions it
   was held. Prices are left as traded unless back-adjustment is asked for;
   every roll is reported so charts can mark it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

ET = "America/New_York"
SESSION_SHIFT = pd.Timedelta(hours=6)
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec


def candidate_contracts(product: str, start: date, end: date) -> list[tuple[str, date]]:
    """Every outright ticker whose contract month falls in [start, end]: [(ticker, month start)].
    Tickers use a one-digit year (CLZ6 = December 2026), unambiguous within such a window."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((f"{product.upper()}{MONTH_CODES[m - 1]}{y % 10}", date(y, m, 1)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def contract_month(ticker: str, product: str, around: date) -> date | None:
    """Contract month of an outright ticker, choosing the decade closest to `around`."""
    rest = ticker.upper()[len(product):]
    if len(rest) != 2 or rest[0] not in MONTH_CODES or not rest[1].isdigit():
        return None
    month, digit = MONTH_CODES.index(rest[0]) + 1, int(rest[1])
    year = min((around.year // 10 * 10 + digit + d for d in (-10, 0, 10)), key=lambda y: abs(y - around.year))
    return date(year, month, 1)


def session_dates(index: pd.DatetimeIndex) -> pd.Index:
    """Trading session (settlement date) of each bar."""
    return (index.tz_convert(ET) + SESSION_SHIFT).normalize().date


def roll_schedule(
    volumes: dict[str, dict[date, float]], expiries: dict[str, date], rank: int = 1
) -> list[tuple[date, str]]:
    """[(session, contract held)] from per-contract session volumes."""
    sessions = sorted({d for v in volumes.values() for d in v})
    front: str | None = None
    out: list[tuple[date, str]] = []
    for d in sessions:
        live = [t for t in volumes if expiries.get(t) and expiries[t] >= d and d in volumes[t]]
        if not live:
            continue
        vol = lambda t: volumes[t].get(d, 0.0)  # noqa: E731
        # Re-select at expiry, or when the held contract has stopped trading (its expiry is only
        # provisional until it has ended).
        if front is None or d > expiries[front] or front not in live:
            later = [t for t in live if front is None or expiries[t] >= expiries[front]]
            pool = later or live
            front = max(pool, key=lambda t: (vol(t), -expiries[t].toordinal()))
        else:
            challengers = [t for t in live if expiries[t] > expiries[front] and vol(t) > vol(front)]
            if challengers:
                front = max(challengers, key=vol)
        if rank == 1:
            out.append((d, front))
        else:
            nxt = sorted((t for t in live if expiries[t] > expiries[front]), key=lambda t: expiries[t])
            if len(nxt) >= rank - 1:
                out.append((d, nxt[rank - 2]))
    return out


@dataclass(frozen=True)
class Roll:
    session: date  # first session on the new contract
    time: pd.Timestamp  # first bar on the new contract
    old: str
    new: str
    gap: float | None  # new - old close at the last bar of the old contract (None if not overlapping)


def _gap_from_sessions(daily: dict[str, pd.DataFrame], old: str, new: str, before: pd.Timestamp, ratio: bool):
    """Roll gap from official session closes on the old contract's last held session."""
    o, n = daily.get(old), daily.get(new)
    if o is None or n is None or o.empty or n.empty:
        return None
    last_session = session_dates(pd.DatetimeIndex([before]))[0]
    o_s = o[[d <= last_session for d in session_dates(o.index)]]
    if o_s.empty:
        return None
    day = session_dates(o_s.index[-1:])[0]
    n_s = n[[d == day for d in session_dates(n.index)]]
    if n_s.empty:
        return None
    oc, nc = float(o_s["close"].iloc[-1]), float(n_s["close"].iloc[-1])
    return nc / oc if ratio else nc - oc


def continuous(
    frames: dict[str, pd.DataFrame], schedule: list[tuple[date, str]], adjust: str = "none",
    sessions: dict[str, pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, list[Roll]]:
    """Join contract frames (indexed by time, OHLC columns) along the roll schedule.

    adjust: "none" (prices as traded), "difference" (shift earlier contracts by the
    price gap at each roll) or "ratio" (scale them by the ratio). The gap is taken from
    both contracts' bars at the old contract's last bar when they overlap, otherwise
    from their official session closes on that session (`sessions`).
    """
    if adjust not in ("none", "difference", "ratio"):
        raise ValueError("adjust must be none, difference or ratio")
    held = dict(schedule)
    parts = []
    for ticker, f in frames.items():
        if f.empty:
            continue
        mask = [held.get(s) == ticker for s in session_dates(f.index)]
        part = f[mask]
        if not part.empty:
            parts.append(part.assign(contract=ticker))
    if not parts:
        empty = next(iter(frames.values()), pd.DataFrame()).iloc[0:0]
        return empty.assign(contract=pd.Series(dtype=str)), []
    joined = pd.concat(parts).sort_index(kind="stable")

    rolls: list[Roll] = []
    contracts = joined["contract"].to_numpy()
    change = [i for i in range(1, len(joined)) if contracts[i] != contracts[i - 1]]
    for i in change:
        old, new = contracts[i - 1], contracts[i]
        last_old = joined.index[i - 1]
        new_frame = frames[new]
        at = new_frame.loc[:last_old]
        if not at.empty:
            old_close, new_close = float(joined["close"].iloc[i - 1]), float(at["close"].iloc[-1])
            gap = new_close / old_close if adjust == "ratio" else new_close - old_close
        else:
            gap = _gap_from_sessions(sessions or {}, old, new, last_old, adjust == "ratio")
        rolls.append(Roll(session_dates(joined.index[i:i + 1])[0], joined.index[i], old, new, gap))

    if adjust != "none" and rolls:
        joined = joined.copy()
        price_cols = [c for c in ("open", "high", "low", "close") if c in joined]
        for roll in reversed(rolls):
            if roll.gap is None:
                continue
            before = joined.index < roll.time
            if adjust == "difference":
                joined.loc[before, price_cols] += roll.gap
            else:
                joined.loc[before, price_cols] *= roll.gap
    return joined, rolls
