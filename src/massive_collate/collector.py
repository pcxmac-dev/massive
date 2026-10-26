"""Download logic: the finest data each plan allows, as far back as it allows.

Everything here stays inside the plan's limits: requests go through the
source's rate limiter and request cap, history is never requested beyond the
plan's window, and a refusal from the API stops the run (or that series)
instead of retrying.

Series are filled newest-first and saved page by page, so a run that stops
part-way loses nothing and the stored data always stays contiguous.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from .config import UNLIMITED_BACKFILL_YEARS, Tier, asset_tier
from .errors import BudgetExhausted, NotEntitled, Paused, RateLimited, SourceError
from . import freshness
from .futures import candidate_contracts, roll_schedule
from .coverage import STEP_MS, FeedProfile, frame_parts, frontier_ms, has_open_time, ms, plan, second_floor
from .sources.base import DataSource
from .symbols import ASSETS
from .storage import Store

ET = ZoneInfo("America/New_York")
Log = Callable[[str], None]
# Days that come back empty this recently may just not be published yet.
SETTLE_DAYS = 3
# Daily bars are final a few hours after the 16:00 ET close.
EOD_READY_HOUR_ET = 20


class StopRun(Exception):
    """The run must stop: request cap reached, or the API is rate limiting us. `strike`: the
    feed misbehaved (refused, or failing): its fetching is killed (killswitch.py)."""

    def __init__(self, message: str = "", strike: bool = False):
        super().__init__(message)
        self.strike = strike


@dataclass
class SyncResult:
    rows: int = 0
    stopped: str | None = None
    skipped: int = 0  # symbols refreshed recently enough to leave for a later run
    messages: list[str] = field(default_factory=list)


def now_et() -> datetime:
    return datetime.now(ET)


def history_floor(tier: Tier, today: date, asset: str | None = None) -> date:
    """Oldest date we may request on this plan (one day of margin inside a plan's years); with
    `asset`, on that asset class's plan. A plan without a limit reaches back to where its source's
    history begins (`history_start`), or UNLIMITED_BACKFILL_YEARS when that is not known."""
    tier = asset_tier(tier, asset)
    if tier.history_years is None and tier.history_start:
        floor = tier.history_start
    else:
        years = tier.history_years or UNLIMITED_BACKFILL_YEARS
        try:
            floor = today.replace(year=today.year - years)
        except ValueError:  # 29 February
            floor = today.replace(year=today.year - years, day=28)
        floor += timedelta(days=1)
    if tier.history_days:
        floor = max(floor, today - timedelta(days=tier.history_days))
    return max(floor, tier.history_start) if tier.history_start else floor


def _date_ns(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()) * 10**9


def _ms_to_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, ET).date()


def _guard(pages: Iterator[list[dict]]) -> Iterator[list[dict]]:
    """Turn 'stop everything' conditions into StopRun while iterating pages."""
    try:
        yield from pages
    except Paused as e:
        raise StopRun(str(e)) from e
    except BudgetExhausted as e:
        raise StopRun(f"request cap reached ({e})") from e
    except RateLimited as e:
        raise StopRun(f"rate limited ({e.message}); stopping until the next run", strike=True) from e


# -- resolution --------------------------------------------------------------

def resolve_resolution(
    source: DataSource, store: Store, ticker: str, log: Log = print, asset: str = "stock", fetch: str | None = None,
) -> str | None:
    """Find the finest resolution the plan allows for a ticker and remember it.
    `fetch` is the source's own ticker when it differs (I:COMP, C:EURUSD, a futures contract).

    Entitlements belong to the plan and asset class, not the ticker, so an answer is reused
    for every symbol of that class for a week instead of probing each one again."""
    for resolution in source.resolutions(asset):
        ok = known = store.entitlement(source.name, asset, resolution)
        if ok is None:
            try:
                ok = source.is_entitled(resolution, fetch or ticker, asset)
            except Paused as e:
                raise StopRun(str(e)) from e
            except BudgetExhausted as e:
                raise StopRun(f"request cap reached ({e})") from e
            except RateLimited as e:
                raise StopRun(f"rate limited ({e.message})", strike=True) from e
            store.set_entitlement(source.name, asset, resolution, ok)
        if ok:
            log(f"{ticker}: finest resolution on your plan is '{resolution}'")
            store.set_resolution(source.name, ticker, resolution)
            return resolution
        if known is None:  # only report what was actually asked; known answers cost nothing
            log(f"{ticker}: '{resolution}' not on your plan")
    return None


# -- one series ----------------------------------------------------------------

TICK_STOP_FRACTION = 5  # below TICK_MIN_FREE_GB / 5 no ticks are downloaded at all


def tick_space(store: Store, min_free_gb: int) -> str:
    """'ok', 'no-backfill' (free disk under min_free_gb: only new ticks) or 'stop' (under a fifth
    of it). Remembered in kv so the planner and the Fetcher know."""
    import shutil

    where = store.feeds_dir if store.feeds_dir and store.feeds_dir.is_dir() else Path.home()
    free_gb = shutil.disk_usage(where).free / 1e9
    state = "stop" if free_gb < min_free_gb / TICK_STOP_FRACTION else "no-backfill" if free_gb < min_free_gb else "ok"
    if state == "ok":
        store.kv_set("ticks:backfill-paused", "")
    else:
        store.kv_set("ticks:backfill-paused", f"{free_gb:.0f} GB free")
    return state


def sync_trades(source: DataSource, store: Store, ticker: str, floor: date, now: datetime, me: FeedProfile,
                feeds: dict[str, FeedProfile], fetch: str | None = None, asset: str = "stock",
                listed_from: date | None = None, log: Log = print, rules: dict | None = None) -> int:
    """Ticks for the recent window (TICK_HISTORY_DAYS) the way bars are filled: spans nobody holds,
    newest first, recorded in the ledger page by page. One-second and minute bars are then built
    from them here (condition rules as the tape uses them), so those spans need no bar downloads."""
    from .coverage import tick_floor

    today = now.astimezone(ET).date()
    lo = tick_floor(store, me, ticker, today, floor)
    gaps = plan(store, me, feeds, ticker, asset, "trades", lo, now, listed_from)
    edge = frontier_ms(asset, me.tier, now, "minute")  # the feed's publication edge
    saved = 0
    for i, (a, b) in enumerate(reversed(gaps)):
        n, span = _fill_ticks(source, store, ticker, a, b, fetch or ticker, asset, frontier=b >= edge, rules=rules, log=log)
        saved += n
        if i == 0 and n:
            _queue_audit(store, me, feeds, ticker, asset, "minute", span[1] - 60_000 if span else None)
    return saved


def _fill_ticks(source: DataSource, store: Store, ticker: str, a: int, b: int, fetch: str, asset: str,
                frontier: bool, rules: dict | None, log: Log) -> tuple[int, tuple[int, int] | None]:
    """Download ticks in [a, b) ms newest first; after every page record the span held and, at the
    end (or when a cap stops the run), build bars for the whole minutes now fully held."""
    if not has_open_time(asset, a, b):
        store.add_coverage(source.name, ticker, "trades", a, b)
        return 0, None
    sessions = [x for x in freshness.CALENDAR.sessions(asset, _ms_to_date(a) - timedelta(days=1), _ms_to_date(b))
                if ms(x.open) < b]
    last_open = ms(sessions[-1].open) if sessions else a
    saved, oldest, newest, span = 0, None, None, None

    def top() -> int:
        if not frontier or (newest is not None and newest >= last_open):
            return b
        return (newest // 60_000 + 1) * 60_000 if newest is not None else a

    try:
        for page in _guard(source.trade_pages(fetch, gte_ns=a * 1_000_000, lt_ns=b * 1_000_000, descending=True)):
            if not page:
                continue
            saved += store.upsert_trades(source.name, ticker, page)
            times = [int(t["sip_timestamp"]) for t in page]
            o, n = min(times) // 1_000_000, max(times) // 1_000_000
            oldest = o if oldest is None else min(oldest, o)
            newest = n if newest is None else max(newest, n)
            # held from the millisecond after the oldest: more ticks of that same millisecond may come
            # on the next page, and a run stopped here asks for the whole of it again (the store
            # keeps one of each)
            store.add_coverage(source.name, ticker, "trades", oldest + 1, top())
        _close_head(store, source.name, ticker, "trades", a, top(), oldest, frontier)
        span = (a, top())
    finally:  # also when a cap stops the run: build what is complete
        if oldest is not None:
            build_from_ticks(store, source.name, ticker, oldest, top(), rules, log)
    return saved, span


def build_from_ticks(store: Store, source: str, ticker: str, a: int, b: int, rules: dict | None = None,
                     log: Log = print) -> int:
    """Build one-second and minute bars from stored ticks for the whole minutes of [a, b) that the
    feed's tick spans fully hold (a minute cut by a stopped run is built once the rest arrives),
    and record them in the ledger as built from ticks. Works a day at a time; each day's bars are
    kept apart from the feed's own (built_bars) under a derivation record of the ticks read and the
    condition rules applied. Returns bars built."""
    from . import provenance
    from .ticks import BAR_RULES, REGULAR, build_bars

    minute = 60_000
    if not store.ticks_allowed:  # bars-only mode: the ticks are not read (none would be built, all claimed)
        return 0
    spans = store.coverage(ticker, "trades").get(source, [])
    span = next(((x, y) for x, y in spans if x < b and y > a), None)  # the merged span holding [a, b)
    if span is None:
        return 0
    # minutes touching [a, b), but only those entirely inside the held span
    lo = max(a // minute * minute, -(-span[0] // minute) * minute)
    hi = min(-(-b // minute) * minute, span[1] // minute * minute)
    if hi <= lo:
        return 0
    rules = rules if rules is not None else store.condition_rules(source)
    settings = {"steps": ["second", "minute"], "rules": provenance.digest(sorted(rules.items())), "rule_count": len(rules),
                "overrides": sorted([f"{k[0]}:{k[1]}", v] for k, v in BAR_RULES.items()), "regular": list(REGULAR),
                "method": "SIP consolidated update rules per trade condition (ticks.build_bars)"}
    built, unknown = 0, set()
    day = 86_400_000
    start = lo
    while start < hi:
        end = min(hi, (start // day + 1) * day)
        ticks = store.trades_between(source, ticker, start * 1_000_000, end * 1_000_000)
        derivation = provenance.record(store, "bars-from-ticks", f"{source}:{ticker}", settings, {
            "ticks": f"{source}:{ticker}", "count": len(ticks),
            "first_ns": int(ticks["ts_ns"].iloc[0]) if len(ticks) else None,
            "last_ns": int(ticks["ts_ns"].iloc[-1]) if len(ticks) else None}, (start, end))
        made = 0
        prior = store.close_before(source, ticker, "minute", start)  # the window's first bars keep it
        for step, timespan in ((1_000_000_000, "second"), (60_000_000_000, "minute")):
            bars, unk = build_bars(ticks, step, rules, prior_close=prior)
            unknown |= unk
            made += store.upsert_built_bars(source, ticker, timespan, 1, bars, derivation)
            store.add_coverage(source, ticker, timespan, start, end, origin="ticks")
        store.set_derivation_rows(derivation, made)
        built += made
        start = end
    if unknown:
        log(f"{ticker}: trade conditions without a rule ({', '.join(sorted(unknown))}): counted as regular")
    return built


def invert_bars(bars: list[dict]) -> list[dict]:
    """A currency pair's bars as the inverse pair (EURUSD -> USDEUR).

    Prices invert exactly, and high/low swap (1/low is the highest inverse price).
    VWAP does not invert (1/average != average of 1/price), so it is dropped
    rather than shown wrong.
    """
    out = []
    for b in bars:
        o, h, l, c = b.get("o"), b.get("h"), b.get("l"), b.get("c")
        if not all((o, h, l, c)):
            continue
        out.append({**b, "o": 1 / o, "h": 1 / l, "l": 1 / h, "c": 1 / c, "vw": None})
    return out


def _store_bars(store: Store, source: DataSource, ticker: str, timespan: str, page: list[dict], invert: bool) -> int:
    span, mult = frame_parts(timespan)
    return store.upsert_bars(source.name, ticker, span, mult, invert_bars(page) if invert else page)


def _fill(source: DataSource, store: Store, ticker: str, timespan: str, a: int, b: int, fetch: str,
          asset: str, invert: bool, frontier: bool) -> tuple[int, int | None]:
    """Download [a, b) of a series from `source`, newest first, recording in the coverage ledger
    what it now holds after every page (a run stopped by a cap resumes exactly there).
    The newest span (`frontier`) counts as held only once the feed has published its last
    session: a span the feed has not caught up on stays open for the next run.
    Returns (rows saved, newest bar ms)."""
    if not has_open_time(asset, a, b):  # nights, weekends, holidays: nothing to ask for
        store.add_coverage(source.name, ticker, timespan, a, b)
        return 0, None
    step = STEP_MS.get(timespan, 60_000)
    if timespan == "day":
        last_open = b - step - 6 * 3_600_000  # the last day bar, wherever its day starts (ET or UTC)
    else:
        sessions = [x for x in freshness.CALENDAR.sessions(asset, _ms_to_date(a) - timedelta(days=1), _ms_to_date(b))
                    if ms(x.open) < b]
        last_open = ms(sessions[-1].open) if sessions else a
    saved, oldest, newest = 0, None, None

    def top() -> int:
        if not frontier or (newest is not None and newest >= last_open):
            return b
        return newest + step if newest is not None else a

    lo, hi = datetime.fromtimestamp(a / 1000, ET), datetime.fromtimestamp(b / 1000, ET)
    span, mult = frame_parts(timespan)
    for page in _guard(source.bar_pages(fetch, lo, hi, span, mult, descending=True, asset=asset)):
        window = getattr(page, "window", None)
        page = [x for x in page if a <= int(x["t"]) < b]
        if not page:
            if window and not frontier and max(a, window[0]) < min(b, window[1]):
                # asked, and the feed has nothing there: recorded now, so a capped run resumes past it
                store.add_coverage(source.name, ticker, timespan, max(a, window[0]), min(b, window[1]), origin="none")
            continue
        saved += _store_bars(store, source, ticker, timespan, page, invert)
        times = [int(x["t"]) for x in page]
        oldest = min(times) if oldest is None else min(oldest, min(times))
        newest = max(times) if newest is None else max(newest, max(times))
        store.add_coverage(source.name, ticker, timespan, oldest, top())
    _close_head(store, source.name, ticker, timespan, a, top(), oldest, frontier)
    return saved, newest


EMPTY_HEAD_MS = 7 * 86_400_000  # a feed's history starting this much after a span's start: it has none there


def _close_head(store: Store, source: str, ticker: str, timespan: str, a: int, b: int, oldest: int | None,
                frontier: bool) -> None:
    """Record a finished span: held from `a`, or, when the feed's data starts well after `a` (its
    history begins later), held from its first bar and 'none' before it, so another feed may fill
    that head. The frontier span is judged so too: a stock's first fetch is one span from its
    listing to the newest published, and recorded held from `a` it never offered its head to
    another feed (META's 2012-2016 and GOOGL's 2004-2014 on Massive). A frontier span with no bars
    is left open (nothing published there yet)."""
    if oldest is None:
        if not frontier:
            store.add_coverage(source, ticker, timespan, a, b, origin="none")
    elif oldest - a >= EMPTY_HEAD_MS:
        store.add_coverage(source, ticker, timespan, a, oldest, origin="none")
        store.add_coverage(source, ticker, timespan, oldest, b)
    else:
        store.add_coverage(source, ticker, timespan, a, b)


def retired_end(item: dict) -> datetime | None:
    """Where a retired symbol's series ends (the start of the day after its last listed day), or
    None while it is listed."""
    day = item.get("retired_on")
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ET) if day else None


def _plan(store: Store, me: FeedProfile, feeds: dict[str, FeedProfile], ticker: str, asset: str, timespan: str,
          floor: date, now: datetime, listed_from: date | None = None, stop: datetime | None = None) -> list[tuple[int, int]]:
    """coverage.plan, ended at `stop` (a retired symbol's end, retired_end): nothing after it is asked for."""
    gaps = plan(store, me, feeds, ticker, asset, timespan, floor, now, listed_from)
    if stop is None:
        return gaps
    cut, step = ms(stop), STEP_MS.get(timespan, 60_000)
    return [(a, min(b, cut)) for a, b in gaps if min(b, cut) - a >= step]


def sync_bars(
    source: DataSource, store: Store, ticker: str, timespan: str, floor: date, now: datetime,
    me: FeedProfile, feeds: dict[str, FeedProfile], fetch: str | None = None, asset: str = "stock",
    invert: bool = False, listed_from: date | None = None, log: Log = print, stop: datetime | None = None,
) -> int:
    """Download every span of a series this feed should hold and nobody holds yet (coverage.plan:
    one feed per span), newest first, so current data lands before older history. `fetch` is the
    source's ticker when it differs from the stored one; `invert` stores an fx pair inverted.
    `stop` ends a retired symbol's series (retired_end): a span cut there is history, not the
    frontier, so it is recorded held (or 'none') once asked. (Its last session has no bars: held
    only as far as published, the span was asked again on every run, for good.)"""
    fetch = fetch or ticker
    gaps = _plan(store, me, feeds, ticker, asset, timespan, floor, now, listed_from, stop)
    # Only a gap reaching the feed's publication edge is its frontier (held only as far as published);
    # an older gap ending where another feed's data begins is history like any other.
    edge = frontier_ms(asset, me.tier, now, timespan)
    saved = 0
    for i, (a, b) in enumerate(reversed(gaps)):
        try:
            n, newest = _fill(source, store, ticker, timespan, a, b, fetch, asset, invert, frontier=b >= edge)
        except NotEntitled as e:
            if i == 0:
                raise
            log(f"{ticker} {timespan}: {_ms_to_date(a)} to {_ms_to_date(b)} refused by the plan ({e.message})")
            continue
        saved += n
        if i == 0 and n and timespan == "minute":
            _queue_audit(store, me, feeds, ticker, asset, timespan, newest)
    return saved


REFETCH_MAX = 10  # requests a symbol's fetch may spend asking again for bars that failed the check


def verify_fetched(source: DataSource, store: Store, ticker: str, asset: str, fetch: str, invert: bool, since_ms: int,
                   me: FeedProfile, feeds: dict[str, FeedProfile], log: Log = print) -> dict:
    """Check what a symbol's fetch just stored against the feed's own finer bars (baddata.inconsistent:
    a day within its minutes, a minute within its seconds), ask the feed once more for each bar that
    fails (at most REFETCH_MAX requests; a different answer is stored as a correction, the first kept
    in bar_revisions), and queue a second feed's audit of every span the fetch updated and of every bar
    still failing. What still fails is pruned by the check derive runs next (baddata.scan_symbol), so it
    never reaches the filtered data. Returns {"failed", "asked", "corrected"}."""
    from . import baddata

    out = {"failed": 0, "asked": 0, "corrected": 0}
    for timespan in ("day", "minute"):
        span = store.changed_span(source.name, ticker, timespan, since_ms - 60_000)
        if span is None:
            continue
        lo, hi = span[0], span[1] + STEP_MS.get(timespan, 60_000)
        _queue_audit_span(store, me, feeds, ticker, asset, timespan, lo, hi)  # a second feed's view of what changed
        bad, evidence = baddata.inconsistent(store, source.name, ticker, timespan, lo, hi, asset)
        if bad.empty:
            continue
        out["failed"] += len(bad)
        for row, ev in zip(bad.itertuples(), evidence):
            if out["asked"] >= REFETCH_MAX:
                break
            a = int(row.ts)
            b = a + (86_400_000 if timespan == "day" else 60_000)
            out["asked"] += 1
            try:
                page = [x for p in _guard(source.bar_pages(fetch, datetime.fromtimestamp(a / 1000, ET),
                                                           datetime.fromtimestamp(b / 1000, ET), *frame_parts(timespan),
                                                           asset=asset))
                        for x in p if int(x["t"]) == a]
            except SourceError as e:  # no second answer: the check prunes it as it stands
                log(f"{ticker} {timespan}: could not ask {source.name} again ({e})")
                page = []
            again = page[0] if page else None
            seen = (invert_bars([again])[0] if invert else again) if again is not None else None
            if seen is not None and (seen["h"], seen["l"]) != (row.high, row.low):
                _store_bars(store, source, ticker, timespan, [again], invert)  # a correction: the first is kept
                out["corrected"] += 1
                log(f"{ticker} {timespan} {datetime.fromtimestamp(a / 1000, ET):%Y-%m-%d %H:%M}: {source.name} answered "
                    f"high {row.high} low {row.low}, its {ev['finer']} bars {ev['finer_low']}-{ev['finer_high']}; asked "
                    f"again: high {seen['h']} low {seen['l']}")
            else:
                log(f"{ticker} {timespan} {datetime.fromtimestamp(a / 1000, ET):%Y-%m-%d %H:%M}: {source.name} answered "
                    f"high {row.high} low {row.low} twice, its {ev['finer']} bars {ev['finer_low']}-{ev['finer_high']}: "
                    "bad data, kept off the filtered view")
            _queue_audit_span(store, me, feeds, ticker, asset, timespan, a, b)
    if out["failed"]:
        log(f"{ticker}: {out['failed']} fetched bars outside the feed's own finer bars; asked again {out['asked']}, "
            f"{out['corrected']} answered differently")
    return out


def _queue_audit_span(store: Store, me: FeedProfile, feeds: dict[str, FeedProfile], ticker: str, asset: str,
                      timespan: str, lo: int, hi: int) -> None:
    """Ask another feed of the same market watching the ticker to audit [lo, hi) (a spot of it)."""
    from .coverage import same_market

    if any(f.name != me.name and asset in f.assets and same_market(feeds, f.name, me.name, asset)
           for f in feeds.values()):
        store.queue_audit(ticker, timespan, lo, hi, me.name)


def _queue_audit(store: Store, me: FeedProfile, feeds: dict[str, FeedProfile], ticker: str, asset: str,
                 timespan: str, newest: int | None) -> None:
    """After new data: ask another feed watching the ticker to audit the newest session."""
    if newest is None:
        return
    from .coverage import same_market

    if not any(f.name != me.name and asset in f.assets and same_market(feeds, f.name, me.name, asset)
               for f in feeds.values()):
        return  # no copy of this market to audit against
    t = datetime.fromtimestamp(newest / 1000, ET)
    sessions = [x for x in freshness.CALENDAR.sessions(asset, t.date() - timedelta(days=1), t.date() + timedelta(days=1))
                if x.open <= t < x.close]
    if sessions:
        store.queue_audit(ticker, timespan, ms(sessions[-1].open), newest + STEP_MS[timespan], me.name)


# -- futures -------------------------------------------------------------------

ROLL_HORIZON_DAYS = 150  # contracts expiring this far ahead can matter for the roll
CONTRACT_LEAD_DAYS = 120  # a contract is fetched from this long before its month (covers its time as front)
ENDED_AFTER_DAYS = 5  # no session bar for this long: the contract has expired


def _windows(schedule: list[tuple[date, str]]) -> list[tuple[str, date, date]]:
    """Consecutive sessions held on the same contract: [(contract, first, last)]."""
    out: list[list] = []
    for d, t in schedule:
        if out and out[-1][0] == t:
            out[-1][2] = d
        else:
            out.append([t, d, d])
    return [tuple(w) for w in out]


def sync_future(
    source: DataSource, store: Store, item: dict, floor: date, today: date, log: Log = print,
) -> int:
    """Continuous futures: candidate contracts -> official session bars -> roll schedule ->
    intraday bars of each held contract over the sessions it was held.

    Contracts are derived from their tickers (product + month code + year digit) rather
    than listed: a listing returns one row per contract per day and costs dozens of pages.
    Each candidate costs one request for its session bars, which also shows whether it
    exists and when it stopped trading; finished and non-existent contracts are remembered
    and never requested again, live ones at most once a day.
    """
    ticker, product, rank = item["ticker"], item["source_ticker"], item["rank"]
    if not item["checked_at"]:
        store.set_listing(source.name, ticker, None, None, today, f"{product} continuous #{rank}", "future")
    horizon = today + timedelta(days=ROLL_HORIZON_DAYS)
    known = {c["ticker"]: c for c in store.contracts(source.name, product)}
    saved = 0
    for month, tickers in candidate_contracts(product, floor - timedelta(days=31), horizon):
        recs = [known[t] for t in tickers if t in known]
        if any(r["state"] == "ended" for r in recs):
            continue
        live = next((r for r in recs if r["state"] == "live"), None)
        if live is None and recs and len(recs) == len(tickers) and all(r["state"] == "missing" for r in recs):
            continue  # neither naming exists for this month
        if live is None and len(recs) == len(tickers) and all(r["state"] in ("pending", "missing") for r in recs) \
                and any(r["state"] == "pending" for r in recs) \
                and all(r["checked"] and (today - r["checked"]).days < LISTING_RECHECK_DAYS for r in recs):
            continue  # every naming tried, not trading yet: look again next week
        # The working ticker if known; otherwise each naming (one-digit year, then two) until one has data.
        options = [live["ticker"]] if live else [t for t in tickers if known.get(t, {}).get("state") != "missing"]
        for t in options:
            stored = store.bar_range(source.name, t, "day")
            if stored and _ms_to_date(stored[1]) >= today - timedelta(days=1):
                break  # live and already up to date today
            start = _ms_to_date(stored[1]) if stored else max(floor, month - timedelta(days=CONTRACT_LEAD_DAYS))
            end = min(today, month + timedelta(days=62))
            if start > end:
                store.set_contract(source.name, product, t, month, "pending", None, today)
                break
            for page in _guard(source.bar_pages(t, start, end, "day", asset="future")):
                saved += store.upsert_bars(source.name, t, "day", 1, page)
            stored = store.bar_range(source.name, t, "day")
            if stored is None:
                store.set_contract(source.name, product, t, month, "missing" if month < today else "pending", None, today)
                continue  # try the other naming
            ended = _ms_to_date(stored[1]) < today - timedelta(days=ENDED_AFTER_DAYS)
            store.set_contract(source.name, product, t, month, "ended" if ended else "live",
                               _ms_to_date(stored[1]) if ended else None, today)
            for other in tickers:  # this month's contract has one name: the others never exist
                if other != t:
                    store.set_contract(source.name, product, other, month, "missing", None, today)
            break

    volumes, expiries = {}, {}
    for c in store.contracts(source.name, product):
        if c["state"] not in ("live", "ended"):
            continue
        df = store.load_bars([c["ticker"]], "day", 1, source=source.name)
        volumes[c["ticker"]] = {t.date(): float(v or 0) for t, v in zip(df["time"], df["volume"])}
        # Live contracts: provisional expiry after their month; the roll also moves on when
        # the held contract stops trading.
        expiries[c["ticker"]] = c["last_trade"] or (c["month"] + timedelta(days=62))
    schedule = [(d, t) for d, t in roll_schedule(volumes, expiries, rank) if d >= floor]
    store.set_rolls(source.name, product, rank, schedule)
    if not schedule:
        return saved

    resolution = item["resolution"]
    if resolution == "auto":
        resolution = resolve_resolution(source, store, ticker, log, asset="future", fetch=schedule[-1][1])
        if resolution is None:
            return saved
    for contract, first, last in _windows(schedule):
        start, end = first - timedelta(days=1), (today if contract == schedule[-1][1] else last)
        stored = store.bar_range(source.name, contract, resolution)
        if stored is None:
            pages = source.bar_pages(contract, start, end, resolution, asset="future")
        elif _ms_to_date(stored[0]) > start:  # backward fill keeps stored data contiguous
            pages = source.bar_pages(contract, start, _ms_to_date(stored[0]), resolution, descending=True, asset="future")
        elif _ms_to_date(stored[1]) < end or contract == schedule[-1][1]:
            # the held contract keeps trading: always ask from the day of its newest stored bar
            pages = source.bar_pages(contract, _ms_to_date(stored[1]), end, resolution, asset="future")
        else:
            continue
        for page in _guard(pages):
            saved += store.upsert_bars(source.name, contract, resolution, 1, page)
    return saved


# -- listing / identity --------------------------------------------------------

LISTING_RECHECK_DAYS = 7
# Stocks a listing check found on another exchange than the one watched, not known to be the same
# instrument: {ticker: {mic, pinned, on}}. Skipped without a request until checked again a week on.
ELSEWHERE_KEY = "listing:elsewhere"


@dataclass
class Listing:
    ok: bool  # False: skip this ticker this run
    listed_from: date | None = None


def listed_elsewhere(store: Store, today: date) -> dict[str, dict]:
    """The stocks found on another exchange than watched (ELSEWHERE_KEY) and checked within the
    week: they wait for their next check, and the minute pass does not wait for them (_minutes_first)."""
    out = {}
    for ticker, mark in store.kv_json(ELSEWHERE_KEY, {}).items():
        try:
            if (today - date.fromisoformat(mark["on"])).days < LISTING_RECHECK_DAYS:
                out[ticker] = mark
        except (KeyError, TypeError, ValueError):
            continue
    return out


_LEGAL_FORMS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the",
                "common", "stock", "shares"}


