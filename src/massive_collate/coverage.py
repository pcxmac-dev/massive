"""One feed per span: what a feed may download for a series.

Every span of a ticker's series is downloaded by exactly one feed per market (the `coverage` ledger
in the store records who holds what). Feeds of different markets are not copies of each other:
Coinbase's and Kraken's Bitcoin, Nasdaq's and NYSE's trades of a stock, are different data that
the composite model combines; feeds of the same market (two consolidated tapes, SIP via Alpaca and
via Massive) are copies, and only one of them downloads a span. Before a feed asks for anything it takes the span it could fetch
now (its plan's history floor to the newest data its plan has published) and removes

- everything any feed already holds (whoever got there first keeps it), and
- everything a better-ranked feed watching the same ticker could fetch now.

Which feed downloads a span (`rank`), best first:
1. availability: only feeds whose plan reaches the span are asked at all (history floor to newest);
2. precision: the finer resolution it collects (ticks, then seconds, then minutes);
3. freshness: how far its plan trails real time (real time, then delayed, then end of day);
4. quality measured against the other exchanges, then accuracy measured in audits;
5. SOURCE_PRIORITY.
So of two copies of the consolidated tape, the real-time one with ticks takes the spans (Massive
Advanced over Alpaca's 15-minute delayed tape). Only audits fetch a span twice, and they store no bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone  # noqa: F401

from . import freshness
from .config import Tier
from .sessions import ET

STEP_MS = {"minute": 60_000, "minute:5": 300_000, "hour": 3_600_000, "day": 86_400_000}


def frame_key(timespan: str, multiplier: int = 1) -> str:
    """The ledger's name for a time frame: 'minute', 'hour', 'day', or 'minute:5' (5-minute bars)."""
    return timespan if multiplier == 1 else f"{timespan}:{multiplier}"


def frame_parts(key: str) -> tuple[str, int]:
    """'minute:5' -> ('minute', 5); 'hour' -> ('hour', 1)."""
    span, _, mult = key.partition(":")
    return span, int(mult or 1)
RES_RANK = {"trades": 0, "second": 1, "minute": 2, "auto": 2, "hour": 3, "day": 4}
UTC_DAY_ASSETS = ("fx", "crypto")  # their day bars are UTC days


@dataclass(frozen=True)
class FeedProfile:
    """What a configured feed can serve on this machine."""
    name: str
    tier: Tier
    assets: frozenset = field(default_factory=frozenset)
    priority: int = 99  # position in SOURCE_PRIORITY (lower first)
    tick_days: int = 365  # (unused: ticks are no longer fetched for history)
    second_days: int = 365  # 1-second bars are backfilled this far (minutes cover all the history)
    venues: dict = field(default_factory=dict)  # asset class -> the market its data is ('sip', 'coinbase', ...)
    capacity: float | None = None  # requests a day its subscription allows: its daily cap and rate (sourcing.py)


def same_market(feeds: dict, a: str, b: str, asset: str) -> bool:
    """Whether two feeds' data of an asset class is the same market (then one download per span)."""
    pa, pb = feeds.get(a), feeds.get(b)
    va = pa.venues.get(asset, "") if pa else ""
    vb = pb.venues.get(asset, "") if pb else ""
    return va == vb


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def day_start_ms(d: date) -> int:
    return ms(datetime.combine(d, time(0), ET))


def frontier_ms(asset: str, tier: Tier, now: datetime, timespan: str) -> int:
    """End (exclusive) of what a plan has published by `now`: up to its newest expected bar; for
    day bars only days whose session is over (a day bar of a session still trading would change).
    A real-time plan's newest minutes are left to settle first (freshness.settle)."""
    now = now - freshness.settle(tier, asset)
    exp = freshness.expected(asset, tier, now)
    if exp.latest is None:
        return ms(now)
    if timespan == "day":
        tz = timezone.utc if asset in UTC_DAY_ASSETS else ET
        t = exp.latest.astimezone(tz)
        start = datetime.combine(t.date(), time(0), tz)
        complete = not freshness.CALENDAR.is_open(asset, exp.latest + timedelta(minutes=1))
        return ms(start + timedelta(days=1)) if complete else ms(start)
    step = STEP_MS.get(timespan, 60_000)
    if step > 60_000:  # a coarser frame (5 minutes, an hour): only its bars that are complete
        return (ms(exp.latest) + 60_000) // step * step
    return ms(exp.latest) + step


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


