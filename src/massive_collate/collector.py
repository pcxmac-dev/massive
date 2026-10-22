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
from .futures import candidate_contracts, roll_schedule
from .sources.base import RESOLUTIONS, RESOLUTIONS_BY_ASSET, DataSource
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

def resolve_resolution(
    source: DataSource, store: Store, ticker: str, log: Log = print, asset: str = "stock", fetch: str | None = None,
) -> str | None:
    """Find the finest resolution the plan allows for a ticker and remember it.
    `fetch` is the source's own ticker when it differs (I:COMP, C:EURUSD, a futures contract)."""
    for resolution in RESOLUTIONS_BY_ASSET.get(asset, RESOLUTIONS):
        try:
            ok = source.is_entitled(resolution, fetch or ticker, asset)
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
    return store.upsert_bars(source.name, ticker, timespan, 1, invert_bars(page) if invert else page)


def sync_bars(
    source: DataSource, store: Store, ticker: str, timespan: str, floor: date, today: date,
    backfilled_to: date | None, fetch: str | None = None, asset: str = "stock", invert: bool = False,
) -> int:
    """Bring a bar series up to date, then backfill it to the floor. `fetch` is the source's
    ticker when it differs from the stored one; `invert` stores an fx pair inverted."""
    fetch = fetch or ticker
    saved = 0
    stored = store.bar_range(source.name, ticker, timespan)

    if stored:  # forward: from the day of the newest stored bar to today
        for page in _guard(source.bar_pages(fetch, _ms_to_date(stored[1]), today, timespan, asset=asset)):
            saved += _store_bars(store, source, ticker, timespan, page, invert)

    if backfilled_to is None or backfilled_to > floor:  # backward: to the plan floor
        stored = store.bar_range(source.name, ticker, timespan)
        end = _ms_to_date(stored[0]) if stored else today
        try:
            for page in _guard(source.bar_pages(fetch, floor, end, timespan, descending=True, asset=asset)):
                saved += _store_bars(store, source, ticker, timespan, page, invert)
        except NotEntitled:
            pass
        store.set_backfilled(source.name, ticker, floor)
    return saved


def sync_daily(
    source: DataSource, store: Store, ticker: str, floor: date, today: date,
    fetch: str | None = None, asset: str = "stock", invert: bool = False,
) -> int:
    """Official daily bars (include the closing auction), one request per run.

    Day/week/month charts use these rather than bars rebuilt from intraday data.
    """
    stored = store.bar_range(source.name, ticker, "day")
    start = max(floor, _ms_to_date(stored[1])) if stored else floor
    saved = 0
    try:
        for page in _guard(source.bar_pages(fetch or ticker, start, today, "day", asset=asset)):
            saved += _store_bars(store, source, ticker, "day", page, invert)
    except NotEntitled:
        pass
    return saved


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
        elif _ms_to_date(stored[1]) < end:
            pages = source.bar_pages(contract, _ms_to_date(stored[1]), end, resolution, asset="future")
        else:
            continue
        for page in _guard(pages):
            saved += store.upsert_bars(source.name, contract, resolution, 1, page)
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
    store.set_listing(source.name, ticker, inst.list_date, inst.figi, today, inst.name, inst.kind)
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
            ticker, asset = item["ticker"], item["asset"]
            if asset == "future":
                try:
                    n = sync_future(source, store, item, floor, today, log)
                except NotEntitled as e:
                    log(f"{ticker}: futures refused by your plan ({e.message}); skipped")
                    continue
                result.rows += n
                log(f"{ticker}: {n} rows")
                continue
            if asset == "stock":  # listing/identity checks only apply to listed securities
                listing = check_listing(source, store, item, today, log)
                if not listing.ok:
                    continue
            else:
                listing = Listing(True)
            item_floor = max(floor, listing.listed_from) if listing.listed_from else floor
            item = next(w for w in store.watchlist(source.name) if w["ticker"] == ticker)
            resolution, backfilled_to = item["resolution"], item["backfilled_to"]
            fetch, invert = item["source_ticker"], item["invert"]
            if resolution == "auto":
                resolution = resolve_resolution(source, store, ticker, log, asset=asset, fetch=fetch)
                backfilled_to = None
                if resolution is None:
                    log(f"{ticker}: no supported resolution on your plan; skipped")
                    continue
            try:
                if resolution == "trades":
                    n = sync_trades(source, store, ticker, item_floor, backfilled_to)
                else:
                    n = sync_bars(source, store, ticker, resolution, item_floor, today, backfilled_to,
                                  fetch=fetch, asset=asset, invert=invert)
            except NotEntitled as e:
                log(f"{ticker} {resolution}: refused by your plan ({e.message}); re-checking resolution next run")
                store.set_resolution(source.name, ticker, "auto")
                continue
            result.rows += n
            log(f"{ticker} {resolution}: {n} rows")
            if resolution != "day":
                result.rows += sync_daily(source, store, ticker, item_floor, today, fetch=fetch, asset=asset,
                                          invert=invert)
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
