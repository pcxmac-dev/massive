"""Which source delivers what: each source's capabilities and costs, read from its subscription, and
the delivery plan made from them (the user, 2026-10-27: "a scheme that interprets the capabilities
and cost of each source and optimizes data delivery based on cost and capability").

Jobs, in the user's order of priority: #1 daily bars, #2 1-minute bars, #3 1-second bars. The
current session is the live streams' (live.py), shown apart.

- **Capability** (`Offer`): what a source's plan gives for an asset class at one resolution - whether
  at all (its plugin's `resolutions_for`, and a plan probe that refused it), how far back (the plan's
  history floor; 1-second bars SECOND_HISTORY_DAYS), how fresh (real time, delayed, end of day) and
  which market its data is (the consolidated tape, one exchange's book, an official index file).
- **Cost**: a subscription's price is fixed; what a job spends is its limits. Requests per
  symbol-year (the bars a request returns) against the requests a day the subscription allows (its
  daily cap, or its rate where tighter; a monthly symbol cap too): days of its limits.
- **Unique or common**: per job and market, history is cut into eras where the set of sources
  reaching it changes. An era only one source reaches is unique to it: it delivers it, whatever it
  costs. An era several reach is common data (copies of the same market).
- **Delivery** (`build`): each source first takes its unique eras (that load is fixed); the common
  eras then go, the user's first job first, each to the source that would finish its whole load
  soonest with it (days of its limits), so cheap capacity does the common work and a scarce
  subscription is kept for what only it has; ties: cheaper, fresher, SOURCE_PRIORITY. Audits are
  shown beside each source, not used to order them: most fail today on differences between
  exchanges, not on bad copies.

The fetcher follows it (coverage.plan): in an era the first source downloads and the others stand
back, unless it does not watch the symbol or found nothing there. `python -m massive_collate
sourcing` shows it.
"""
from __future__ import annotations

import math
import threading
import time
import weakref
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from .coverage import FeedProfile, day_start_ms
from .sessions import ET

JOBS = ("day", "minute", "second")  # the user's order of priority
JOB_NAMES = {"day": "daily bars", "minute": "1-minute bars", "second": "1-second bars"}
DAYS_A_YEAR = {"stock": 252, "index": 252, "fx": 260, "crypto": 365, "future": 252}
MINUTES_A_DAY = {"stock": 960, "index": 390, "fx": 1440, "crypto": 1440, "future": 1380}  # extended hours
DEFAULT_CAPACITY = 2000.0  # requests a day, for a feed whose subscription is not known
CACHE_S = 300.0
SCAN_KEY = "capabilities:{}"  # a provider's last capability scan (cli `capabilities`): {at, results, plan}
COUNTS_S = 30.0  # the symbols watched are counted again this often
OPEN_END = 2**62  # the newest era runs on (ms)


def bars_a_year(asset: str, resolution: str) -> int:
    """Bars a symbol has in a year at a resolution (1-second: at most; a second without trades has none)."""
    days = DAYS_A_YEAR.get(asset, 252)
    if resolution == "day":
        return days
    minutes = days * MINUTES_A_DAY.get(asset, 960)
    return minutes if resolution == "minute" else minutes * 60


@dataclass(frozen=True)
class Offer:
    """What one source's subscription gives for one job: an asset class at one resolution."""
    source: str
    asset: str
    resolution: str
    market: str
    since: date  # its plan's history floor
    latency: float  # seconds behind real time (86,400: end of day)
    per_request: int | None  # bars a request returns (None: a symbol's whole history at once)
    capacity: float  # requests a day its subscription allows
    symbols_a_day: float | None  # a monthly symbol cap, a day's share
    priority: int  # SOURCE_PRIORITY
    accuracy: float | None = None  # audits passed (90 days)

    def requests(self, years: float) -> float:
        """Requests one symbol's `years` of history take."""
        if self.per_request is None:
            return 1.0
        return max(1.0, math.ceil(bars_a_year(self.asset, self.resolution) * years / self.per_request))

    def days(self, years: float, symbols: int) -> float:
        """Days of its subscription's limits that `symbols` symbols' `years` take."""
        d = symbols * self.requests(years) / max(self.capacity, 1e-9)
        return max(d, symbols / self.symbols_a_day) if self.symbols_a_day else d


@dataclass
class Era:
    """A stretch of one job's history in one market: who delivers it first, who else can."""
    asset: str
    resolution: str
    market: str
    start: date
    end: date | None  # exclusive; None: up to now
    sources: list[str]  # in order: the first delivers, the others stand in where it cannot
    unique: bool
    symbols: int  # watched there
    days: dict[str, float]  # source -> days of its limits the era takes

    def years(self, today: date) -> float:
        return max(((self.end or today) - self.start).days, 0) / 365.25

    def span_ms(self) -> tuple[int, int]:
        return day_start_ms(self.start), day_start_ms(self.end) if self.end else OPEN_END


