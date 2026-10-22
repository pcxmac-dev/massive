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
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from .config import UNLIMITED_BACKFILL_YEARS, Tier
from .errors import BudgetExhausted, NotEntitled, RateLimited
from .sources.base import RESOLUTIONS, DataSource
from .storage import Store

ET = ZoneInfo("America/New_York")
Log = Callable[[str], None]
# Days that come back empty this recently may just not be published yet.
SETTLE_DAYS = 3
# Daily bars are final a few hours after the 16:00 ET close.
EOD_READY_HOUR_ET = 20


class StopRun(Exception):
    """The run must stop: request cap reached, or the API is rate limiting us."""


@dataclass
class SyncResult:
    rows: int = 0
    stopped: str | None = None
    messages: list[str] = field(default_factory=list)


def now_et() -> datetime:
    return datetime.now(ET)


def history_floor(tier: Tier, today: date) -> date:
    """Oldest date we may request on this plan (one day of margin inside the limit)."""
    years = tier.history_years or UNLIMITED_BACKFILL_YEARS
    try:
        floor = today.replace(year=today.year - years)
    except ValueError:  # 29 February
        floor = today.replace(year=today.year - years, day=28)
    return floor + timedelta(days=1)


def _date_ns(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()) * 10**9


def _ms_to_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, ET).date()


def _guard(pages: Iterator[list[dict]]) -> Iterator[list[dict]]:
    """Turn 'stop everything' conditions into StopRun while iterating pages."""
    try:
        yield from pages
    except BudgetExhausted as e:
        raise StopRun(f"request cap reached ({e})") from e
    except RateLimited as e:
        raise StopRun(f"rate limited ({e.message}); stopping until the next run") from e


# -- resolution --------------------------------------------------------------

def resolve_resolution(source: DataSource, store: Store, ticker: str, log: Log = print) -> str | None:
    """Find the finest resolution the plan allows for a ticker and remember it."""
    for resolution in RESOLUTIONS:
        try:
            ok = source.is_entitled(resolution, ticker)
        except BudgetExhausted as e:
            raise StopRun(f"request cap reached ({e})") from e
        except RateLimited as e:
            raise StopRun(f"rate limited ({e.message})") from e
        if ok:
            log(f"{ticker}: finest resolution on your plan is '{resolution}'")
            store.set_resolution(source.name, ticker, resolution)
            return resolution
        log(f"{ticker}: '{resolution}' not on your plan")
    return None


# -- one series ----------------------------------------------------------------

def sync_trades(source: DataSource, store: Store, ticker: str, floor: date, backfilled_to: date | None) -> int:
    saved = 0
    stored = store.trade_range(source.name, ticker)

    if stored:  # forward: from the newest stored trade to now
        for page in _guard(source.trade_pages(ticker, gte_ns=stored[1])):
            saved += store.upsert_trades(source.name, ticker, page)

    if backfilled_to is None or backfilled_to > floor:  # backward: to the plan floor
        stored = store.trade_range(source.name, ticker)
        lt = stored[0] + 1 if stored else None
        try:
            for page in _guard(source.trade_pages(ticker, gte_ns=_date_ns(floor), lt_ns=lt, descending=True)):
                saved += store.upsert_trades(source.name, ticker, page)
        except NotEntitled:
            pass  # older ticks are outside the plan: what we have is as far back as it goes
        store.set_backfilled(source.name, ticker, floor)
    return saved


def sync_bars(
    source: DataSource, store: Store, ticker: str, timespan: str, floor: date, today: date,
    backfilled_to: date | None,
) -> int:
    saved = 0
    stored = store.bar_range(source.name, ticker, timespan)

    if stored:  # forward: from the day of the newest stored bar to today
        for page in _guard(source.bar_pages(ticker, _ms_to_date(stored[1]), today, timespan)):
            saved += store.upsert_bars(source.name, ticker, timespan, 1, page)

    if backfilled_to is None or backfilled_to > floor:  # backward: to the plan floor
        stored = store.bar_range(source.name, ticker, timespan)
        end = _ms_to_date(stored[0]) if stored else today
        try:
            for page in _guard(source.bar_pages(ticker, floor, end, timespan, descending=True)):
                saved += store.upsert_bars(source.name, ticker, timespan, 1, page)
        except NotEntitled:
            pass
        store.set_backfilled(source.name, ticker, floor)
    return saved