def _name_key(name: str) -> str:
    """A company's name as compared: lower case, words only, its legal form and "common stock" left
    out (Massive's "Balchem Corp" and Alpaca's "Balchem Corporation Common Stock" compare equal)."""
    import re

    return " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _LEGAL_FORMS)


def _same_instrument(store: Store, ticker: str, inst) -> str | None:
    """How the instrument a listing check found is known to be the one watched: "FIGI" (a FIGI the
    feeds' earlier checks recorded, or the directory's where none did), "name" (no FIGIs to compare:
    the same name), or None (another instrument, or nothing to compare it with)."""
    known = [(w["figi"], w["name"]) for w in store.watchlist(ticker=ticker)]
    if not any(f for f, _ in known):  # never checked with a FIGI here: the directory the pin came from
        known += store.directory_identity(ticker)
    figis, names = {f for f, _ in known if f}, {_name_key(n) for _, n in known if n}
    if inst.figi and figis:
        return "FIGI" if inst.figi in figis else None
    if inst.name and _name_key(inst.name) in names - {""}:
        return "name"
    return None


def _feeds_watching(store: Store, source: str, ticker: str) -> list[str]:
    """Every feed watching a ticker (so holding its bars and spans), the checking one first."""
    return list(dict.fromkeys([source, *(w["source"] for w in store.watchlist(ticker=ticker))]))