@dataclass
class Scheme:
    today: date
    offers: list[Offer]
    eras: list[Era]
    load: dict[str, float]  # source -> days of its limits the eras it delivers take
    live: dict[str, list[tuple[str, str]]]  # asset class -> [(source, what)], the freshest first

    def order(self, asset: str, resolution: str, market: str | None) -> list[tuple[int, int, list[str]]]:
        """A job's eras in one market: [(start ms, end ms, sources in order)], oldest first."""
        return [(*e.span_ms(), e.sources) for e in self.eras
                if e.asset == asset and e.resolution == resolution and e.market == market]

    def as_dict(self) -> dict:
        return {"today": self.today.isoformat(), "jobs": list(JOBS),
                "offers": [{**o.__dict__, "since": o.since.isoformat()} for o in self.offers],
                "eras": [{"asset": e.asset, "resolution": e.resolution, "market": e.market,
                          "start": e.start.isoformat(), "end": e.end.isoformat() if e.end else None,
                          "sources": e.sources, "unique": e.unique, "symbols": e.symbols,
                          "days": {k: round(v, 3) for k, v in e.days.items()}} for e in self.eras],
                "load": {k: round(v, 3) for k, v in self.load.items()}, "live": self.live}


def reports(feeds: dict[str, FeedProfile], today: date, settings=None, scans: dict | None = None) -> dict:
    """Every configured provider's standard report (sources.registry.Report), by name; `scans`:
    their last capability scans (kv SCAN_KEY), shown in them."""
    from .sources import providers

    known = providers()
    return {name: known[name].report(f, today, settings, (scans or {}).get(name))
            for name, f in feeds.items() if name in known}


def offers(feeds: dict[str, FeedProfile], today: date, entitled=None, scores: dict | None = None) -> list[Offer]:
    """Every configured source's offer per job, read from its standard report alone.
    `entitled(source, asset, resolution)`: a plan probe's answer (None: not asked); a refusal takes
    the offer away."""
    out = []
    for name, r in reports(feeds, today).items():
        accuracy = ((scores or {}).get(name) or {}).get("accuracy")
        per_day = r.limits.symbols_a_month / 30 if r.limits.symbols_a_month else None
        for c in r.capabilities:
            if entitled and entitled(name, c.asset, c.resolution) is False:
                continue
            out.append(Offer(name, c.asset, c.resolution, c.market, c.since, c.latency, c.per_request,
                             r.limits.requests_a_day or DEFAULT_CAPACITY, per_day, feeds[name].priority, accuracy))
    return out


def build(feeds: dict[str, FeedProfile], counts: dict[tuple[str, str], int], today: date,
          entitled=None, scores: dict | None = None) -> Scheme:
    """The delivery plan (the module's docstring). `counts`: {(source, asset class): symbols watched}."""
    all_offers = offers(feeds, today, entitled, scores)
    by_job: dict[tuple[str, str, str], list[Offer]] = defaultdict(list)
    for o in all_offers:
        by_job[(o.asset, o.resolution, o.market)].append(o)
    eras: list[Era] = []
    for (asset, res, market), group in by_job.items():
        bounds = sorted({o.since for o in group} | {today})
        for start, end in zip(bounds, bounds[1:] + [None]):
            if end is None and start >= today:
                continue
            reach = [o for o in group if o.since <= start]
            if not reach:
                continue
            symbols = max(counts.get((o.source, asset), 0) for o in reach)
            years = max(((end or today) - start).days, 0) / 365.25
            era = Era(asset, res, market, start, None if end == today else end, [], len(reach) == 1, symbols,
                      {o.source: o.days(years, max(symbols, 1)) for o in reach})
            era.sources = [o.source for o in sorted(reach, key=lambda o: (era.days[o.source], o.latency, o.priority,
                                                                          o.source))]
            eras.append(era)
    eras = [e for e in eras if e.end is None or e.start < e.end]
    load: dict[str, float] = defaultdict(float)
    offered = {(o.source, o.asset, o.resolution, o.market): o for o in all_offers}
    eras.sort(key=lambda e: (JOBS.index(e.resolution), e.asset, e.market, e.start))
    for job in JOBS:  # the user's order: a job's load never waits on a later job's
        mine = [e for e in eras if e.resolution == job]
        for e in mine:
            if e.unique:
                load[e.sources[0]] += e.days[e.sources[0]]
        for e in (e for e in mine if not e.unique):
            def key(name: str, e=e):
                o = offered[(name, e.asset, e.resolution, e.market)]
                return load[name] + e.days[name], e.days[name], o.latency, o.priority, name

            e.sources = sorted(e.sources, key=key)
            load[e.sources[0]] += e.days[e.sources[0]]

    live: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    for name, r in sorted(reports(feeds, today).items(), key=lambda kv: feeds[kv[0]].priority):
        for asset, what in r.streams:
            live[asset].append((0.0, name, f"stream: {what}"))
        for asset in sorted({c.asset for c in r.capabilities}):
            lag = min(c.latency for c in r.capabilities if c.asset == asset)
            what = "real time" if lag < 60 else f"{lag / 60:.0f} min delayed" if lag < 86_400 else "end of day"
            live[asset].append((lag + 1, name, f"requests: {what}"))
    return Scheme(today, all_offers, eras, dict(load),
                  {a: [(n, w) for _, n, w in sorted(rows, key=lambda r: r[0])] for a, rows in live.items()})