# -- listing / identity --------------------------------------------------------

LISTING_RECHECK_DAYS = 7


@dataclass
class Listing:
    ok: bool  # False: skip this ticker this run
    listed_from: date | None = None


def check_listing(source: DataSource, store: Store, item: dict, today: date, log: Log = print) -> Listing:
    """Make sure the symbol still means the instrument we are collecting.

    Symbols get reused, so data is only kept from the current instrument's
    listing date, only active instruments are collected, and a change of FIGI
    (the symbol now belongs to someone else) clears what was stored for it.
    Costs one request per ticker per week.
    """
    ticker, checked = item["ticker"], item["checked_at"]
    if checked and (today - checked).days < LISTING_RECHECK_DAYS:
        return Listing(True, item["listed_from"])
    try:
        inst = _guard_call(source.instrument, ticker, resolve_exchange=False)
    except NotEntitled as e:
        log(f"{ticker}: reference data not available ({e.message}); collecting without a listing check")
        return Listing(True, item["listed_from"])

    if not inst.active:
        log(f"{ticker}: not an active listing; skipped")
        return Listing(False)
    mic = inst.primary_exchange.mic if inst.primary_exchange else None
    if item["exchange"] and mic and mic != item["exchange"]:
        log(f"{ticker}: listed on {mic}, not {item['exchange']} as watched; skipped")
        return Listing(False)

    if item["figi"] and inst.figi and inst.figi != item["figi"]:
        n = store.delete_before(source.name, ticker)
        store.reset_backfill(source.name, ticker)
        log(f"{ticker}: symbol now belongs to a different instrument ({inst.name}); removed {n} old rows")
    if inst.list_date:
        n = store.delete_before(source.name, ticker, _date_ns(inst.list_date))
        if n:
            log(f"{ticker}: removed {n} rows from before its {inst.list_date} listing (another instrument)")
        if item["backfilled_to"] and item["backfilled_to"] < inst.list_date:
            store.set_backfilled(source.name, ticker, inst.list_date)
    store.set_listing(source.name, ticker, inst.list_date, inst.figi, today)
    return Listing(True, inst.list_date)


# -- scheduled run -----------------------------------------------------------

def sync_exchanges(source: DataSource, store: Store, log: Log = print) -> int:
    if not store.exchanges(source.name).empty:
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
    except BudgetExhausted as e:
        raise StopRun(f"request cap reached ({e})") from e
    except RateLimited as e:
        raise StopRun(f"rate limited ({e.message}); stopping until the next run") from e


def sync(source: DataSource, store: Store, tier: Tier, now: datetime | None = None, log: Log = print) -> SyncResult:
    """One scheduled run over the watchlist for a source."""
    now = (now or now_et()).astimezone(ET)
    today = now.date()
    floor = history_floor(tier, today)
    result = SyncResult()
    try:
        sync_exchanges(source, store, log)
        for item in store.watchlist(source.name):
            ticker = item["ticker"]
            listing = check_listing(source, store, item, today, log)
            if not listing.ok:
                continue
            item_floor = max(floor, listing.listed_from) if listing.listed_from else floor
            item = next(w for w in store.watchlist(source.name) if w["ticker"] == ticker)
            resolution, backfilled_to = item["resolution"], item["backfilled_to"]
            if resolution == "auto":
                resolution = resolve_resolution(source, store, ticker, log)
                backfilled_to = None
                if resolution is None:
                    log(f"{ticker}: no supported resolution on your plan; skipped")
                    continue
            try:
                if resolution == "trades":
                    n = sync_trades(source, store, ticker, item_floor, backfilled_to)
                else:
                    n = sync_bars(source, store, ticker, resolution, item_floor, today, backfilled_to)
            except NotEntitled as e:
                log(f"{ticker} {resolution}: refused by your plan ({e.message}); re-checking resolution next run")
                store.set_resolution(source.name, ticker, "auto")
                continue
            result.rows += n
            log(f"{ticker} {resolution}: {n} rows")
    except StopRun as e:
        result.stopped = str(e)
        log(f"stopped: {e}")
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
    floor = history_floor(tier, today)
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
