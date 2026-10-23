"""One feed per span: what a feed may download for a series.

Every span of a ticker's series is downloaded by exactly one feed (the `coverage` ledger in the
store records who holds what). Before a feed asks for anything it takes the span it could fetch
now (its plan's history floor to the newest data its plan has published) and removes

- everything any feed already holds (whoever got there first keeps it), and
- everything a better-ranked feed watching the same ticker could fetch now.

Feeds rank by resolution first (ticks, then seconds, then minutes), then by accuracy measured in
audits, then by SOURCE_PRIORITY. Only audits fetch a span twice, and they store no bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from . import freshness
from .config import Tier
from .sessions import ET

STEP_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}
RES_RANK = {"trades": 0, "second": 1, "minute": 2, "auto": 2, "hour": 3, "day": 4}
UTC_DAY_ASSETS = ("fx", "crypto")  # their day bars are UTC days


@dataclass(frozen=True)
class FeedProfile:
    """What a configured feed can serve on this machine."""
    name: str
    tier: Tier
    assets: frozenset = field(default_factory=frozenset)
    priority: int = 99  # position in SOURCE_PRIORITY (lower first)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def day_start_ms(d: date) -> int:
    return ms(datetime.combine(d, time(0), ET))


def frontier_ms(asset: str, tier: Tier, now: datetime, timespan: str) -> int:
    """End (exclusive) of what a plan has published by `now`: up to its newest expected bar; for
    day bars only days whose session is over (a day bar of a session still trading would change)."""
    exp = freshness.expected(asset, tier, now)
    if exp.latest is None:
        return ms(now)
    if timespan == "day":
        tz = timezone.utc if asset in UTC_DAY_ASSETS else ET
        t = exp.latest.astimezone(tz)
        start = datetime.combine(t.date(), time(0), tz)
        complete = not freshness.CALENDAR.is_open(asset, exp.latest + timedelta(minutes=1))
        return ms(start + timedelta(days=1)) if complete else ms(start)
    return ms(exp.latest) + STEP_MS.get(timespan, 60_000)


def subtract(gaps: list[tuple[int, int]], spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """`gaps` minus every span in `spans` (all half-open [start, end))."""
    out = gaps
    for a, b in sorted(spans):
        nxt = []
        for x, y in out:
            if b <= x or a >= y:
                nxt.append((x, y))
                continue
            if x < a:
                nxt.append((x, a))
            if b < y:
                nxt.append((b, y))
        out = nxt
    return out


def has_open_time(asset: str, a: int, b: int) -> bool:
    """Whether the market trades at all in [a, b): a span of nights, weekends and holidays needs
    no request."""
    if asset == "crypto":
        return b > a
    lo = datetime.fromtimestamp(a / 1000, ET)
    hi = datetime.fromtimestamp(b / 1000, ET)
    for s in freshness.CALENDAR.sessions(asset, lo.date() - timedelta(days=1), hi.date() + timedelta(days=1)):
        if s.open < hi and s.close > lo:
            return True
    return False


def rank(profile: FeedProfile, resolution: str, scores: dict) -> tuple:
    accuracy = (scores.get(profile.name) or {}).get("accuracy")
    return (RES_RANK.get(resolution, 2), -(accuracy or 0.0), profile.priority, profile.name)


def plan(store, me: FeedProfile, feeds: dict[str, FeedProfile], ticker: str, asset: str, timespan: str,
         floor: date, now: datetime, listed_from: date | None = None) -> list[tuple[int, int]]:
    """The spans `me` should download for a series now, oldest first."""
    from .collector import history_floor

    today = now.astimezone(ET).date()
    lo, hi = day_start_ms(floor), frontier_ms(asset, me.tier, now, timespan)
    gaps = [(lo, hi)] if lo < hi else []
    held = [s for spans in store.coverage(ticker, timespan).values() for s in spans]
    gaps = subtract(gaps, held)
    if gaps:
        watch = {(w["source"], w["ticker"]): w for w in store.watchlist() if w["ticker"] == ticker.upper()}
        scores = store.audit_scores()
        mine = rank(me, (watch.get((me.name, ticker.upper())) or {}).get("resolution", "auto"), scores)
        for peer in feeds.values():
            row = watch.get((peer.name, ticker.upper()))
            if peer.name == me.name or row is None or asset not in peer.assets:
                continue
            if rank(peer, row.get("resolution", "auto"), scores) >= mine:
                continue
            p_floor = history_floor(peer.tier, today)
            if listed_from:
                p_floor = max(p_floor, listed_from)
            gaps = subtract(gaps, [(day_start_ms(p_floor), frontier_ms(asset, peer.tier, now, timespan))])
    step = STEP_MS.get(timespan, 60_000)
    return [(a, b) for a, b in gaps if b - a >= step]
