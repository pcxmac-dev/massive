"""Settings and plan tiers.

Settings come from environment variables, optionally loaded from a `.env` file
(variables already set in the environment win):

    MASSIVE_API_KEY   required for any API call
    MASSIVE_TIER      free | starter | developer | advanced   (default: free): your Stocks plan; sets
                      the history the collector reaches back to. `python -m massive_collate plan` finds it
    MASSIVE_TIER_INDICES / _CURRENCIES / _CRYPTO / _FUTURES   the plan of each other asset class
                      (Massive sells one per class; default: free)
    MASSIVE_RPM       override the tier's requests-per-minute limit (0 = unlimited)
    MASSIVE_DB        path of the SQLite database (default: ~/MassiveData/massive.db)
    MASSIVE_BASE_URL  API base URL (default: https://api.massive.com)
    MASSIVE_MAX_REQUESTS_PER_RUN   hard cap on API requests in one command (default: 250)
    MASSIVE_MAX_REQUESTS_PER_DAY   hard cap on API requests per day, all runs (default: 2000)
    MASSIVE_REFRESH_HOURS  extra minimum hours between refreshes of a backfilled symbol (default: 0;
                           symbols are already only refreshed when their plan should have new data)
    SOURCE_PRIORITY   feed order when sources overlap (default: massive,alpaca); a span is downloaded by
                      one feed only, ranked by resolution, then audited accuracy, then this order
    AUDITS_PER_RUN    spot audits of another feed's newly downloaded data per feed run (default: 2)
    AUDITS_RANDOM_PER_RUN   spot audits of a random stored session per feed run (default: 1); each
                      compares one random hour, and a run's audits use at most 2 % of its requests
    SECOND_HISTORY_DAYS how far back 1-second bars are backfilled where a feed has them (default:
                      365); minute bars cover all the history (fetched first), back to the plan's start
    TICK_HISTORY_DAYS (no longer used for history: no ticks are downloaded, only the live streams'
                      trades are kept, to build their bars)
    TICK_MIN_FREE_GB  tick backfill pauses when the disk has less free space (default: 250); below
                      a fifth of it no ticks are downloaded at all

Public historical sources (closed feeds: they fill history nobody else holds, then are not asked
again): Bitstamp (crypto minute bars from 2011) and Cboe (index daily history) need nothing;
    TIINGO_API_KEY    a free tiingo.com account key: US stock daily bars from 1962 (environment only)
    CRYPTO_VENUE_DAYS limit the crypto exchange feeds (Coinbase, Bitfinex) to this many days of history
                      (default: 0, everything they hold: Coinbase from 2015, Bitfinex from 2013)

Alpaca (second feed; stocks and crypto). Keys are only ever read from the environment:
    ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY   (Alpaca's own APCA_API_KEY_ID / APCA_API_SECRET_KEY
                      also work). Without them only crypto is collected (it needs no key).
    ALPACA_PLAN       basic | algo_trader_plus   (default: basic)
    ALPACA_RPM        override the plan's requests-per-minute limit
    ALPACA_FEED       sip (all US exchanges, default) | iex
    ALPACA_CRYPTO_LOC us-1 (Kraken, default) | eu-1 (Kraken EU) | us (Alpaca's own venue)
    ALPACA_PAPER      1 (default): keys are paper-trading keys; 0: live keys (decides the host
                      asked for reference data; market data is the same either way)
    LIVE_STOCKS       on (default) | off: with keys, the live recorder streams IEX's trades for the
                      symbols open on a chart and on the watchlists (at most 30; live.py)
    ALPACA_MAX_REQUESTS_PER_RUN / ALPACA_MAX_REQUESTS_PER_DAY   hard caps (default: 3000 / 30000)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Mapping, MutableMapping

DEFAULT_BASE_URL = "https://api.massive.com"
# Stock data lives on this machine, outside the repository, so it never ends up in git.
DEFAULT_DB_PATH = Path.home() / "MassiveData" / "massive.db"


@dataclass(frozen=True)
class Tier:
    name: str
    requests_per_minute: int | None  # None = unlimited
    history_years: int | None  # None = no plan limit
    history_start: date | None = None  # the source holds nothing older (Alpaca: 2016)
    history_days: int | None = None  # collect only this far back (a case-study venue feed)
    # Plans of other asset classes on the same source ((asset, Tier), ...): Massive sells one per class.
    by_asset: tuple = ()


# Backfill depth used when a plan has no history limit and its source no known start.
UNLIMITED_BACKFILL_YEARS = 20

# Stocks plan limits. Check your plan on massive.com and override with
# MASSIVE_RPM if yours differs.
TIERS: dict[str, Tier] = {
    "free": Tier("free", requests_per_minute=5, history_years=2),
    "starter": Tier("starter", requests_per_minute=None, history_years=5),
    "developer": Tier("developer", requests_per_minute=None, history_years=10),
    # Advanced: all of Massive's stock history, which begins 2003-09-10 (`plan`, 2026-10-24).
    "advanced": Tier("advanced", requests_per_minute=None, history_years=None, history_start=date(2003, 9, 10)),
}


def asset_tier(tier: Tier, asset: str | None) -> Tier:
    """The plan for one asset class: the tier's own, unless it carries another for that class."""
    for a, t in tier.by_asset if asset else ():
        if a == asset:
            return t
    return tier