_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_cache_lock = threading.Lock()


def standing(store, feeds: dict[str, FeedProfile], now: datetime | None = None) -> Scheme:
    """The delivery plan for this store's watchlist (built again when the feeds, the symbols watched
    or the day change, and every CACHE_S: the plan probes' answers and the audits move slowly)."""
    today = (now or datetime.now(ET)).astimezone(ET).date()
    with _cache_lock:
        hit = _cache.get(store) or {}
    counts = hit.get("counts")
    if counts is None or time.monotonic() - hit["counted"] >= COUNTS_S:  # the plan runs per symbol: a count a while
        counts, hit = store.watch_counts(), {**hit, "counted": time.monotonic()}
        hit["counts"] = counts
    key = (today, tuple(sorted(counts.items())), tuple(sorted(
        (n, f.tier, tuple(sorted(f.assets)), tuple(sorted(f.venues.items())), f.capacity, f.priority, f.second_days)
        for n, f in feeds.items())))
    if hit.get("key") == key and time.monotonic() - hit["built"] < CACHE_S:
        scheme = hit["scheme"]
    else:
        scheme = build(feeds, counts, today, entitled=lambda s, a, r: store.entitlement(s, a, r),
                       scores=store.audit_scores())
        hit = {**hit, "key": key, "built": time.monotonic(), "scheme": scheme}
    with _cache_lock:
        _cache[store] = hit
    return scheme


# -- the plans the providers' scans found (each plugin refreshes its own plan info) -----------------

def detected_plans(scans: dict) -> dict[str, str]:
    """{plan variable: value} from the providers' last capability scans ({name: kv SCAN_KEY}), each
    plugin's own plan variables only, in the providers' order (registry.Provider.run_scan)."""
    from .sources import providers

    out: dict[str, str] = {}
    for name, p in providers().items():
        plan = ((scans or {}).get(name) or {}).get("plan") or {}
        for var, value in plan.items():
            if var in p.plan_vars and value and var not in out:
                out[var] = str(value)
    return out


def scans_at(db_path) -> dict:
    """The providers' last capability scans held in a database, read without opening it for
    writing (every command reads them at its start); {} when there is none yet."""
    import json
    import sqlite3
    from pathlib import Path

    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT key, value FROM kv WHERE key LIKE 'capabilities:%'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for key, value in rows:
        try:
            out[key.split(":", 1)[1]] = json.loads(value)
        except (ValueError, TypeError):
            continue
    return out


def apply_detected(settings):
    """The settings with the plans the providers' scans found, where the user set none
    (config.with_detected)."""
    from .config import with_detected

    return with_detected(settings, detected_plans(scans_at(settings.db_path)))


def _days(d: float) -> str:
    return "under a minute" if d < 1 / 1440 else f"{d * 24:.1f} h" if d < 1 else f"{d:,.1f} days"


def report(scheme: Scheme, reports: dict) -> str:
    """The scheme for the terminal: each job's eras (who delivers, unique or common, at what cost),
    each provider's standard report with the load the scheme gives it, and the current session."""
    lines = []
    for i, job in enumerate(JOBS, 1):
        lines.append(f"#{i} {JOB_NAMES[job]}")
        eras = [e for e in scheme.eras if e.resolution == job]
        if not eras:
            lines.append("    no source offers them here")
        for e in eras:
            first, rest = e.sources[0], e.sources[1:]
            span = f"{e.start} to {e.end or 'now'}"
            who = (f"{first} only (unique)" if e.unique else
                   f"{first}, then {', '.join(rest)} (common)")
            cost = "; ".join(f"{s} {_days(e.days[s])}" for s in e.sources)
            lines.append(f"    {e.asset:6} {e.market:12} {span:28} {who:44} {e.symbols:,} symbols: {cost}")
    lines.append("Providers (each one's standard report, and the load the eras it delivers take of its limits)")
    accuracy = {o.source: o.accuracy for o in scheme.offers}
    for name, r in reports.items():
        lines += ["    " + line for line in r.lines()]
        acc = accuracy.get(name)
        lines.append(f"        load: {_days(scheme.load.get(name, 0.0))} of its limits"
                     + (f"; audits passed (90 days): {acc:.0%}" if acc is not None else ""))
    lines.append("Current session (live streams follow the symbols open on a chart)")
    for asset, rows in sorted(scheme.live.items()):
        lines.append(f"    {asset:6} " + "; ".join(f"{n}: {w}" for n, w in rows))
    return "\n".join(lines)