def check_listing(source: DataSource, store: Store, item: dict, today: date, log: Log = print) -> Listing:
    """Make sure the symbol still means the instrument we are collecting.

    Symbols get reused, so data is only kept from the current instrument's
    listing date, only active instruments are collected, and a change of FIGI
    (the symbol now belongs to someone else) clears what was stored for it, on
    every feed. A stock found on another exchange than watched is followed there
    when it is known to be the same instrument (its FIGI, or without FIGIs its
    name); otherwise it is skipped until the next week's check.
    Costs one request per ticker per week.
    """
    ticker, checked = item["ticker"], item["checked_at"]
    away = store.kv_json(ELSEWHERE_KEY, {}).get(ticker)
    if away and away.get("pinned") == item["exchange"] and ticker in listed_elsewhere(store, today):
        return Listing(False)  # on another exchange than watched, not known to be the same: asked weekly
    if checked and (today - checked).days < LISTING_RECHECK_DAYS and store.details(source.name, ticker):
        return Listing(True, item["listed_from"])
    # Another feed checked this ticker this week: share its answer instead of asking again, if it
    # says who the instrument is (a listing date or FIGI: Alpaca's asset record has neither, and
    # sharing it blanked Massive's listing date and replaced its reference record, logo link and all).
    for other in store.watchlist(ticker=ticker):
        if (other["source"] != source.name and other["checked_at"] and (other["listed_from"] or other["figi"])
                and (today - other["checked_at"]).days < LISTING_RECHECK_DAYS and store.details(other["source"], ticker)):
            listed = other["listed_from"] or item["listed_from"]
            store.set_listing(source.name, ticker, listed, other["figi"] or item["figi"], other["checked_at"],
                              other["name"], other["kind"], other["exchange"])
            if not store.details(source.name, ticker):  # its own reference record, if it has one, is kept
                store.set_details(source.name, ticker, {**store.details(other["source"], ticker), "sharedFrom": other["source"]})
            return Listing(True, listed)
    try:
        inst = _guard_call(source.instrument, ticker, resolve_exchange=False)
    except NotEntitled as e:
        log(f"{ticker}: reference data not available ({e.message}); collecting without a listing check")
        return Listing(True, item["listed_from"])

    # A record that says who the instrument is (a listing date or FIGI: Massive's) speaks for every
    # feed. Alpaca's has neither, and its status is its own (inactive there need not mean delisted):
    # it retired symbols on every feed, and nothing cleared the mark when they were found listed.
    identity = bool(inst.list_date or inst.figi)
    if not inst.active:
        raw = (inst.details or {}).get("delisted_utc")
        if raw or identity:
            gone = date.fromisoformat(raw[:10]) if raw else (item.get("retired_on") or today)  # not moved on each check
            if item.get("retired_on") != gone:
                store.set_retired(ticker, gone)
                log(f"{ticker}: no longer listed (since {gone}); marked retired, history still filled")
        elif not item.get("retired_on"):
            log(f"{ticker}: inactive on {source.name} (its own status, no delisting date); not marked retired")
        # checked: asked again in a week, not with every sync (it was every day, for good)
        store.set_listing(source.name, ticker, item["listed_from"], item["figi"], today)
        store.set_details(source.name, ticker, {**(inst.details or {}), "active": False})
        return Listing(True, item["listed_from"])
    mic = inst.primary_exchange.mic if inst.primary_exchange else None
    if item["exchange"] and mic and mic != item["exchange"]:
        # Moved exchange (BCPC: "listed on XNAS, not XNYS"): skipped, it got no data, cost a request
        # per feed per run and, never finishing its minutes, held back every stock's 1-second pass.
        how = _same_instrument(store, ticker, inst)
        if how is None:
            store.set_listing(source.name, ticker, item["listed_from"], item["figi"], today)  # checked: in a week again
            store.kv_update(ELSEWHERE_KEY, lambda m: {**m, ticker: {"mic": mic, "pinned": item["exchange"],
                                                                    "on": today.isoformat()}}, {})
            log(f"{ticker}: listed on {mic}, not {item['exchange']} as watched, and not known to be the same "
                "instrument; skipped (checked again in a week)")
            return Listing(False)
        store.set_exchange(ticker, mic)
        log(f"{ticker}: moved from {item['exchange']} to {mic} (the same {how}); followed there")
    if away:  # where it is watched now: no longer skipped
        store.kv_update(ELSEWHERE_KEY, lambda m: {t: v for t, v in m.items() if t != ticker}, {})
    if item.get("retired_on") and identity and mic:
        store.set_retired(ticker, None)  # listed and active on its exchange: its history goes on again
        log(f"{ticker}: listed on {mic} and active; retired mark ({item['retired_on']}) cleared")

    # The previous issuer's data is on every feed watching the ticker (Tiingo's and Alpaca's too),
    # not only on the one that checked.
    feeds = _feeds_watching(store, source.name, ticker)
    if item["figi"] and inst.figi and inst.figi != item["figi"]:
        n = 0
        for feed in feeds:
            n += store.delete_before(feed, ticker)
            store.drop_coverage(feed, ticker)  # what was held is gone: asked for again
            store.reset_backfill(feed, ticker)
            if feed != source.name:  # the new listing is theirs too (an archive fills from its own listing date)
                store.set_listing(feed, ticker, inst.list_date, inst.figi, None, inst.name, inst.kind)  # checked again
                store.set_closed(feed, ticker, None)  # a closed archive has the new issuer's history to fill
        log(f"{ticker}: symbol now belongs to a different instrument ({inst.name}); removed {n} old rows"
            + (f" on {len(feeds)} feeds" if len(feeds) > 1 else ""))
    if inst.list_date:
        n = sum(store.delete_before(feed, ticker, _date_ns(inst.list_date)) for feed in feeds)
        if n:
            log(f"{ticker}: removed {n} rows from before its {inst.list_date} listing (another instrument)")
        for w in store.watchlist(ticker=ticker):
            if w["backfilled_to"] and w["backfilled_to"] < inst.list_date:
                store.set_backfilled(w["source"], ticker, inst.list_date)
    store.set_listing(source.name, ticker, inst.list_date, inst.figi, today, inst.name, inst.kind, mic)
    store.set_details(source.name, ticker, inst.details or {"unavailable": "no reference record"})
    return Listing(True, inst.list_date)