def intersect(spans: list[tuple[int, int]], within: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of `spans` inside `within` (all half-open [start, end))."""
    return [(max(a, x), min(b, y)) for a, b in spans for x, y in within if max(a, x) < min(b, y)]


def eras_ahead(eras: list[tuple[int, int, list[str]]], peer: str, me: str) -> list[tuple[int, int]] | None:
    """Where the delivery scheme (sourcing.Scheme.order: a job's eras in one market) puts `peer`
    before `me`; None when it knows only one of them (or neither): then the rank decides."""
    known = {n for *_, order in eras for n in order}
    if peer not in known or me not in known:
        return None
    return [(a, b) for a, b, order in eras if peer in order and me in order and order.index(peer) < order.index(me)]


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


def second_floor(profile: FeedProfile, today: date, floor: date) -> date:
    """Oldest day a feed backfills 1-second bars to: SECOND_HISTORY_DAYS back, not before its
    plan floor (minute bars cover everything older)."""
    return max(floor, today - timedelta(days=profile.second_days))


def latency(tier: Tier, asset: str) -> float:
    """Seconds a plan's newest data trails real time for an asset class (a day: end of day)."""
    d = freshness.plan_delay(tier, asset)
    return d.lag.total_seconds() if d.kind == "delay" else 86_400.0


def rank(profile: FeedProfile, resolution: str, scores: dict, quality: float = 1.0,
         asset: str | None = None) -> tuple:
    """Best first (see the module's docstring): finer resolution, then fresher plan for `asset`,
    then quality (composite.py), then audited accuracy, then priority."""
    accuracy = (scores.get(profile.name) or {}).get("accuracy")
    return (RES_RANK.get(resolution, 2), latency(profile.tier, asset) if asset else 0.0, -round(quality, 3),
            -(accuracy or 0.0), profile.priority, profile.name)


def plan(store, me: FeedProfile, feeds: dict[str, FeedProfile], ticker: str, asset: str, timespan: str,
         floor: date, now: datetime, listed_from: date | None = None) -> list[tuple[int, int]]:
    """The spans `me` should download for a series now, oldest first."""
    from .collector import history_floor

    today = now.astimezone(ET).date()
    lo, hi = day_start_ms(floor), frontier_ms(asset, me.tier, now, timespan)
    gaps = [(lo, hi)] if lo < hi else []
    # Held: what a feed of the same market sent. Bars built here from ticks do not count: the
    # feed's own bars replace them when they arrive (received over derived).
    held = [s for src, spans in store.coverage(ticker, timespan, received_only=True).items()
            if same_market(feeds, src, me.name, asset) for s in spans]
    nothing = store.coverage(ticker, timespan, include_none=True)  # feeds' spans incl. their empty ones
    holds = store.coverage(ticker, timespan)
    # what this feed already asked for and found empty is done for it (not for others)
    held += subtract(nothing.get(me.name, []), holds.get(me.name, []))
    gaps = subtract(gaps, held)

    def empty(feed: str) -> list[tuple[int, int]]:
        """Where a feed was asked and has nothing: a better-ranked feed does not stand in the way there."""
        return subtract(nothing.get(feed, []), holds.get(feed, []))
    watch = {(w["source"], w["ticker"]): w for w in store.watchlist(ticker=ticker)}
    if gaps and timespan in ("minute", "second"):
        # Bars are built here from ticks: where another feed holds ticks, the span is left to it. The
        # tick feed itself still downloads its own bars there, which replace the ones built from its
        # ticks. (Ticks are no longer fetched for history: a watch row still at 'trades' from before
        # reserves no window of ticks to come.)
        held_ticks = [s for src, spans in store.coverage(ticker, "trades").items()
                      if src != me.name and same_market(feeds, src, me.name, asset) for s in spans]
        gaps = subtract(gaps, held_ticks)
    if gaps:
        scores = store.audit_scores()
        from .composite import quality_of
        from .sourcing import standing

        eras = standing(store, feeds, now).order(asset, timespan, me.venues.get(asset))
        mine = rank(me, (watch.get((me.name, ticker.upper())) or {}).get("resolution", "auto"), scores,
                    quality_of(store, me.name, asset), asset)
        for peer in feeds.values():
            row = watch.get((peer.name, ticker.upper()))
            if peer.name == me.name or row is None or asset not in peer.assets:
                continue
            if not same_market(feeds, peer.name, me.name, asset):
                continue  # another market's data is not a copy of this feed's
            ahead = eras_ahead(eras, peer.name, me.name)  # the delivery scheme (sourcing.py) decides
            if ahead is None:  # where it does not know both: the rank does
                if rank(peer, row.get("resolution", "auto"), scores, quality_of(store, peer.name, asset), asset) >= mine:
                    continue
                ahead = [(0, 2**62)]
            p_floor = history_floor(peer.tier, today, asset)
            if listed_from:
                p_floor = max(p_floor, listed_from)
            window = intersect([(day_start_ms(p_floor), frontier_ms(asset, peer.tier, now, timespan))], ahead)
            gaps = subtract(gaps, subtract(window, empty(peer.name)))
    step = STEP_MS.get(timespan, 60_000)
    return [(a, b) for a, b in gaps if b - a >= step]
