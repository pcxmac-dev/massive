"""Dependency health: every external thing the app relies on, checked against its live state.

Each check returns a row for the dependency register (docs/DEPENDENCIES.md): status OK / WARN /
FAIL, what was found, and what to do. Nothing here calls the API; it reads what the fetcher
stored and the installed packages and files.

    python -m massive_collate deps
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .storage import Store

STATIC = Path(__file__).parent / "gui" / "static"


@dataclass
class Check:
    area: str
    item: str
    status: str  # OK | WARN | FAIL
    found: str
    action: str = ""


def _age_days(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc) if "T" in iso else datetime.combine(t.date(), datetime.min.time(), timezone.utc)
    return (now - t).total_seconds() / 86400


def _fresh(area, item, iso, now, warn_days, fail_days, what, action) -> Check:
    age = _age_days(iso, now)
    if age is None:
        return Check(area, item, "WARN", f"{what}: never", action)
    status = "OK" if age <= warn_days else "WARN" if age <= fail_days else "FAIL"
    return Check(area, item, status, f"{what}: {age:.1f} days ago", "" if status == "OK" else action)


def coverage_overlaps(store: Store, same_market=None) -> list[str]:
    """Downloaded spans of a series held by more than one feed of the same market (should never
    happen: only audits fetch twice). `same_market(feed_a, feed_b, ticker)`: two exchanges'
    streams of one pair (Coinbase's and Bitstamp's Bitcoin) are different markets, so both hold
    it. Bars built here from a feed's ticks may cover a span another feed downloaded at a coarser
    resolution: that is an upgrade, not a second download."""
    rows = store.conn.execute("SELECT ticker, timespan, source, start_ms, end_ms FROM coverage "
                              "WHERE origin='download' ORDER BY ticker, timespan, start_ms").fetchall()
    out = []
    reach: dict[tuple, tuple[str, int]] = {}  # (ticker, timespan) -> (feed, furthest end so far)
    for ticker, timespan, src, a, b in rows:
        prev = reach.get((ticker, timespan))
        if prev and a < prev[1] and prev[0] != src and (same_market is None or same_market(prev[0], src, ticker)):
            out.append(f"{ticker} {timespan} {prev[0]}/{src} from {datetime.fromtimestamp(a / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC")
        if not prev or b > prev[1]:
            reach[(ticker, timespan)] = (src, b)
    return out


def checks(store: Store, settings, source: str = "massive", now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    out: list[Check] = []

    # -- data source access ---------------------------------------------------------
    out.append(Check("Massive API", "API key", "OK" if settings.api_key else "FAIL",
                     "set" if settings.api_key else "MASSIVE_API_KEY not set",
                     "" if settings.api_key else "set MASSIVE_API_KEY (environment or .env)"))
    runs = store.runs(50)
    ok = next((r for r in runs if r["status"] == "ok" and r["command"] == "sync"), None)
    out.append(_fresh("Massive API", "Scheduled sync", ok["finished_at"] if ok else None, now, 0.2, 1,
                      "last successful sync", "check the MassiveCollateSync task and the log"))
    errors = [r for r in runs[:10] if r["status"] == "error"]
    out.append(Check("Massive API", "Recent errors", "WARN" if errors else "OK",
                     f"{len(errors)} of the last 10 runs failed" + (f": {errors[0]['message'][:80]}" if errors else ""),
                     "read the log; a plan change can refuse an endpoint" if errors else ""))
    used = store.requests_today(source)
    out.append(Check("Massive API", "Daily request cap", "OK" if used < settings.max_requests_per_day * 0.9 else "WARN",
                     f"{used} of {settings.max_requests_per_day} used today"))

    # -- second feed and the one-feed-per-span rule ---------------------------------
    a = getattr(settings, "alpaca", None)
    if a is not None:
        out.append(Check("Alpaca", "API keys", "OK" if a.has_keys else "WARN",
                         f"set ({a.tier.name}, feed {a.feed}, crypto from {a.crypto_loc})" if a.has_keys
                         else "not set: crypto only (it needs no key)",
                         "" if a.has_keys else "set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (environment) "
                         "for stock history from 2016 and the session up to 15 min ago"))
        used = store.requests_today("alpaca")
        out.append(Check("Alpaca", "Daily request cap", "OK" if used < a.max_requests_per_day * 0.9 else "WARN",
                         f"{used} of {a.max_requests_per_day} used today"))
    import shutil

    free_gb = shutil.disk_usage(store.feeds_dir if store.feeds_dir and store.feeds_dir.is_dir() else Path.home()).free / 1e9
    min_free = getattr(settings, "tick_min_free_gb", 250)
    paused = store.kv_get("ticks:backfill-paused")
    out.append(Check("Ticks", "Tick files and disk", "WARN" if paused or free_gb < min_free * 2 else "OK",
                     f"{store.ticks_bytes() / 1e9:.1f} GB in the feeds' tick files, "
                     f"{free_gb:,.0f} GB free" + (f"; backfill paused ({paused})" if paused else ""),
                     f"tick backfill pauses under {min_free} GB free (TICK_MIN_FREE_GB); "
                     "free space or lower TICK_HISTORY_DAYS" if paused or free_gb < min_free * 2 else ""))
    rules = store.condition_rules()
    have_ticks = any(k == "trades" for _, _, k in store.stored_series())
    out.append(Check("Ticks", "Trade condition rules", "OK" if rules or not have_ticks else "WARN",
                     f"{len(rules)} tape condition codes with bar rules" if rules else "none stored yet",
                     "" if rules or not have_ticks else
                     "fetched weekly with the Massive sync; until then bars from ticks count every trade"))
    from .sources import providers

    for name, p in sorted(providers().items()):
        if not p.archival:
            continue
        rows = store.watchlist(name)
        shut = sum(1 for w in rows if w.get("closed_at"))
        keyless = p.keys_set(settings) is False
        status = "WARN" if keyless and rows else "OK"
        found = ("nothing watched" if not rows else "closed: archive complete" if shut == len(rows)
                 else f"{shut} of {len(rows)} symbols complete")
        out.append(Check("Public archives", name, status, found + (f"; {p.key_hint} not set" if keyless else ""),
                         f"set {p.key_hint} (from {p.signup or p.title}) for {p.about}"
                         if keyless and rows else ""))
    from .coverage import same_market
    from .sources import feed_profiles

    profiles = feed_profiles(settings)
    assets = {w["ticker"]: w["asset"] for w in store.watchlist()}
    overlaps = coverage_overlaps(store, lambda a, b, t: same_market(profiles, a, b, assets.get(t, "stock"))
                                 and a in profiles and b in profiles)
    out.append(Check("Feeds", "One feed per span", "OK" if not overlaps else "FAIL",
                     "no span is held by two feeds" if not overlaps else
                     f"{len(overlaps)} overlapping spans, e.g. {overlaps[0]}",
                     "" if not overlaps else "a second download happened outside an audit: report it"))
    scores = store.audit_scores()
    if scores:
        worst = min(scores.items(), key=lambda kv: kv[1]["accuracy"] if kv[1]["accuracy"] is not None else 1)
        acc = worst[1]["accuracy"]
        out.append(Check("Feeds", "Audits (90 days)", "OK" if acc is None or acc >= 0.9 else "WARN",
                         ", ".join(f"{f}: {s['passed']}/{s['audits']} passed" for f, s in sorted(scores.items())),
                         "" if acc is None or acc >= 0.9 else "open the Fetcher view's Audits to see where the feeds differ"))

    # -- feeds the app keeps itself ------------------------------------------------
    out.append(_fresh("Reference data", "Exchange holidays + futures schedules", store.kv_get(f"calendar:{source}"),
                      now, 8, 15, "refreshed", "runs weekly with the sync; check the log for 'calendar:'"))
    out.append(_fresh("Reference data", "Headlines", store.kv_get(f"news:{source}"), now, 0.1, 1,
                      "fetched", "runs with each sync; check the log for 'news:'"))
    days = store.proxy_days(source)
    out.append(_fresh("Reference data", "Market moves (proxy funds)", days[-1].isoformat() if days else None, now,
                      4, 8, "newest day", "runs with each sync once a session is published"))
    out.append(_fresh("Reference data", "Directory of Nasdaq/NYSE listings", store.kv_get(f"directory:{source}"),
                      now, 8, 15, "listed", "runs weekly with the sync; check the log for 'directory:'"))

    # Proxy funds the globe colours by: still trading? (absent from the latest market snapshot)
    from .gui.world import PROXIES

    latest = store.conn.execute("SELECT MAX(day) FROM market_snapshot WHERE source=?", (source,)).fetchone()[0]
    if latest:
        present = {r[0] for r in store.conn.execute(
            "SELECT ticker FROM market_snapshot WHERE source=? AND day=?", (source, latest))}
        missing = [p for p in PROXIES if p not in present]
        out.append(Check("Reference data", "Proxy funds still trading", "WARN" if missing else "OK",
                         f"missing on {latest}: {', '.join(missing)}" if missing else f"all {len(PROXIES)} traded on {latest}",
                         "replace the fund in gui/world.py (delisted or renamed)" if missing else ""))

    # -- icons ----------------------------------------------------------------------
    from .collector import COMPANY_TYPES

    watched = [w["ticker"] for w in store.watchlist(source) if w["asset"] == "stock" and w.get("synced_at")]
    have = store.icon_tickers()
    without = [t for t in watched if t not in have]
    out.append(Check("Icons", "Watched stocks with a logo", "OK" if not without else "WARN",
                     f"{len(watched) - len(without)} of {len(watched)}" + (f"; missing: {', '.join(without[:8])}" if without else ""),
                     "fetched after each stock's backfill; companies without branding stay lettered" if without else ""))
    s = store.directory_stats(source, COMPANY_TYPES)
    if s["companies"]:
        out.append(Check("Icons", "Directory company logos", "OK" if s["iconsChecked"] >= s["companies"] else "WARN",
                         f"{s['icons']} logos, {s['iconsChecked']} of {s['companies']} companies checked",
                         "fills from leftover budget each sync (2 requests per company)"))
    changed = store.icon_link_changes(source)
    out.append(Check("Icons", "Logos with a newer link", "OK" if not changed else "WARN",
                     f"{len(changed)} companies rebranded since their logo was saved",
                     "re-fetched automatically with leftover budget" if changed else ""))

    # -- calendars --------------------------------------------------------------------
    try:
        import exchange_calendars as xc

        from .gui import world

        short = []
        for e in world.EXCHANGES:
            if e.kind == "crypto":  # around the clock: no calendar
                continue
            cal = world._calendar(e.calendar or e.code)
            last = cal.sessions[-1].date()
            if (last - now.date()).days < 60:
                short.append(f"{e.code} to {last}")
        out.append(Check("Calendars", f"exchange_calendars {xc.__version__} coverage",
                         "WARN" if short else "OK",
                         f"holidays recorded less than 60 days ahead: {', '.join(sorted(set(short)))}" if short
                         else "every exchange has at least 60 days of holidays",
                         "upgrade exchange_calendars (pip install -U exchange_calendars) once the "
                         "exchanges publish next year's holidays" if short else ""))
    except Exception as e:  # missing package or a calendar that fails to build
        out.append(Check("Calendars", "exchange_calendars", "FAIL", str(e), "pip install -U exchange_calendars"))
    out.append(_nyse_rules_check(store, source, now))
    try:
        import tzdata

        out.append(Check("Calendars", "tz database (Python tzdata)", "OK", f"version {tzdata.IANA_VERSION}",
                         "pip install -U tzdata after governments change time-zone rules"))
    except Exception as e:
        out.append(Check("Calendars", "tz database (Python tzdata)", "FAIL", str(e), "pip install tzdata"))

    # -- vendored files ---------------------------------------------------------------
    for rel, what in (("vendor/klinecharts/klinecharts.min.js", "KLineChart (charting)"),
                      ("vendor/world/countries-110m.json", "world-atlas 110m outlines"),
                      ("vendor/world/countries-50m.json", "world-atlas 50m outlines"),
                      ("vendor/world/countries-10m.json", "world-atlas 10m outlines"),
                      ("vendor/world/coast-h/index.json", "GSHHG high-resolution coast tiles"),
                      ("vendor/world/time-zones.json", "civil time-zone grid"),
                      ("vendor/sky/stars.6.json", "d3-celestial stars"),
                      ("vendor/sky/constellations.lines.json", "d3-celestial constellations")):
        f = STATIC / rel
        out.append(Check("Vendored files", what, "OK" if f.exists() else "FAIL",
                         f"{f.stat().st_size // 1024} KB" if f.exists() else "missing",
                         "" if f.exists() else "see docs/DEPENDENCIES.md for the source URL"))
    tzgrid = STATIC / "vendor/world/time-zones.json"
    if tzgrid.exists():
        age = (now.timestamp() - os.path.getmtime(tzgrid)) / 86400
        out.append(Check("Vendored files", "time-zone grid age", "OK" if age < 400 else "WARN", f"built {age:.0f} days ago",
                         "rebuild with tools/build_timezones.py if zone borders changed" if age >= 400 else ""))
    return out


def _nyse_rules_check(store: Store, source: str, now: datetime) -> Check:
    """The rule-based NYSE calendar (sessions.py) against the source's official upcoming list."""
    from .sessions import MarketCalendar

    rules = MarketCalendar()  # rules only, no overrides
    official = store.market_holidays(source)
    if not official:
        return Check("Calendars", "NYSE rules vs official holidays", "WARN", "no official list stored yet",
                     "fetched weekly with the sync")
    diffs = []
    for day, status, _o, close_ms in official:
        if day < now.date():
            continue
        if status == "closed" and rules.is_trading_day(day):
            diffs.append(f"{day} closed (rules say open)")
        if status == "early-close" and close_ms:
            close = datetime.fromtimestamp(close_ms / 1000, timezone.utc)
            mine = rules.regular_hours(day)
            if mine and abs((mine[1] - close).total_seconds()) > 60:
                diffs.append(f"{day} closes {close:%H:%M} UTC (rules {mine[1].astimezone(timezone.utc):%H:%M})")
    return Check("Calendars", "NYSE rules vs official holidays", "WARN" if diffs else "OK",
                 "; ".join(diffs) if diffs else f"{len(official)} upcoming closures/early closes agree",
                 "the official list overrides the rules automatically; add lasting changes to sessions.py" if diffs else "")


def as_rows(items: list[Check]) -> list[dict]:
    return [asdict(c) for c in items]