def check_reference(source: DataSource, store: Store, item: dict, today: date, log: Log = print) -> None:
    """Reference details (name, description, ...) for an index or currency pair, at most weekly.
    A refusal is recorded too, so it is not retried every run."""
    ticker = item["ticker"]
    have = store.details(source.name, ticker)
    if have and (today - date.fromisoformat(have["_updated_at"][:10])).days < LISTING_RECHECK_DAYS:
        return
    try:
        inst = _guard_call(source.instrument, item["source_ticker"], resolve_exchange=False)
    except NotEntitled as e:
        store.set_details(source.name, ticker, {"unavailable": e.message})
        log(f"{ticker}: reference data not available on your plan")
        return
    details = dict(inst.details or {"unavailable": "no reference record"})
    name = inst.name
    if item["invert"] and name:
        # The record describes the source's pair (EURUSD); this symbol is its inverse.
        name = f"{name}, inverted ({ticker[:3]} priced in {ticker[3:]})"
        details["name"] = name
    store.set_details(source.name, ticker, details)
    if name and item.get("name") != name:
        store.set_listing(source.name, ticker, None, None, today, name, item["asset"])


# -- scheduled run -----------------------------------------------------------

def sync_exchanges(source: DataSource, store: Store, log: Log = print) -> int:
    if not store.exchanges(source.name).empty or not source.serves("stock"):
        return 0
    try:
        n = store.upsert_exchanges(_guard_call(source.exchanges))
    except NotEntitled as e:
        log(f"exchange list not available on your plan ({e.message})")
        return 0
    log(f"saved {n} exchanges")
    return n


