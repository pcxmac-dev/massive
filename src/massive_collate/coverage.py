"""One feed per span: what a feed may download for a series.

Every span of a ticker's series is downloaded by exactly one feed per market (the `coverage` ledger
in the store records who holds what). Feeds of different markets are not copies of each other:
Coinbase's and Kraken's Bitcoin, Nasdaq's and NYSE's trades of a stock, are different data that
the composite model combines; feeds of the same market (two consolidated tapes, SIP via Alpaca and
via Massive) are copies, and only one of them downloads a span. Before a feed asks for anything it takes the span it could fetch
now (its plan's history floor to the newest data its plan has published) and removes

- everything any feed already holds (whoever got there first keeps it), and
- everything a better-ranked feed watching the same ticker could fetch now.

Feeds rank by resolution first (ticks, then seconds, then minutes), then by accuracy measured in
audits, then by SOURCE_PRIORITY. Only audits fetch a span twice, and they store no bars.
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
    tick_days: int = 365  # ticks are backfilled this far where the feed collects them
    venues: dict = field(default_factory=dict)  # asset class -> the market its data is ('sip', 'coinbase', ...)


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


def tick_floor(store, profile: FeedProfile, ticker: str, today: date, floor: date) -> date:
    """Oldest day a feed backfills ticks to: TICK_HISTORY_DAYS back (not before its plan floor),
    or, while backfill is paused for disk space, no further back than the ticks it already holds."""
    lo = max(floor, today - timedelta(days=profile.tick_days))
    if store.kv_get("ticks:backfill-paused"):
        held = [a for a, _ in store.coverage(ticker, "trades").get(profile.name, [])]
        lo = max(lo, datetime.fromtimestamp(min(held) / 1000, ET).date()) if held else today
    return lo


def rank(profile: FeedProfile, resolution: str, scores: dict, quality: float = 1.0) -> tuple:
    """Best first: finer resolution, then quality (composite.py), then audited accuracy, then priority."""
    accuracy = (scores.get(profile.name) or {}).get("accuracy")
    return (RES_RANK.get(resolution, 2), -round(quality, 3), -(accuracy or 0.0), profile.priority, profile.name)


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
    watch = {(w["source"], w["ticker"]): w for w in store.watchlist() if w["ticker"] == ticker.upper()}
    if gaps and timespan in ("minute", "second"):
        # Bars are built here from ticks: the window another feed collects ticks for is left to it
        # (even before the ticks arrive). The tick feed itself still downloads its own bars there,
        # which replace the ones built from its ticks.
        held_ticks = [s for src, spans in store.coverage(ticker, "trades").items()
                      if src != me.name and same_market(feeds, src, me.name, asset) for s in spans]
        gaps = subtract(gaps, held_ticks)
        for f in feeds.values():
            row = watch.get((f.name, ticker.upper()))
            if f.name != me.name and row and row.get("resolution") == "trades" and asset in f.assets \
                    and same_market(feeds, f.name, me.name, asset):
                f_floor = history_floor(f.tier, today)
                if listed_from:
                    f_floor = max(f_floor, listed_from)
                t_lo = tick_floor(store, f, ticker, today, f_floor)
                window = [(day_start_ms(t_lo), frontier_ms(asset, f.tier, now, "minute"))]
                ticks_none = store.coverage(ticker, "trades", source=f.name, include_none=True).get(f.name, [])
                ticks_held = store.coverage(ticker, "trades").get(f.name, [])
                gaps = subtract(gaps, subtract(window, subtract(ticks_none, ticks_held)))
    if gaps:
        scores = store.audit_scores()
        from .composite import quality_of

        mine = rank(me, (watch.get((me.name, ticker.upper())) or {}).get("resolution", "auto"), scores,
                    quality_of(store, me.name, asset))
        for peer in feeds.values():
            row = watch.get((peer.name, ticker.upper()))
            if peer.name == me.name or row is None or asset not in peer.assets:
                continue
            if not same_market(feeds, peer.name, me.name, asset):
                continue  # another market's data is not a copy of this feed's
            if rank(peer, row.get("resolution", "auto"), scores, quality_of(store, peer.name, asset)) >= mine:
                continue
            p_floor = history_floor(peer.tier, today)
            if listed_from:
                p_floor = max(p_floor, listed_from)
            window = [(day_start_ms(p_floor), frontier_ms(asset, peer.tier, now, timespan))]
            gaps = subtract(gaps, subtract(window, empty(peer.name)))
    step = STEP_MS.get(timespan, 60_000)
    return [(a, b) for a, b in gaps if b - a >= step]
