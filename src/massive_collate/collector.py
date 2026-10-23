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
from .errors import BudgetExhausted, NotEntitled, RateLimited, SourceError
from . import freshness
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
    skipped: int = 0  # symbols refreshed recently enough to leave for a later run
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
    `fetch` is the source's own ticker when it differs (I:COMP, C:EURUSD, a futures contract).

    Entitlements belong to the plan and asset class, not the ticker, so an answer is reused
    for every symbol of that class for a week instead of probing each one again."""
    for resolution in RESOLUTIONS_BY_ASSET.get(asset, RESOLUTIONS):
        ok = known = store.entitlement(source.name, asset, resolution)
        if ok is None:
            try:
                ok = source.is_entitled(resolution, fetch or ticker, asset)
            except BudgetExhausted as e:
                raise StopRun(f"request cap reached ({e})") from e
            except RateLimited as e:
                raise StopRun(f"rate limited ({e.message})") from e
            store.set_entitlement(source.name, asset, resolution, ok)
        if ok:
            log(f"{ticker}: finest resolution on your plan is '{resolution}'")
            store.set_resolution(source.name, ticker, resolution)
            return resolution
        if known is None:  # only report what was actually asked; known answers cost nothing
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
    if checked and (today - checked).days < LISTING_RECHECK_DAYS and store.details(source.name, ticker):
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


def _backfilling(item: dict, floor: date) -> bool:
    if item["asset"] == "future":
        return item.get("synced_at") is None
    return item["backfilled_to"] is None or item["backfilled_to"] > max(floor, item["listed_from"] or floor)


def _due(asset: str, tier: Tier, synced: datetime, now: datetime, refresh_hours: float) -> bool:
    """Whether a fully synced symbol is worth asking again: its plan should have published
    data since the last sync (stocks on the free plan: once each morning; delayed futures:
    continuously), and at least `refresh_hours` have passed."""
    if refresh_hours and (now - synced).total_seconds() < refresh_hours * 3600:
        return False
    return freshness.expected(asset, tier, now).latest != freshness.expected(asset, tier, synced).latest


def sync(
    source: DataSource, store: Store, tier: Tier, now: datetime | None = None, log: Log = print,
    refresh_hours: float = 0.0, on_item: Callable[[str], None] | None = None, only: set[str] | None = None,
    directory_allowance: int = 0,
) -> SyncResult:
    """One scheduled run over the watchlist for a source.

    A fully synced symbol is skipped until its plan should have published new data (see
    `freshness`; on the free plan stocks only change once a day, so asking hourly would only
    spend requests) and, if set, `refresh_hours` have passed. Symbols still backfilling are
    never skipped. `on_item` is told which symbol is
    being worked on (for the activity view). `only` limits the run to those symbols and
    fetches them whatever the refresh interval (used by `scan --fetch`).
    """
    now = (now or now_et()).astimezone(ET)
    today = now.date()
    floor = history_floor(tier, today)
    result = SyncResult()
    try:
        sync_exchanges(source, store, log)
        refresh_calendars(source, store, today, log)
        fetch_news(source, store, now, log)
        from .gui.world import PROXIES
        fetch_market_moves(source, store, now, PROXIES, log)
        # Keep complete symbols current first; backfills take what budget is left.
        for item in sorted(store.watchlist(source.name), key=lambda w: (_backfilling(w, floor), w["ticker"])):
            ticker, asset = item["ticker"], item["asset"]
            synced = item.get("synced_at")
            if only is not None and ticker not in only:
                continue
            if only is None and synced and not _due(asset, tier, synced, now, refresh_hours):
                result.skipped += 1
                continue
            if on_item:
                on_item(ticker)
            spent = source.requests_made
            if asset == "future":
                try:
                    n = sync_future(source, store, item, floor, today, log)
                except NotEntitled as e:
                    log(f"{ticker}: futures refused by your plan ({e.message}); skipped")
                    continue
                result.rows += n
                log(f"{ticker}: {n} rows ({source.requests_made - spent} requests)")
                store.set_synced(source.name, ticker, now)
                continue
            if asset == "stock":  # listing/identity checks only apply to listed securities
                listing = check_listing(source, store, item, today, log)
                if not listing.ok:
                    continue
            else:
                check_reference(source, store, item, today, log)
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
                store.clear_entitlements(source.name)  # the plan may have changed: probe again
                continue
            result.rows += n
            if resolution != "day":
                n_day = sync_daily(source, store, ticker, item_floor, today, fetch=fetch, asset=asset, invert=invert)
                result.rows += n_day
                log(f"{ticker} {resolution}: {n} rows, daily: {n_day} rows ({source.requests_made - spent} requests)")
            else:
                log(f"{ticker} {resolution}: {n} rows ({source.requests_made - spent} requests)")
            store.set_synced(source.name, ticker, now)
            if asset == "stock":
                fetch_icon(source, store, ticker, log)
        if only is None and directory_allowance > 0:  # placeholders and icons: leftover budget only
            spent = source.requests_made
            refresh_directory(source, store, today, log)
            fill_directory_icons(source, store, directory_allowance - (source.requests_made - spent), today, log)
    except StopRun as e:
        result.stopped = str(e)
        log(f"stopped: {e}")
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
        except (SourceError, NotImplementedError) as e:
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
    except (NotEntitled, SourceError, NotImplementedError) as e:
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
    except (NotEntitled, SourceError, NotImplementedError) as e:
        log(f"calendar: upcoming holidays not available ({e})")
    products = sorted({w["source_ticker"] for w in store.watchlist(source.name) if w["asset"] == "future"})
    for product in products:
        try:
            n = store.save_futures_schedule(source.name, product, _guard_call(source.futures_schedule, product, today))
            log(f"calendar: {product} futures schedule, {n} session events")
        except (NotEntitled, SourceError, NotImplementedError) as e:
            log(f"calendar: {product} futures schedule not available ({e})")
    store.kv_set(key, today.isoformat())
    return source.requests_made - before


DIRECTORY_EXCHANGES = ("XNAS", "XNYS")
DIRECTORY_REFRESH_DAYS = 7
COMPANY_TYPES = ("CS", "ADRC", "OS", "GDR", "NYRS")  # operating companies (not funds, warrants, units...)
DAILY_RESERVE = 400  # requests of the daily cap kept back for the watchlist's own updates
DETAILS_REFRESH_DAYS = 90  # directory companies' details (and logo links) are re-checked this often


def refresh_directory(source: DataSource, store: Store, today: date, log: Log = print) -> int:
    """Weekly: every active Nasdaq and NYSE listing and the futures products, as placeholders."""
    key = f"directory:{source.name}"
    last = store.kv_get(key)
    if last and (today - date.fromisoformat(last)).days < DIRECTORY_REFRESH_DAYS:
        return 0
    before = source.requests_made
    try:
        rows = []
        for mic in DIRECTORY_EXCHANGES:
            rows += _guard_call(source.listings, mic)
        n = store.save_directory(source.name, rows, today)
        gone = store.prune_directory(source.name, "stock", today)
        log(f"directory: {n} listings on {', '.join(DIRECTORY_EXCHANGES)}" + (f", {gone} no longer listed" if gone else ""))
    except (NotEntitled, SourceError, NotImplementedError) as e:
        log(f"directory: listings not available ({e})")
    try:
        fut = _guard_call(source.futures_products)
        n = store.save_directory(source.name, fut, today)
        store.prune_directory(source.name, "future", today)
        log(f"directory: {n} futures products")
    except (NotEntitled, SourceError, NotImplementedError) as e:
        log(f"directory: futures products not available ({e})")
    store.kv_set(key, today.isoformat())
    return source.requests_made - before


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
    url = ((store.details(source.name, ticker) or {}).get("branding") or {}).get("icon_url")
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
    r = store.bar_edges(source, item["ticker"], "minute" if item["resolution"] == "auto" else item["resolution"])
    return (datetime.fromtimestamp(r[1] / 1000, ET) if r else None), item["source_ticker"]


def scan(
    source: DataSource | None, store: Store, tier: Tier, source_name: str | None = None,
    now: datetime | None = None, log: Log = print, probe_limit: int = 30, only: set[str] | None = None,
) -> ScanResult:
    """Check every watched symbol for data that should be available but is not stored.

    Free part: compare each symbol's newest stored bar with what its plan should have
    published by now (market calendar + publication delay). With a `source`, every symbol
    that looks behind costs one request (the source's newest bar, limit 1) to confirm the
    data really exists, up to `probe_limit` symbols.
    """
    now = (now or now_et()).astimezone(ET)
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