# The variables naming Massive's plan for each asset class besides stocks (MASSIVE_TIER).
MASSIVE_CLASS_TIERS = {"index": "MASSIVE_TIER_INDICES", "fx": "MASSIVE_TIER_CURRENCIES",
                       "crypto": "MASSIVE_TIER_CRYPTO", "future": "MASSIVE_TIER_FUTURES"}


def tier_for(reach: dict[str, bool]) -> str:
    """The Massive stocks plan (TIERS) a plan probe's reach matches (MassiveSource.plan_reach):
    Advanced has quotes and more than 10 years, Developer trades and 10 years, Starter 5 years,
    the free plan 2."""
    if reach.get("quotes") or reach.get("day_11y") or reach.get("day_21y"):
        return "advanced"
    if reach.get("trades") or reach.get("day_6y"):
        return "developer"
    if reach.get("day_3y") or reach.get("second"):
        return "starter"
    return "free"


# Alpaca market data plans (docs.alpaca.markets, Oct 2026). Basic is free: every US exchange (SIP)
# except the latest 15 minutes, 200 requests/min; both plans go back to 2016. A little under the
# published rate so the limiter never meets a 429.
ALPACA_TIERS: dict[str, Tier] = {
    "basic": Tier("alpaca-basic", requests_per_minute=190, history_years=None, history_start=date(2016, 1, 1)),
    "algo_trader_plus": Tier("alpaca-plus", requests_per_minute=9500, history_years=None,
                             history_start=date(2016, 1, 1)),
}
ALPACA_FEEDS = ("sip", "iex")

# Public historical sources: (plan, max requests per run, per day). Bitstamp allows 8,000 requests
# per 10 minutes, Tiingo's free plan 50 an hour and 1,000 a day; well inside both.
PUBLIC_FEEDS: dict[str, tuple[Tier, int, int]] = {
    "bitstamp": (Tier("bitstamp-public", requests_per_minute=300, history_years=None,
                      history_start=date(2011, 8, 1)), 8000, 20000),
    # Crypto exchange feeds (also the composite / reference models' inputs): all the history they hold,
    # unless CRYPTO_VENUE_DAYS limits it. Coinbase allows 10 public requests a second, Bitfinex about
    # 30 candle requests a minute.
    "coinbase": (Tier("coinbase-public", requests_per_minute=300, history_years=None, history_start=date(2015, 1, 26)),
                 3000, 20000),
    "bitfinex": (Tier("bitfinex-public", requests_per_minute=20, history_years=None, history_start=date(2013, 4, 1)),
                 300, 2000),
    "cboe": (Tier("cboe-public", requests_per_minute=30, history_years=None, history_start=date(1990, 1, 1)), 50, 200),
    "tiingo": (Tier("tiingo-free", requests_per_minute=None, history_years=None, history_start=date(1962, 1, 1)), 45, 900),
}
ALPACA_CRYPTO_LOCS = ("us-1", "eu-1", "us")