def _guard_call(fn: Callable, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Paused as e:
        raise StopRun(str(e)) from e
    except BudgetExhausted as e:
        raise StopRun(f"request cap reached ({e})") from e
    except RateLimited as e:
        raise StopRun(f"rate limited ({e.message}); stopping until the next run", strike=True) from e


class Refusals:
    """Sanity check for a symbol a feed's plan refuses run after run: after REFUSAL_LIMIT runs in a
    row it is left alone for REFUSAL_PAUSE (asked again sooner only when the chart opens it), so a
    lasting refusal does not cost requests and fill the log every hour. Kept in kv
    `refusals:<feed>` as {ticker: {n, at, why}}; an answer clears it."""

    LIMIT, PAUSE = 3, timedelta(hours=24)

    def __init__(self, store: Store, feed: str):
        self.store, self.key = store, f"refusals:{feed}"
        self.rows: dict = store.kv_json(self.key, {})

    def _save(self) -> None:
        import json

        self.store.kv_set(self.key, json.dumps(self.rows))

    def paused(self, ticker: str, now: datetime) -> bool:
        r = self.rows.get(ticker)
        return bool(r and r["n"] >= self.LIMIT and now - datetime.fromisoformat(r["at"]) < self.PAUSE)

    def note(self, ticker: str, now: datetime, why: str) -> str:
        """Count a refusal; returns what the log line should add."""
        r = self.rows.get(ticker) or {"n": 0}
        self.rows[ticker] = {"n": r["n"] + 1, "at": now.isoformat(), "why": str(why)[:200]}
        self._save()
        n = self.rows[ticker]["n"]
        return f" (refused {n} runs in a row: left alone for {self.PAUSE.total_seconds() / 3600:.0f} h)" \
            if n >= self.LIMIT else ""

    def clear(self, ticker: str) -> None:
        if self.rows.pop(ticker, None) is not None:
            self._save()


# Feeds whose plan limits how many different symbols a month may be asked for (Tiingo's free plan:
# 500): kept a little inside, taken in the watchlist's order (the most traded first).
SYMBOLS_PER_MONTH = {"tiingo": 450}


def _monthly_symbol(store: Store, feed: str, ticker: str, now: datetime) -> bool:
    """Whether `feed` may ask for `ticker` this month (recorded as asked when it may; one
    transaction, so two processes cannot both take the last place)."""
    cap = SYMBOLS_PER_MONTH.get(feed)
    if cap is None:
        return True
    key = f"symbols:{feed}:{now.astimezone(timezone.utc):%Y-%m}"
    allowed = False

    def take(asked: list) -> list:
        nonlocal allowed
        allowed = ticker in asked or len(asked) < cap
        return asked if ticker in asked or not allowed else [*asked, ticker]

    store.kv_update(key, take, [])
    return allowed


def _archive_gap(source: DataSource, store: Store, me: FeedProfile, feeds: dict[str, FeedProfile], item: dict,
                 floor: date, now: datetime) -> bool | None:
    """Whether a closed feed has history to fill for a stock: spans from its listing date that no
    better-placed feed can serve (coverage.plan). None while no feed has checked its listing: a
    reused symbol's earlier issuer must not be filled, so it waits."""
    ticker = item["ticker"]
    listed = item["listed_from"] or next((w["listed_from"] for w in store.watchlist(ticker=ticker) if w["listed_from"]), None)
    if not listed:
        return None
    start = max(floor, listed)
    return any(_plan(store, me, feeds, ticker, "stock", ts, start, now, listed, retired_end(item))
               for ts in dict.fromkeys((*source.resolutions("stock"), "day")))


# Daily bars before the minute backfill: one request brings a stock's whole daily history on the
# plan, while its minutes take tens, so every stock's dailies (from its listing, as far as the plan
# reaches) are in long before the backfill gets to it. At most this many stocks a run.
DAILY_FIRST_PER_RUN = 2000


def daily_first(source: DataSource, store: Store, me: FeedProfile, feeds: dict[str, FeedProfile], watched: list[dict],
                floor: date, order: dict, refusals: "Refusals", now: datetime, log: Log = print,
                limit: int = DAILY_FIRST_PER_RUN, served: dict | None = None) -> tuple[int, str | None]:
    """The daily bars of the stocks no feed holds any of yet, most traded first: the listing
    check (its date bounds what is kept), then the spans coverage.plan leaves this feed. A stock
    a better-placed feed should take costs no request here. It stops for a symbol opened in the
    chart (the run takes that first, then comes back here). Returns (stocks filled, the symbol it
    stopped for or None)."""
    held = store.daily_from()
    todo = sorted((w for w in watched if w["asset"] == "stock" and w["ticker"] not in held and not w.get("closed_at")),
                  key=lambda w: (order.get(w["ticker"], len(order)), w["ticker"]))
    from . import killswitch

    today, done, stopped, server_fails = now.date(), 0, None, 0
    for item in todo:
        if done >= limit:
            break
        stopped = _unserved_request(store, source.name, now, served)
        if stopped:  # opened in the chart: the run's own pass takes it now, then this sweep goes on
            break
        ticker, listed = item["ticker"], item["listed_from"]
        if refusals.paused(ticker, now):
            continue
        if not _plan(store, me, feeds, ticker, "stock", "day", max(floor, listed) if listed else floor, now, listed,
                     retired_end(item)):
            continue  # another feed is better placed for it, or nothing is left: nothing asked here
        try:
            listing = check_listing(source, store, item, today, log)
            item.update(store.watchlist(source.name, ticker)[0])  # the run's own row: not checked again below
            if not listing.ok:
                continue
            start = max(floor, listing.listed_from) if listing.listed_from else floor
            n = sync_bars(source, store, ticker, "day", start, now, me=me, feeds=feeds, fetch=item["source_ticker"],
                          asset="stock", invert=item["invert"], listed_from=listing.listed_from, log=log,
                          stop=retired_end(item))
            fetch_icon(source, store, ticker, log)  # its details were just read: the logo with them
        except NotEntitled as e:
            log(f"{ticker} day: refused by your plan ({e.message})")
            continue
        except RateLimited as e:  # the server says slow down: the run stops and the feed is killed
            raise StopRun(f"rate limited ({e.message}); stopping until the next run", strike=True) from e
        except SourceError as e:  # counted once, when the run's own pass asks for the symbol below
            log(f"{ticker} day: {e}; left to the run's own pass")
            if not isinstance(e, NotEntitled) and killswitch.is_refusal(e, e.status):  # no to the feed, not one symbol
                raise StopRun(f"refused by {source.name} ({e})", strike=True) from e
            server_fails = server_fails + 1 if e.status == 0 or e.status >= 500 else 0
            if server_fails >= killswitch.FETCH_FAILURES:  # an outage: the run stops, not 2,000 stocks of retries
                raise StopRun(f"{server_fails} symbols in a row failed on {source.name}'s side (last: {e})",
                              strike=True) from e
            continue
        server_fails = 0
        done += 1
        log(f"{ticker} day: {n} rows (daily bars first)")
    if done or stopped:
        log(f"{source.name}: daily bars first: {done:,} stocks"
            + (f", stopped for {stopped} (opened in the chart; goes on after it)" if stopped else "")
            + (f" ({len(todo) - done:,} without any daily bars yet)" if len(todo) > done else ""))
    return done, stopped


def _derive_symbol(store: Store, feed: str, ticker: str, asset: str, log) -> int:
    """The frames `feed` does not send for one symbol, built from what its fetch just stored
    (derive.py), so in a long backfill each symbol's derived frames are ready as it is done, not
    when the whole run ends. A failure is logged and never fails the fetch: the pass after the
    run builds it again."""
    from . import derive

    try:
        return derive.build_symbol(store, feed, ticker, asset, log=log)
    except Exception as e:  # noqa: BLE001 - derived frames are rebuilt by the end-of-run pass
        log(f"{ticker}: derived frames not built ({type(e).__name__}: {e}); the end of the run tries again")
        return 0
    finally:
        store.kv_set(derive.PROGRESS_KEY, "")


PAUSE_KEY = "fetch:pause"
PAUSE_HOURS = 2  # a pause ends on its own: background fetching is never left off by mistake


def pause_state(store: Store, now: datetime | None = None) -> dict | None:
    """Background fetching paused by the user (the chart's button), if it is: {at, until}. The
    hourly sync does not start, a running one stops before its next request; fetches for the
    symbol open in the chart (`sync --tickers`) and the live recorder go on."""
    p = store.kv_json(PAUSE_KEY, {})
    now = now or datetime.now(timezone.utc)
    try:
        return p if datetime.fromisoformat(p["until"]) > now else None
    except (KeyError, TypeError, ValueError):
        return None


HALT_UNTIL = "9999-12-31T00:00:00+00:00"  # a halt lasts until the user resumes


def set_pause(store: Store, paused: bool, now: datetime | None = None, minutes: float | None = None,
              halt: bool = False) -> dict | None:
    """Pause background fetching (for `minutes`, at most PAUSE_HOURS: the open symbol's own
    fetches go on), halt every fetch until resumed (`halt`: the open symbol's too), or resume."""
    import json

    now = now or datetime.now(timezone.utc)
    if not paused:
        store.kv_set(PAUSE_KEY, "")
        return None
    if halt:
        p = {"at": now.isoformat(timespec="seconds"), "until": HALT_UNTIL, "halt": True}
        store.kv_set(PAUSE_KEY, json.dumps(p))
        return p
    minutes = min(minutes or PAUSE_HOURS * 60, PAUSE_HOURS * 60)
    p = {"at": now.isoformat(timespec="seconds"), "until": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")}
    store.kv_set(PAUSE_KEY, json.dumps(p))
    return p


def _backfilling(item: dict, floor: date) -> bool:
    if item["asset"] == "future":
        return item.get("synced_at") is None
    return item["backfilled_to"] is None or item["backfilled_to"] > max(floor, item["listed_from"] or floor)


def _seconds_pending(item: dict) -> bool:
    """A symbol at 1-second resolution whose recent window of second bars is not filled yet."""
    return item.get("resolution") == "second" and item.get("seconds_from") is None


def _minutes_first(items: list[dict], floors: dict, refusals: "Refusals", now: datetime,
                   elsewhere: dict | frozenset = frozenset()) -> int:
    """Stocks still filling their minute history (those refused run after run, which may never
    finish, and those on another exchange than watched (`elsewhere`), which cannot, aside): while
    any are, no 1-second window is started, so every stock gets its whole minute history first
    (tens of requests each) before the 1-second years (a hundred and more)."""
    return sum(1 for w in items if w["asset"] == "stock" and _backfilling(w, floors[w["asset"]])
               and not refusals.paused(w["ticker"], now) and w["ticker"] not in elsewhere)


def _due(asset: str, tier: Tier, synced: datetime, now: datetime, refresh_hours: float) -> bool:
    """Whether a fully synced symbol is worth asking again: its plan should have published
    data since the last sync (stocks on the free plan: once each morning; delayed futures:
    continuously), and at least `refresh_hours` have passed."""
    if refresh_hours and (now - synced).total_seconds() < refresh_hours * 3600:
        return False
    return freshness.expected(asset, tier, now).latest != freshness.expected(asset, tier, synced).latest


def needs_fetch(store: Store, ticker: str, tiers: dict[str, Tier], now: datetime,
                assets: dict[str, frozenset] | None = None) -> list[str]:
    """The feeds (of `tiers`: the ones set up here) with something to fetch for `ticker` now:
    history it still lacks, or data its plan has published since its last sync (the same rule
    the sync skips by: nothing new can exist out of session, or before an end-of-day plan's
    release; a retired symbol, nothing after its last listed day). Empty: every feed holding it is
    up to date, and opening it asks for nothing."""
    today = now.astimezone(ET).date()
    out = []
    for row in store.watchlist(ticker=ticker):
        tier = tiers.get(row["source"])
        if tier is None or row.get("closed_at") or (assets is not None and row["asset"] not in assets.get(row["source"], ())):
            continue  # not set up here, finished, or not able to serve it (no key for its asset class)
        synced, end = row.get("synced_at"), retired_end(row)
        if (synced is None or _backfilling(row, history_floor(tier, today, row["asset"]))
                or (_due(row["asset"], tier, synced, now, 0.0) and (end is None or synced < end))):
            out.append(row["source"])
    return out


# -- the symbol open in the chart goes first ------------------------------------------------
PRIORITY_KEY = "fetch:priority"
PRIORITY_HOURS = 6  # a request older than this has been dealt with (or given up on)


# Updates paused by the user: until it ends, a symbol they have not picked (opened in the chart,
# or on one of their watchlists) gets no new-data updates unless it is more than UPDATES_GAP_DAYS
# behind (missing a substantial amount); history goes on (backfills, daily bars first), and the
# picked symbols are updated as usual. The requests go to those and to filling history.
UPDATES_PAUSE_KEY = "updates:pause"
UPDATES_GAP_DAYS = 7


def updates_pause(store: Store, now: datetime | None = None) -> dict | None:
    """The updates pause, if one is on: {at, until}."""
    p = store.kv_json(UPDATES_PAUSE_KEY, {})
    now = now or datetime.now(timezone.utc)
    try:
        return p if datetime.fromisoformat(p["until"]) > now else None
    except (KeyError, TypeError, ValueError):
        return None


def set_updates_pause(store: Store, days: float | None, now: datetime | None = None) -> dict | None:
    """Pause the unpicked symbols' updates for `days` (None or 0: resume them)."""
    import json

    now = now or datetime.now(timezone.utc)
    if not days:
        store.kv_set(UPDATES_PAUSE_KEY, "")
        return None
    p = {"at": now.isoformat(timespec="seconds"), "until": (now + timedelta(days=days)).isoformat(timespec="seconds")}
    store.kv_set(UPDATES_PAUSE_KEY, json.dumps(p))
    return p


def picked_symbols(store: Store, now: datetime | None = None) -> set[str]:
    """The symbols the user picked: opened in the chart lately (priority requests) or on one of
    their watchlists (the rail's lists)."""
    from .live import WATCHLISTS_KEY

    lists = store.kv_json(WATCHLISTS_KEY, [])
    on_lists = {str(t).upper() for lst in lists if isinstance(lst, dict) for t in lst.get("tickers") or []}
    return on_lists | set(priority_requests(store, now))


def _unserved_request(store: Store, source: str, now: datetime, served: dict | None = None) -> str | None:
    """A symbol opened in the chart that this feed has not synced since (nor taken this run:
    `served`): it goes before anything else (the daily-first sweep stops for it)."""
    for ticker, req in priority_requests(store, now).items():
        if served and served.get(ticker) == req["at"]:
            continue
        row = next(iter(store.watchlist(source, ticker)), None)
        if row is not None and (row.get("synced_at") is None or row["synced_at"] < req["at"]):
            return ticker
    return None


# The order a run takes symbols in (after those opened in the chart, which always go first):
# the user's watchlists, then the last session's big movers, then the initializer's order (most
# traded first). Big movers: the largest moves from the previous close among stocks that traded at
# least MOVERS_MIN_DOLLARS, from the daily bars already held (no requests).
MOVERS = 200
MOVERS_MIN_DOLLARS = 5_000_000
MOVERS_TTL_S = 300.0
_movers_cache: dict[str, tuple[float, list[str]]] = {}


def big_movers(store: Store, source: str, tickers: list[str], top: int = MOVERS) -> list[str]:
    """The `top` stocks that moved most (absolute change from the previous close) in the newest
    session `source` holds daily bars for, most first; cached MOVERS_TTL_S."""
    import time as _time

    hit = _movers_cache.get(source)
    if hit is not None and _time.monotonic() - hit[0] < MOVERS_TTL_S:
        return hit[1][:top]
    days = store.last_days(source, tickers, 2)
    # the session most stocks hold: right after the close only those updated so far hold the newest
    held = [r[0][0] for r in days.values()]
    latest = max(set(held), key=held.count) if held else None
    moves = []
    for t, r in days.items():
        if len(r) == 2 and r[0][0] == latest and r[1][1] and r[0][1] * (r[0][2] or 0) >= MOVERS_MIN_DOLLARS:
            moves.append((abs(r[0][1] / r[1][1] - 1), t))
    out = [t for _, t in sorted(moves, reverse=True)]
    _movers_cache[source] = (_time.monotonic(), out)
    return out[:top]


def watchlist_tickers(store: Store) -> list[str]:
    """The tickers on the user's watchlists (the rail's lists), in list order."""
    from .live import WATCHLISTS_KEY

    lists = store.kv_json(WATCHLISTS_KEY, [])
    return list(dict.fromkeys(str(t).upper() for lst in lists if isinstance(lst, dict) for t in lst.get("tickers") or []))


def fetch_order(store: Store, source: str, tickers: list[str]) -> dict[str, int]:
    """{ticker: rank}, 0 first: watchlists, big movers, then the initializer's order."""
    from .initializer import load_order

    first = list(dict.fromkeys([*watchlist_tickers(store), *big_movers(store, source, tickers)]))
    rest = load_order(store)
    out = {t: i for i, t in enumerate(first)}
    n = len(out)
    for t, r in rest.items():
        out.setdefault(t, n + r)
    return out


def _priority_raw(store: Store) -> dict:
    return store.kv_json(PRIORITY_KEY, {})


def _recent(v, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(v["at"]) < timedelta(hours=PRIORITY_HOURS)
    except (KeyError, TypeError, ValueError):
        return False


def request_priority(store: Store, ticker: str, now: datetime | None = None) -> None:
    """Ask every feed holding `ticker` to update it before anything else (the chart opened it)."""
    now = now or datetime.now(timezone.utc)

    def add(asked: dict) -> dict:
        kept = {t: v for t, v in asked.items() if _recent(v, now)}
        return {**kept, ticker.upper(): {"at": now.isoformat(timespec="seconds"), "runs": 0}}

    store.kv_update(PRIORITY_KEY, add, {})


def priority_started(store: Store, ticker: str) -> None:
    """A run for that symbol alone was started for the request."""
    def count(asked: dict) -> dict:
        v = asked.get(ticker.upper())
        if isinstance(v, dict):
            v["runs"] = v.get("runs", 0) + 1
        return asked

    store.kv_update(PRIORITY_KEY, count, {})


def priority_requests(store: Store, now: datetime | None = None) -> dict[str, dict]:
    """Recent requests: ticker -> {at (datetime), runs (dedicated runs started for it)}."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for t, v in _priority_raw(store).items():
        try:
            at = datetime.fromisoformat(v["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - at < timedelta(hours=PRIORITY_HOURS):
            out[t] = {"at": at, "runs": int(v.get("runs", 0))}
    return out


def _next_item(pending: list[dict], store: Store, now: datetime, source: str = "",
               served: dict | None = None) -> tuple[dict, bool]:
    """The next symbol to work on, and whether it was asked for: a symbol opened in the chart
    since this feed last synced it goes first, wherever it is in the line, and also when the run
    has already passed it (skipped as up to date before the request came: AMD waited hours for
    the next run). Read before every symbol, so a request made while the run is going is taken at
    the next one; `served` (ticker -> request time) takes each request once."""
    asked = priority_requests(store, now)
    served = served if served is not None else {}
    for ticker, req in asked.items():
        at = req.get("at")
        if not at or served.get(ticker) == at:
            continue
        i = next((i for i, w in enumerate(pending) if w["ticker"] == ticker), None)
        row = pending[i] if i is not None else next(iter(store.watchlist(source, ticker)), None) if source else None
        if row is not None and (row.get("synced_at") is None or row["synced_at"] < at):
            served[ticker] = at
            if i is not None:
                pending.pop(i)
            return row, True
    return pending.pop(0), False


def sync(
    source: DataSource, store: Store, tier: Tier, now: datetime | None = None, log: Log = print,
    refresh_hours: float = 0.0, on_item: Callable[[str], None] | None = None, only: set[str] | None = None,
    directory_allowance: int = 0, feeds: dict[str, FeedProfile] | None = None, audits: tuple[int, int] = (0, 0),
    tick_min_free_gb: int = 250, published_only: bool = False,
) -> SyncResult:
    """One scheduled run over the watchlist for a source.

    A fully synced symbol is skipped until its plan should have published new data (see
    `freshness`; on the free plan stocks only change once a day, so asking hourly would only
    spend requests) and, if set, `refresh_hours` have passed. Symbols still backfilling are
    never skipped. `on_item` is told which symbol is
    being worked on (for the activity view). `only` limits the run to those symbols and
    fetches them whatever the refresh interval (used by `scan --fetch`); with `published_only`
    (`sync --tickers`) a symbol whose plan has published nothing since its last sync is still
    left alone. A symbol the chart opened (`request_priority`) is taken first, and its refresh
    interval does not apply (its plan's publication does).

    `feeds` describes every configured feed, so this one downloads only spans no better-ranked
    feed can serve (see coverage.py); `audits` = (queued, random) audits this run may do for
    other feeds' data.
    """
    now = (now or now_et()).astimezone(ET)
    today = now.date()
    freshness.use_official(store)  # the official holidays fetched so far: what is published, and when
    floors = {a: history_floor(tier, today, a) for a in ASSETS}  # each asset class on its own plan
    result = SyncResult()
    saved_before = store.rows_saved
    feeds = dict(feeds or {})
    me = feeds.get(source.name) or FeedProfile(source.name, tier, frozenset(ASSETS))
    feeds[source.name] = me
    waiting: dict[str, int] = {}
    archival = getattr(source, "archival", False)  # a closed feed: fills history once per symbol
    closed = capped = unlisted = 0
    if source.name in SYMBOLS_PER_MONTH and not store.kv_get(f"ledger:month-places:{source.name}:v1"):
        # one-off: a feed that never stored anything (Tiingo without its key) took the month's places
        # without sending a request; they are given back
        if not store.holds_any(source.name):
            store.kv_set(f"symbols:{source.name}:{now.astimezone(timezone.utc):%Y-%m}", "[]")
            log(f"{source.name}: this month's symbol places were taken with nothing sent; given back")
        store.kv_set(f"ledger:month-places:{source.name}:v1", now.isoformat())
    if not store.kv_get(f"icons:unasked:{source.name}:v1"):
        # one-off: icons recorded as "none" for stocks whose details were never read (they were not
        # asked, so they waited 30 days for nothing); asked once their details are in
        n = store.forget_unasked_icons(source.name)
        store.kv_set(f"icons:unasked:{source.name}:v1", now.isoformat())
        if n:
            log(f"{source.name}: {n:,} icons marked missing without being asked; asked again with their details")
    if not store.kv_get("ledger:empty-heads:v1"):  # one-off: feeds' empty history heads become 'none'
        n = store.mark_empty_heads()
        store.kv_set("ledger:empty-heads:v1", now.isoformat())
        if n:
            log(f"ledger: {n} spans where a feed's history starts later marked as theirs to leave")
    try:
        if not archival and only is None:  # a run for chosen symbols spends nothing on the rest
            sync_exchanges(source, store, log)
            if refresh_calendars(source, store, today, log):
                freshness.use_official(store)  # this week's official schedules, from here on
            fetch_news(source, store, now, log)
            from .gui.world import PROXIES
            fetch_market_moves(source, store, now, PROXIES, log)
        # Keep complete symbols current first; backfills take what budget is left, in the fetch
        # order (fetch_order: watchlists, big movers, then most traded), then by ticker.
        refusals = Refusals(store, source.name)
        watched = store.watchlist(source.name)
        # watchlists, then the last session's big movers, then the most traded (fetch_order)
        order = fetch_order(store, source.name, [w["ticker"] for w in watched if w["asset"] == "stock"])
        # Two passes: every stock's minute history first, most traded first; then, once none is left
        # to fill, every stock's window of 1-second bars in the same order (SECOND_HISTORY_DAYS).
        filling = _minutes_first(watched, floors, refusals, now, listed_elsewhere(store, today))
        if only is None and any(_seconds_pending(w) for w in watched):
            log(f"{source.name}: minute history first ({filling:,} stocks still filling); 1-second bars after"
                if filling else f"{source.name}: 1-second pass: "
                f"{sum(1 for w in watched if _seconds_pending(w)):,} stocks' 1-second windows to fill")

        def backfilling(w: dict) -> bool:
            return _backfilling(w, floors[w["asset"]]) or (not filling and _seconds_pending(w))

        served: dict = {}  # priority requests taken this run (each once)
        sweep_left = DAILY_FIRST_PER_RUN if only is None and not archival and source.serves("stock") else 0

        def sweep() -> None:  # daily bars first (daily_first), until it stops for a symbol opened in the chart
            nonlocal sweep_left
            done, stopped = daily_first(source, store, me, feeds, watched, floors["stock"], order, refusals, now, log,
                                        limit=sweep_left, served=served)
            sweep_left = sweep_left - done if stopped else 0

        if sweep_left:
            sweep()
        pending = sorted(watched, key=lambda w: (backfilling(w), order.get(w["ticker"], len(order)), w["ticker"]))
        paused_updates = updates_pause(store, now) if not archival else None
        held_back = 0 if paused_updates else None  # symbols whose update the pause left for later
        picked = picked_symbols(store, now) if paused_updates else set()
        on_lists = set(watchlist_tickers(store))  # updated through the session (history_horizon)
        server_fails = 0  # symbols in a row whose server failed (5xx, no answer): FETCH_FAILURES kill the feed
        while pending:
            if sweep_left and not _unserved_request(store, source.name, now, served):
                sweep()  # the symbols opened in the chart are served: back to daily bars first
            item, urgent = _next_item(pending, store, now, source.name, served)
            try:
                ticker, asset = item["ticker"], item["asset"]
                synced = item.get("synced_at")
                if only is not None and ticker not in only:
                    continue
                if archival and item.get("closed_at"):  # archive complete: never asked again
                    closed += 1
                    continue
                if not source.serves(asset):  # e.g. Alpaca stocks or Tiingo without keys: nothing is sent
                    waiting[asset] = waiting.get(asset, 0) + 1
                    continue
                if archival and not source.covers(item["source_ticker"]):
                    store.set_closed(source.name, ticker, now)
                    log(f"{ticker}: {source.name} has no history for it; closed")
                    continue
                if archival and asset == "stock":  # a month's place only for history it can fill
                    gap = _archive_gap(source, store, me, feeds, item, floors[asset], now)
                    if gap is None:
                        unlisted += 1
                        continue
                    if not gap:
                        store.set_closed(source.name, ticker, now)
                        log(f"{ticker}: {source.name} has nothing to add (other feeds hold its history); closed")
                        continue
                if not _monthly_symbol(store, source.name, ticker, now):  # Tiingo's free plan: 500 symbols a month
                    capped += 1
                    continue
                if not urgent and refusals.paused(ticker, now):  # refused run after run: left alone a while
                    result.skipped += 1
                    continue
                floor = floors[asset]
                # History takes completed sessions only; the session under way is the live feeds'.
                # A symbol opened in the chart, or on a watchlist, is the exception: the best feed
                # for its current session is this one (freshness.history_horizon).
                horizon = now if urgent or only is not None or ticker in on_lists \
                    else freshness.history_horizon(asset, tier, now)
                if (only is None or published_only) and synced and not backfilling(item) and not _due(
                        asset, tier, synced, horizon, 0.0 if urgent or only is not None else refresh_hours):
                    # nothing new published since its last sync (a symbol still backfilling is never skipped)
                    if urgent or only is not None:  # opened in the chart: checked, nothing new; its request is met
                        store.set_synced(source.name, ticker, max(now, datetime.now(timezone.utc)))
                    result.skipped += 1
                    continue
                if (held_back is not None and not urgent and only is None and synced and not backfilling(item)
                        and ticker not in picked and now - synced < timedelta(days=UPDATES_GAP_DAYS)):
                    held_back += 1  # updates paused: not picked, and not missing much
                    result.skipped += 1
                    continue
                if on_item:
                    on_item(ticker)
                spent = source.requests_made
                began = int(datetime.now(timezone.utc).timestamp() * 1000)  # what this symbol's fetch stores is newer
                if asset == "future":
                    try:
                        n = sync_future(source, store, item, floor, today, log)
                    except NotEntitled as e:
                        log(f"{ticker}: futures refused by your plan ({e.message}); skipped"
                            + refusals.note(ticker, now, e.message))
                        continue
                    refusals.clear(ticker)
                    result.rows += n
                    log(f"{ticker}: {n} rows ({source.requests_made - spent} requests)")
                    store.set_synced(source.name, ticker, now)
                    continue
                if asset == "stock" and archival:  # an archive has no reference data: use the other feeds'
                    listing = Listing(True, item["listed_from"] or next(
                        (w["listed_from"] for w in store.watchlist(ticker=ticker) if w["listed_from"]), None))
                elif asset == "stock":  # listing/identity checks only apply to listed securities
                    listing = check_listing(source, store, item, today, log)
                    if not listing.ok:
                        continue
                else:
                    check_reference(source, store, item, today, log)
                    listing = Listing(True)
                item_floor = max(floor, listing.listed_from) if listing.listed_from else floor
                item = store.watchlist(source.name, ticker)[0]
                # a retired symbol has nothing after its last listed day: only its history is filled
                stop = retired_end(item)
                resolution, backfilled_to = item["resolution"], item["backfilled_to"]
                fetch, invert = item["source_ticker"], item["invert"]
                if resolution == "trades":  # no ticks for history any more: found again (second or minute)
                    resolution = "auto"
                if resolution == "auto":
                    resolution = resolve_resolution(source, store, ticker, log, asset=asset, fetch=fetch)
                    backfilled_to = None
                    if resolution is None:
                        log(f"{ticker}: no supported resolution on your plan; skipped")
                        continue
                span = dict(me=me, feeds=feeds, fetch=fetch, asset=asset, invert=invert,
                            listed_from=listing.listed_from, log=log, stop=stop)
                try:
                    if resolution == "second":
                        # minute bars for all the history (tens of requests for twenty years); 1-second
                        # bars for the recent window (SECOND_HISTORY_DAYS) in the second pass, or now for
                        # a symbol opened in the chart, or to keep a filled window current
                        n = sync_bars(source, store, ticker, "minute", item_floor, horizon, **span)
                        if not filling or urgent or only is not None or item.get("seconds_from"):
                            s_floor = second_floor(me, today, item_floor)
                            n += sync_bars(source, store, ticker, "second", s_floor, horizon, **span)
                            if not item.get("seconds_from"):
                                store.set_seconds_from(source.name, ticker, s_floor)
                    else:
                        n = sync_bars(source, store, ticker, resolution, item_floor, horizon, **span)
                except NotEntitled as e:
                    log(f"{ticker} {resolution}: refused by your plan ({e.message}); re-checking resolution next run"
                        + refusals.note(ticker, now, e.message))
                    store.set_resolution(source.name, ticker, "auto")
                    store.clear_entitlements(source.name)  # the plan may have changed: probe again
                    continue
                refusals.clear(ticker)
                result.rows += n
                store.set_backfilled(source.name, ticker, item_floor)  # every span it should hold is in
                if resolution != "day":
                    try:  # official daily bars (closing auction included) for day/week/month charts
                        n_day = sync_bars(source, store, ticker, "day", item_floor, horizon, **span)
                    except NotEntitled:
                        n_day = 0
                    result.rows += n_day
                    # every other frame (30 s, 5 and 30 min, hours, weeks ...) is built here from the
                    # 1-second, 1-minute and daily bars (derive.py): a feed's own 5-minute and hourly
                    # candles are not fetched (requests, and the same bars stored twice)
                    log(f"{ticker} {resolution}: {n} rows, daily: {n_day} rows ({source.requests_made - spent} requests)")
                else:
                    log(f"{ticker} {resolution}: {n} rows ({source.requests_made - spent} requests)")
                store.set_synced(source.name, ticker, now)
                verify_fetched(source, store, ticker, asset, fetch, invert, began, me, feeds, log)
                _derive_symbol(store, source.name, ticker, asset, log)  # bad data checked first (baddata.py)
                if archival:
                    spans = [resolution] if resolution == "day" else [resolution, "day"]
                    if not any(_plan(store, me, feeds, ticker, asset, ts, item_floor, now, listing.listed_from, stop)
                               for ts in spans):
                        store.set_closed(source.name, ticker, now)
                        log(f"{ticker}: {source.name} archive complete; closed (not asked again)")
                elif asset == "stock":
                    fetch_icon(source, store, ticker, log)
            except RateLimited as e:  # the server says slow down: the run stops and the feed is killed
                raise StopRun(f"rate limited ({e.message}); stopping until the next run", strike=True) from e
            except SourceError as e:  # this symbol's own error (404, 5xx after retries, an unexpected
                # answer): it is skipped this run, the others go on, and one failing run after run waits a day
                log(f"{item['ticker']}: {e}; skipped this run" + refusals.note(item["ticker"], now, str(e)))
                from . import killswitch

                if not isinstance(e, NotEntitled) and killswitch.is_refusal(e, e.status):  # no to the feed, not one symbol
                    raise StopRun(f"refused by {source.name} ({e})", strike=True) from e
                server_fails = server_fails + 1 if e.status == 0 or e.status >= 500 else 0
                if server_fails >= killswitch.FETCH_FAILURES:
                    raise StopRun(f"{server_fails} symbols in a row failed on {source.name}'s side (last: {e})",
                                  strike=True) from e
                continue
            server_fails = 0  # a symbol went through: the feed works
        if closed:
            log(f"{source.name}: {closed} symbols closed (archive complete), not asked")
        if capped:
            log(f"{source.name}: {capped} symbols wait for next month (its plan: "
                f"{SYMBOLS_PER_MONTH[source.name]} different symbols a month)")
        if unlisted:
            log(f"{source.name}: {unlisted} stocks wait for a listing date from another feed (history is kept "
                "from the current listing only)")
        if held_back:
            log(f"{source.name}: {held_back:,} symbols' updates paused until {paused_updates['until'][:10]} "
                f"(not picked, at most {UPDATES_GAP_DAYS} days behind)")
        if waiting:
            log(f"{source.name}: " + ", ".join(f"{n} {a} symbols" for a, n in sorted(waiting.items()))
                + " not collected by this feed here (see `deps`: keys or plan)")
        if only is None and any(audits) and not archival:
            from .audit import allowance, run_audits
            run_audits(source, store, me, feeds, now, queued=audits[0], random_count=audits[1], log=log,
                       requests=allowance(source))  # spot checks: 2 % of the run's cap
        if only is None and directory_allowance > 0 and not archival and source.serves("stock"):  # placeholders and icons: leftover budget only
            spent = source.requests_made
            refresh_directory(source, store, today, log)
            fill_directory_icons(source, store, directory_allowance - (source.requests_made - spent), today, log)
    except StopRun as e:
        result.stopped = str(e)
        log(f"stopped: {e}")
        if e.strike:  # refused, or failing: the feed's fetching is killed (killswitch.py)
            from . import killswitch

            killswitch.strike(store, source.name, "fetch", str(e), log=log)
        # rows of the symbol it stopped in were stored page by page: count them too
        result.rows = max(result.rows, store.rows_saved - saved_before)
    return result


CALENDAR_REFRESH_DAYS = 7
NEWS_REFRESH_MINUTES = 30


def fetch_market_moves(source: DataSource, store: Store, now: datetime, proxies: list[str], log: Log = print,
                       max_requests: int = 4) -> int:
    """Closes of the market proxy funds for the two newest trading days that have data (the
    whole-market daily file: one request per day, once). A day the source has not published yet
    is asked again at most every 3 hours. The globe colours each exchange by this move."""
    now = now.astimezone(ET)
    have = set(store.proxy_days(source.name))
    # A proxy added or replaced later is filled from the stored market snapshot (no request).
    for day in have:
        known = store.proxy_closes(source.name, day)
        missing = [p for p in proxies if p not in known]
        if missing:
            closes = store.snapshot_closes(source.name, day, missing)
            if closes:
                store.save_proxy_closes(source.name, day, closes)
    used, found, d = 0, 0, now.date()
    while found < 2 and used < max_requests and (now.date() - d).days < 10:
        if not freshness.is_trading_day(d):
            d -= timedelta(days=1)
            continue
        if d in have:
            found += 1
            d -= timedelta(days=1)
            continue
        key = f"proxy-empty:{source.name}:{d}"
        tried = store.kv_get(key)
        if tried and (now - datetime.fromisoformat(tried)).total_seconds() < 3 * 3600:
            d -= timedelta(days=1)  # not published at the last try: skip for now
            continue
        try:
            bars = _guard_call(source.market_daily, d)
        except NotEntitled as e:
            used += 1
            if "before end of day" in str(e).lower():  # free plan: the day is not out yet
                store.kv_set(key, now.isoformat())
                log(f"market moves: {d} not published yet")
                d -= timedelta(days=1)
                continue
            log(f"market moves: {d} not available ({e})")
            break
        except NotImplementedError:
            break
        except SourceError as e:
            log(f"market moves: {d} not available ({e})")
            break
        used += 1
        if bars:
            store.save_market_snapshot(source.name, d, bars)
        closes = {b["T"]: b["c"] for b in bars if b.get("T") in proxies and b.get("c")}
        if closes:
            store.save_proxy_closes(source.name, d, closes)
            found += 1
            log(f"market moves: {d}, {len(closes)} market proxies")
        else:
            store.kv_set(key, now.isoformat())
            log(f"market moves: {d} not published yet")
        d -= timedelta(days=1)
    return used


def fetch_news(source: DataSource, store: Store, now: datetime, log: Log = print) -> int:
    """Newest headlines (one request, at most every 30 minutes) for the announcements ticker."""
    key = f"news:{source.name}"
    last = store.kv_get(key)
    if last and (now - datetime.fromisoformat(last)).total_seconds() < NEWS_REFRESH_MINUTES * 60:
        return 0
    newest = store.latest_news_ms(source.name)
    since = datetime.fromtimestamp(newest / 1000, timezone.utc) if newest else now - timedelta(days=2)
    try:
        n = store.save_news(source.name, _guard_call(source.news, since, 100))
        log(f"news: {n} headlines")
    except NotImplementedError:
        pass
    except (NotEntitled, SourceError) as e:
        log(f"news: not available ({e})")
    store.kv_set(key, now.isoformat())
    return 1



def refresh_calendars(source: DataSource, store: Store, today: date, log: Log = print) -> int:
    """Weekly: the source's official upcoming market holidays and the futures schedules of every
    watched product (1 request each). They override the rule-based calendar, so unscheduled
    closures and exchange-specific holiday hours are projected exactly. Returns requests used."""
    key = f"calendar:{source.name}"
    last = store.kv_get(key)
    if last and (today - date.fromisoformat(last)).days < CALENDAR_REFRESH_DAYS:
        return 0
    before = source.requests_made
    try:
        n = store.save_market_holidays(source.name, _guard_call(source.market_holidays))
        log(f"calendar: {n} upcoming exchange holidays and early closes")
    except NotImplementedError:
        pass
    except (NotEntitled, SourceError) as e:
        log(f"calendar: upcoming holidays not available ({e})")
    try:
        n = store.save_conditions(source.name, _guard_call(source.trade_conditions))
        log(f"calendar: {n} trade conditions and their bar rules")
    except NotImplementedError:
        pass
    except (NotEntitled, SourceError) as e:
        log(f"calendar: trade condition rules not available ({e})")
    products = sorted({w["source_ticker"] for w in store.watchlist(source.name) if w["asset"] == "future"})
    for product in products:
        try:
            n = store.save_futures_schedule(source.name, product, _guard_call(source.futures_schedule, product, today))
            log(f"calendar: {product} futures schedule, {n} session events")
        except (NotEntitled, SourceError, NotImplementedError) as e:
            log(f"calendar: {product} futures schedule not available ({e})")
    store.kv_set(key, today.isoformat())
    return source.requests_made - before


# Nasdaq, NYSE, NYSE American and NYSE Arca (where most ETFs list); CME Group's products come from
# the futures product list.
DIRECTORY_EXCHANGES = ("XNAS", "XNYS", "XASE", "ARCX")
DIRECTORY_REFRESH_DAYS = 1  # daily: new listings, retirements and reused symbols are found within a day
COMPANY_TYPES = ("CS", "ADRC", "OS", "GDR", "NYRS")  # operating companies (not funds, warrants, units...)
DAILY_RESERVE = 400  # requests of the daily cap kept back for the watchlist's own updates
DETAILS_REFRESH_DAYS = 90  # directory companies' details (and logo links) are re-checked this often


def refresh_directory(source: DataSource, store: Store, today: date, log: Log = print) -> int:
    """Daily: every active listing on the directory's exchanges and the futures products, as
    placeholders. Listings that appear are logged as new, those that leave as retired (kept, with
    the day), a symbol back as relisted, a symbol that now names another instrument as reassigned;
    a watched symbol that retires is marked so (its history is still filled, no new data is
    expected) and the mark is cleared if it comes back."""
    key = f"directory:{source.name}"
    last = store.kv_get(key)
    if last and (today - date.fromisoformat(last)).days < DIRECTORY_REFRESH_DAYS:
        return 0
    before = source.requests_made
    done = True
    try:
        rows = []
        for mic in DIRECTORY_EXCHANGES:
            rows += _guard_call(source.listings, mic)
        _apply_listing(store, source.name, "stock", rows, today, log)
    except NotImplementedError:
        pass
    except (NotEntitled, SourceError) as e:
        log(f"directory: listings not available ({e})")
        done = False
    try:
        _apply_listing(store, source.name, "future", _guard_call(source.futures_products, today), today, log)
    except NotImplementedError:
        pass
    except (NotEntitled, SourceError) as e:
        log(f"directory: futures products not available ({e})")
        done = False
    if done:  # a step that failed is tried again on the next run, not a day later
        store.kv_set(key, today.isoformat())
    return source.requests_made - before


def _apply_listing(store: Store, source: str, kind: str, rows: list[dict], today: date, log: Log) -> None:
    if not rows:
        log(f"directory: no {kind} listings returned; nothing changed")
        return
    ch = store.update_directory(source, kind, rows, today)
    what = "listings on " + ", ".join(DIRECTORY_EXCHANGES) if kind == "stock" else "futures products"
    parts = [f"{len(ch[k])} {k}" for k in ("listed", "retired", "relisted", "reassigned") if ch[k]]
    log(f"directory: {len(rows)} {what}" + (f"; {', '.join(parts)}" if parts else "; no changes"))
    if ch["suspect"]:
        log(f"directory: too many {kind} listings missing at once; none retired (checked again next run)")
    watched = {w["ticker"] for w in store.watchlist() if w["asset"] == ("stock" if kind == "stock" else "future")}
    for t in ch["retired"]:
        if kind == "stock" and t in watched:
            store.set_retired(t, today)
            log(f"{t}: no longer listed; marked retired (its history is kept and still filled, no new data expected)")
    for t in ch["relisted"]:
        if kind == "stock" and t in watched:
            store.set_retired(t, None)
            log(f"{t}: listed again; retired mark cleared")
    for t in ch["reassigned"]:
        if kind == "stock" and t in watched:
            log(f"{t}: the symbol now names another instrument; its listing is re-checked on the next update")
            for w in store.watchlist():
                if w["ticker"] == t:
                    store.set_listing(w["source"], t, w["listed_from"], w["figi"], None, w["name"], w["kind"],
                                      w["exchange"])  # checked_at cleared: check_listing asks again


def fill_directory_icons(source: DataSource, store: Store, allowance: int, today: date, log: Log = print) -> int:
    """Company icons for the directory, biggest companies first, within `allowance` requests:
    each company costs one request for its details (where the logo link is) and one for the
    logo itself. Only budget the watchlist did not need is used."""
    if allowance < 2:
        return 0
    before = source.requests_made
    done = 0
    for ticker in store.directory_icon_queue(source.name, COMPANY_TYPES, allowance // 2 + 1):
        if source.requests_made - before + 2 > allowance:
            break
        if not store.details(source.name, ticker):
            try:
                inst = _guard_call(source.instrument, ticker, resolve_exchange=False)
                store.set_details(source.name, ticker, inst.details or {"unavailable": "no reference record"})
            except (NotEntitled, SourceError) as e:
                store.save_icon(source.name, ticker, None, None, None)
                log(f"{ticker}: details not available ({e})")
                continue
        done += fetch_icon(source, store, ticker, lambda m: None)
    # With budget to spare: re-check details older than DETAILS_REFRESH_DAYS, so new logos are found
    # (the link changes when a company's branding does); fetch_icon then fetches only changed ones.
    for ticker in store.stale_details(source.name, COMPANY_TYPES, DETAILS_REFRESH_DAYS, allowance):
        if source.requests_made - before + 2 > allowance:
            break
        try:
            inst = _guard_call(source.instrument, ticker, resolve_exchange=False)
        except (NotEntitled, SourceError):
            continue
        if inst.details:
            store.set_details(source.name, ticker, inst.details)
        done += fetch_icon(source, store, ticker, lambda m: None)
    used = source.requests_made - before
    if used:
        s = store.directory_stats(source.name, COMPANY_TYPES)
        log(f"directory icons: +{done} ({used} requests); {s['icons']} of {s['companies']} companies have icons")
    return used


def fetch_icon(source: DataSource, store: Store, ticker: str, log: Log = print) -> bool:
    """Download a stock's company icon once (one request) and keep it locally.
    Asked for only after the symbol's data is complete, so icons never delay a backfill."""
    details = store.details(source.name, ticker)
    if not details:  # its reference record was never read: not "no logo", just not asked yet
        return False
    url = (details.get("branding") or {}).get("icon_url")
    if store.icon_checked(source.name, ticker, url=url):  # fetched from this link already (or none lately)
        return False
    if not url:
        store.save_icon(source.name, ticker, None, None, None)
        return False
    try:
        data, mime = _guard_call(source.icon, url)
    except (NotEntitled, SourceError, NotImplementedError) as e:
        log(f"{ticker}: icon not available ({e})")
        store.save_icon(source.name, ticker, url, None, None)
        return False
    if not mime.startswith("image/") or len(data) > 512 * 1024:
        store.save_icon(source.name, ticker, url, None, None)
        return False
    store.save_icon(source.name, ticker, url, mime.split(";")[0], data)
    log(f"{ticker}: icon saved ({len(data) // 1024 or 1} KB, 1 request)")
    return True


# -- scan: is there new data? ----------------------------------------------------

@dataclass
class ScanResult:
    items: list[dict] = field(default_factory=list)
    stopped: str | None = None

    @property
    def new(self) -> list[str]:
        """Symbols the source confirmed it has newer data for."""
        return [i["ticker"] for i in self.items if i.get("new")]


def _newest_stored(store: Store, source: str, item: dict) -> tuple[datetime | None, str | None]:
    """(newest stored bar, source ticker to ask) for a watchlist item."""
    if item["asset"] == "future":
        rolls = store.rolls(source, item["source_ticker"])
        edges = [e for e in (store.bar_edges(source, t, "minute") for t in {t for _, t in rolls}) if e]
        newest = max(e[1] for e in edges) if edges else None
        return (datetime.fromtimestamp(newest / 1000, ET) if newest else None), (rolls[-1][1] if rolls else None)
    if item["resolution"] == "trades":
        r = store.trade_range(source, item["ticker"])
        return (datetime.fromtimestamp(r[1] / 1e9, ET) if r else None), item["source_ticker"]
    timespan = "minute" if item["resolution"] == "auto" else item["resolution"]
    r = store.bar_edges(source, item["ticker"], timespan)
    newest = r[1] if r else None
    other = store.newest_any(item["ticker"], timespan)  # one feed per span: any feed's newest counts
    if other and (newest is None or other[1] > newest):
        newest = other[1]
    return (datetime.fromtimestamp(newest / 1000, ET) if newest else None), item["source_ticker"]


def scan(
    source: DataSource | None, store: Store, tier: Tier, source_name: str | None = None,
    now: datetime | None = None, log: Log = print, probe_limit: int = 30, only: set[str] | None = None,
    feeds: dict[str, FeedProfile] | None = None,
) -> ScanResult:
    """Check every watched symbol for data that should be available but is not stored.

    Free part: compare each symbol's newest stored bar with what its plan should have
    published by now (market calendar + publication delay). With a `source`, every symbol
    that looks behind costs one request (the source's newest bar, limit 1) to confirm the
    data really exists, up to `probe_limit` symbols.
    """
    now = (now or now_et()).astimezone(ET)
    freshness.use_official(store)  # an unscheduled closure has nothing to publish
    name = source.name if source else source_name or "massive"
    result = ScanResult()
    probes = 0
    for item in store.watchlist(name):
        if only is not None and item["ticker"] not in only:
            continue
        last, ask = _newest_stored(store, name, item)
        a = freshness.assess(item["asset"], tier, last, now)
        row = {"ticker": item["ticker"], "asset": item["asset"], "stored": last, **a, "available": None, "new": None}
        result.items.append(row)
        if a["state"] != "behind" or source is None or not ask:
            continue
        if feeds and source.name in feeds and item["asset"] != "future" and item["resolution"] != "trades":
            if not source.serves(item["asset"]) or not plan(
                    store, feeds[source.name], feeds, item["ticker"], item["asset"], "minute",
                    history_floor(tier, now.date(), item["asset"]), now, item.get("listed_from")):
                row["new"] = False  # nothing this feed should fetch: another feed holds or will serve it
                continue
        if probes >= probe_limit:
            row["new"] = None
            continue
        try:
            bar = _guard_call(source.latest_bar, ask, last.date(), item["asset"])
        except StopRun as e:
            result.stopped = str(e)
            log(f"stopped: {e}")
            break
        except (NotEntitled, SourceError, NotImplementedError) as e:
            log(f"{item['ticker']}: could not check ({e})")
            continue
        probes += 1
        newest = datetime.fromtimestamp(bar["t"] / 1000, ET) if bar else None
        row["available"] = newest
        row["new"] = bool(newest and newest > last)
        store.set_probe(name, item["ticker"], now, bar["t"] if bar else None)
    for row in result.items:
        if row["state"] == "behind":
            have = f"source has through {row['available']:%Y-%m-%d %H:%M}" if row["available"] else (
                "not checked with the source" if row["new"] is None else "source has nothing newer")
            log(f"{row['ticker']}: stored through {row['stored']:%Y-%m-%d %H:%M}, should have through "
                f"{row['expectedThrough']:%Y-%m-%d %H:%M} ET ({row['rule']}); {have}"
                + (" -> new data" if row["new"] else ""))
    behind = sum(r["state"] == "behind" for r in result.items)
    log(f"scan: {len(result.items)} symbols, {behind} behind, {len(result.new)} with new data confirmed "
        f"({probes} requests)")
    return result


# -- one-off commands ----------------------------------------------------------

def download_bars(
    source: DataSource, store: Store, tier: Tier, ticker: str, start: date | None, end: date | None,
    timespan: str = "day", multiplier: int = 1, log: Log = print,
) -> int:
    """One-off bar download for a date range (clamped to the plan's history)."""
    today = now_et().date()
    floor = history_floor(tier, today)
    start = start or floor
    if start < floor:
        log(f"{ticker}: your plan covers history from {floor}; starting there")
        start = floor
    end = min(end or today, today)
    saved = 0
    for page in _guard(source.bar_pages(ticker, start, end, timespan, multiplier)):
        saved += store.upsert_bars(source.name, ticker, timespan, multiplier, page)
    log(f"{ticker} {multiplier}/{timespan}: {saved} bars ({start} to {end})")
    return saved


def sync_market_daily(
    source: DataSource, store: Store, tier: Tier, now: datetime | None = None, log: Log = print
) -> SyncResult:
    """Whole-market daily bars, newest missing day first, back to the plan floor."""
    now = (now or now_et()).astimezone(ET)
    today = now.date()
    newest = today if now.hour >= EOD_READY_HOUR_ET else today - timedelta(days=1)
    floor = history_floor(tier, today, "stock")
    known = store.grouped_days(source.name)
    denied = [d for d, s in known.items() if s == "denied"]
    if denied:
        floor = max(floor, max(denied) + timedelta(days=1))

    result = SyncResult()
    day = newest
    try:
        while day >= floor:
            if day.weekday() < 5 and day not in known:
                try:
                    bars = _guard_call(source.market_daily, day)
                except NotEntitled as e:
                    if "end of day" in str(e).lower():  # not released yet (the free plan, the evening): later
                        log(f"{day}: not out yet ({e.message})")
                        day -= timedelta(days=1)
                        continue
                    store.mark_grouped_day(source.name, day, "denied")
                    log(f"{day}: not covered by your plan ({e.message}); not going further back")
                    break
                if bars:
                    n = store.save_grouped_day(source.name, day, bars)
                    result.rows += n
                    log(f"{day}: {n} tickers")
                elif (today - day).days > SETTLE_DAYS:
                    store.mark_grouped_day(source.name, day, "empty")
            day -= timedelta(days=1)
    except StopRun as e:
        result.stopped = str(e)
        log(f"stopped: {e}")
    return result