# What each server publishes, shown beside the window the app keeps (sources.rate_window).
# Massive's depends on the plan: see TIERS.
PUBLISHED_LIMITS: dict[str, str] = {
    "alpaca": "Basic (free): 200 requests per minute; Algo Trader Plus: 10,000",
    "bitstamp": "8,000 requests per 10 minutes",
    "coinbase": "10 requests per second per IP (public endpoints)",
    "bitfinex": "about 30 candle requests per minute",
    "cboe": "none published (a public file server)",
    "tiingo": "free plan: 50 requests per hour, 1,000 per day",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    tier: Tier
    db_path: Path
    base_url: str = DEFAULT_BASE_URL
    max_requests_per_run: int = 250
    max_requests_per_day: int = 2000
    refresh_hours: float = 0.0
    # Feed order when several sources cover the same ticker (SOURCE_PRIORITY, comma-separated).
    source_priority: tuple[str, ...] = ("massive", "alpaca")

    alpaca: "AlpacaSettings | None" = None
    audits_per_run: int = 2
    audits_random_per_run: int = 1
    tick_history_days: int = 365
    second_history_days: int = 365
    tick_min_free_gb: int = 250
    tiingo_api_key: str | None = field(default=None, repr=False)
    crypto_venue_days: int = 0  # 0: all the history the exchange feeds hold
    # Coins recorded live from the exchanges' public streams (live.py); LIVE_CRYPTO=off for none.
    live_coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "LINK")
    # Stocks streamed live from Alpaca's IEX feed (with its keys; live.py); LIVE_STOCKS=off for none.
    live_stocks: bool = True
    # MASSIVE_OFFLINE=1: nothing is fetched and no recorder starts (a test copy of the app on a
    # scratch database must never spend the real plan's requests: its own ledger would not know them).
    offline: bool = False

    # The settings a feed view was made from, so any view can switch to another feed.
    origin: "Settings | None" = field(default=None, compare=False, repr=False)

    def for_source(self, name: str) -> "Settings":
        """These settings as seen by one feed: its plan, rate and request caps."""
        base = self.origin or self
        if name in PUBLIC_FEEDS:
            tier, per_run, per_day = PUBLIC_FEEDS[name]
            if name in ("coinbase", "bitfinex") and base.crypto_venue_days:
                tier = replace(tier, history_days=base.crypto_venue_days)
            return replace(base, tier=tier, max_requests_per_run=per_run, max_requests_per_day=per_day, origin=base)
        if name == "alpaca" and base.alpaca:
            a = base.alpaca
            return replace(base, tier=a.tier, max_requests_per_run=a.max_requests_per_run,
                           max_requests_per_day=a.max_requests_per_day, origin=base)
        return base

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "MASSIVE_API_KEY is not set. Set it in your environment or in a .env file "
                "(see .env.example)."
            )
        return self.api_key


@dataclass(frozen=True)
class AlpacaSettings:
    key_id: str | None
    secret_key: str | None
    tier: Tier
    feed: str = "sip"
    crypto_loc: str = "us-1"
    paper: bool = True
    max_requests_per_run: int = 3000
    max_requests_per_day: int = 30000

    @property
    def has_keys(self) -> bool:
        return bool(self.key_id and self.secret_key)

    def __repr__(self) -> str:  # never print the keys
        return (f"AlpacaSettings(keys={'set' if self.has_keys else 'not set'}, tier={self.tier.name}, "
                f"feed={self.feed}, crypto_loc={self.crypto_loc}, paper={self.paper})")


def _alpaca(merged: Mapping[str, str]) -> AlpacaSettings:
    plan = (merged.get("ALPACA_PLAN") or "basic").strip().lower()
    if plan not in ALPACA_TIERS:
        raise ConfigError(f"Unknown ALPACA_PLAN {plan!r}; choose one of {', '.join(ALPACA_TIERS)}")
    tier = ALPACA_TIERS[plan]
    rpm = _int_setting(merged, "ALPACA_RPM")
    if rpm is not None:
        tier = replace(tier, requests_per_minute=rpm or None)
    feed = (merged.get("ALPACA_FEED") or "sip").strip().lower()
    if feed not in ALPACA_FEEDS:
        raise ConfigError(f"Unknown ALPACA_FEED {feed!r}; choose one of {', '.join(ALPACA_FEEDS)}")
    loc = (merged.get("ALPACA_CRYPTO_LOC") or "us-1").strip().lower()
    if loc not in ALPACA_CRYPTO_LOCS:
        raise ConfigError(f"Unknown ALPACA_CRYPTO_LOC {loc!r}; choose one of {', '.join(ALPACA_CRYPTO_LOCS)}")
    per_run = _int_setting(merged, "ALPACA_MAX_REQUESTS_PER_RUN")
    per_day = _int_setting(merged, "ALPACA_MAX_REQUESTS_PER_DAY")
    return AlpacaSettings(
        key_id=merged.get("ALPACA_API_KEY_ID") or merged.get("APCA_API_KEY_ID") or None,
        secret_key=merged.get("ALPACA_API_SECRET_KEY") or merged.get("APCA_API_SECRET_KEY") or None,
        tier=tier, feed=feed, crypto_loc=loc,
        paper=(merged.get("ALPACA_PAPER") or "1").strip().lower() not in ("0", "false", "no"),
        max_requests_per_run=3000 if per_run is None else per_run,
        max_requests_per_day=30000 if per_day is None else per_day,
    )


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Blank lines and # comments are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# API keys set as user environment variables after a process started (the chart app, the runs it
# starts) are not in its environment; on Windows they are read from the user's saved variables.
KEY_NAMES = ("MASSIVE_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "APCA_API_KEY_ID",
             "APCA_API_SECRET_KEY", "TIINGO_API_KEY")
# Changed when a plan is bought (and its caps with it): read the same way.
PLAN_NAMES = ("MASSIVE_TIER", *MASSIVE_CLASS_TIERS.values(), "MASSIVE_MAX_REQUESTS_PER_RUN",
              "MASSIVE_MAX_REQUESTS_PER_DAY", "ALPACA_PLAN")


def saved_user_keys() -> dict[str, str]:
    """The API keys and plan names among the user's saved environment variables (the Windows
    registry's user Environment key): a process started before they were saved still sees them."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        out = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in KEY_NAMES + PLAN_NAMES:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value:
                    out[name] = str(value)
        return out
    except OSError:
        return {}


def load_settings(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
) -> Settings:
    merged: MutableMapping[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    if env is None:
        merged.update(saved_user_keys())  # the process's own environment still wins
    merged.update(os.environ if env is None else env)

    tier_name = merged.get("MASSIVE_TIER", "free").strip().lower()
    if tier_name not in TIERS:
        raise ConfigError(f"Unknown MASSIVE_TIER {tier_name!r}; choose one of {', '.join(TIERS)}")
    tier = TIERS[tier_name]

    rpm = _int_setting(merged, "MASSIVE_RPM")
    if rpm is not None:
        tier = replace(tier, requests_per_minute=rpm or None)
    classes = []
    for asset, var in MASSIVE_CLASS_TIERS.items():
        name = (merged.get(var) or "free").strip().lower()
        if name not in TIERS:
            raise ConfigError(f"Unknown {var} {name!r}; choose one of {', '.join(TIERS)}")
        if name != tier.name:
            classes.append((asset, TIERS[name]))
    tier = replace(tier, by_asset=tuple(classes))

    per_run = _int_setting(merged, "MASSIVE_MAX_REQUESTS_PER_RUN")
    per_day = _int_setting(merged, "MASSIVE_MAX_REQUESTS_PER_DAY")
    refresh = _int_setting(merged, "MASSIVE_REFRESH_HOURS")

    return Settings(
        api_key=merged.get("MASSIVE_API_KEY") or None,
        tier=tier,
        db_path=Path(merged["MASSIVE_DB"]).expanduser() if merged.get("MASSIVE_DB") else DEFAULT_DB_PATH,
        base_url=(merged.get("MASSIVE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        max_requests_per_run=250 if per_run is None else per_run,
        max_requests_per_day=2000 if per_day is None else per_day,
        refresh_hours=float(refresh or 0),
        source_priority=tuple(
            s.strip().lower() for s in (merged.get("SOURCE_PRIORITY") or "massive,alpaca").split(",") if s.strip()
        ),
        alpaca=_alpaca(merged),
        audits_per_run=_default(_int_setting(merged, "AUDITS_PER_RUN"), 2),
        audits_random_per_run=_default(_int_setting(merged, "AUDITS_RANDOM_PER_RUN"), 1),
        tick_history_days=_default(_int_setting(merged, "TICK_HISTORY_DAYS"), 365),
        second_history_days=_default(_int_setting(merged, "SECOND_HISTORY_DAYS"), 365),
        tick_min_free_gb=_default(_int_setting(merged, "TICK_MIN_FREE_GB"), 250),
        tiingo_api_key=merged.get("TIINGO_API_KEY") or None,
        crypto_venue_days=_default(_int_setting(merged, "CRYPTO_VENUE_DAYS"), 0),
        live_coins=_live_coins(merged.get("LIVE_CRYPTO")),
        live_stocks=str(merged.get("LIVE_STOCKS", "on")).strip().lower() not in ("off", "0", "false", "no", "none"),
        offline=str(merged.get("MASSIVE_OFFLINE", "")).strip().lower() in ("1", "true", "yes", "on"),
    )


def _live_coins(value: str | None) -> tuple[str, ...]:
    """LIVE_CRYPTO: coins every live exchange stream lists (LIVE_COINS); another would be refused
    by Coinbase (taking its whole stream down) and entered as covered by Bitstamp."""
    from .live import LIVE_COINS

    if value is None or not value.strip():
        return LIVE_COINS
    if value.strip().lower() in ("off", "none", "0", "false"):
        return ()
    coins = tuple(c.strip().upper() for c in value.split(",") if c.strip())
    unknown = [c for c in coins if c not in LIVE_COINS]
    if unknown:
        raise ConfigError(f"LIVE_CRYPTO: {', '.join(unknown)} not streamed live; choose from {', '.join(LIVE_COINS)}")
    return coins


def _default(value: int | None, default: int) -> int:
    return default if value is None else value


def _int_setting(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e
    if value < 0:
        raise ConfigError(f"{name} must not be negative")
    return value
