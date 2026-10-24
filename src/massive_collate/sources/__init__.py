"""Data sources. Add a provider by subclassing DataSource and registering a builder here."""
from __future__ import annotations

from typing import Callable

from ..config import ConfigError, Settings
from ..coverage import FeedProfile
from ..symbols import ASSETS
from ..ratelimit import RateLimiter
from .base import RESOLUTIONS, DataSource
from .alpaca import TRADING_URLS, AlpacaClient, AlpacaSource
from .public import BitfinexSource, BitstampSource, CboeSource, CoinbaseSource, PublicClient, TiingoSource
from .massive import MassiveClient, MassiveSource


def rate_window(name: str, settings: Settings) -> tuple[int | None, float]:
    """The window each feed's limiter keeps: (requests, seconds). A little inside what each
    server publishes (config.PUBLISHED_LIMITS) so a limiter never meets a refusal."""
    if name == "tiingo":
        return 45, 3600.0  # free plan: 50 an hour
    if name == "coinbase":
        return 8, 1.0  # 10 a second per address (public endpoints): a minute's window let 300 go in a burst
    rpm = settings.for_source(name).tier.requests_per_minute
    # A few a minute (Massive's free classes: 5): a couple of seconds' margin, so jitter between
    # sending and arriving never puts six in the server's minute.
    return rpm, 62.0 if rpm and rpm <= 10 else 60.0


def _limiter(name: str, settings: Settings) -> RateLimiter:
    return RateLimiter(*rate_window(name, settings))


def _build_massive(settings: Settings, request_budget: int | None) -> DataSource:
    client = MassiveClient(
        settings.require_api_key(),
        base_url=settings.base_url,
        rate_limiter=_limiter("massive", settings),
        request_budget=request_budget,
    )
    # Asset classes on another plan keep that plan's rate (the free plan: 5 a minute per class).
    client.class_limiters = {a: RateLimiter(t.requests_per_minute, 60.0) for a, t in settings.tier.by_asset}
    return MassiveSource(client)


def _build_alpaca(settings: Settings, request_budget: int | None) -> DataSource:
    """Builds without keys too: crypto needs none, and stock requests are refused before sending."""
    a = settings.alpaca
    if a is None:
        raise ConfigError("Alpaca settings missing")
    client = AlpacaClient(a.key_id, a.secret_key, trading_url=TRADING_URLS[a.paper],
                          rate_limiter=_limiter("alpaca", settings), request_budget=request_budget)
    return AlpacaSource(client, feed=a.feed, crypto_loc=a.crypto_loc, realtime=a.tier.name == "alpaca-plus")


def _build_bitstamp(settings: Settings, request_budget: int | None) -> DataSource:
    return BitstampSource(PublicClient("https://www.bitstamp.net/api/v2", rate_limiter=_limiter("bitstamp", settings),
                                       request_budget=request_budget))


def _build_coinbase(settings: Settings, request_budget: int | None) -> DataSource:
    return CoinbaseSource(PublicClient("https://api.exchange.coinbase.com", rate_limiter=_limiter("coinbase", settings),
                                       request_budget=request_budget))


def _build_bitfinex(settings: Settings, request_budget: int | None) -> DataSource:
    return BitfinexSource(PublicClient("https://api-pub.bitfinex.com/v2", rate_limiter=_limiter("bitfinex", settings),
                                       request_budget=request_budget))


def _build_cboe(settings: Settings, request_budget: int | None) -> DataSource:
    return CboeSource(PublicClient("https://cdn-api.cboe.com/api/global/us_indices/daily_prices",
                                   rate_limiter=_limiter("cboe", settings), request_budget=request_budget))


def _build_tiingo(settings: Settings, request_budget: int | None) -> DataSource:
    key = settings.tiingo_api_key
    headers = {"Authorization": f"Token {key}"} if key else {}
    return TiingoSource(PublicClient("https://api.tiingo.com", headers=headers, request_budget=request_budget,
                                     rate_limiter=_limiter("tiingo", settings)), has_key=bool(key))


BUILDERS: dict[str, Callable[[Settings, int | None], DataSource]] = {
    "massive": _build_massive,
    "alpaca": _build_alpaca,
    "bitstamp": _build_bitstamp,
    "coinbase": _build_coinbase,
    "bitfinex": _build_bitfinex,
    "cboe": _build_cboe,
    "tiingo": _build_tiingo,
}
# Closed feeds (public archives): each symbol is filled once, then never asked again.
ARCHIVAL = frozenset({"cboe", "tiingo"})


# Asset classes each feed can collect at all (with keys / on some plan). Watching a symbol puts it
# on every feed that can; the coverage ledger then keeps them from downloading the same span.
SUPPORTED_ASSETS: dict[str, frozenset] = {"massive": frozenset(ASSETS), "alpaca": frozenset({"stock", "crypto"}),
                                          "bitstamp": frozenset({"crypto"}), "coinbase": frozenset({"crypto"}),
                                          "bitfinex": frozenset({"crypto"}), "cboe": frozenset({"index"}),
                                          "tiingo": frozenset({"stock"})}


def supports(feed: str, spec) -> bool:
    """Whether a feed can hold a symbol at all (its asset class, and for the archives, its list)."""
    from .public import BITSTAMP_PAIRS, CBOE_INDICES, CRYPTO_PAIRS

    if spec.asset not in SUPPORTED_ASSETS.get(feed, frozenset()):
        return False
    if feed == "bitstamp":
        return spec.ticker.upper() in BITSTAMP_PAIRS
    if feed in ("coinbase", "bitfinex"):
        return spec.ticker.upper() in CRYPTO_PAIRS
    if feed == "cboe":
        return spec.source_ticker.upper().removeprefix("I:") in CBOE_INDICES
    return True


def feed_profiles(settings: Settings) -> dict[str, FeedProfile]:
    """Every feed configured on this machine and what it can serve (see coverage.py)."""
    order = list(settings.source_priority) + sorted(set(BUILDERS) - set(settings.source_priority))
    out = {}
    for i, name in enumerate(n for n in order if n in BUILDERS):
        tier = settings.for_source(name).tier
        if name == "massive":
            if not settings.api_key:
                continue
            assets = frozenset(ASSETS)
        elif name == "alpaca":
            a = settings.alpaca
            assets = frozenset({"crypto"} | ({"stock"} if a and a.has_keys else set()))
        elif name == "tiingo":
            assets = frozenset({"stock"}) if settings.tiingo_api_key else frozenset()
        elif name in ("bitstamp", "coinbase", "bitfinex", "cboe"):
            assets = SUPPORTED_ASSETS[name]
        else:
            assets = frozenset(ASSETS)
        out[name] = FeedProfile(name, tier, assets, i, tick_days=settings.tick_history_days,
                                venues=markets(name, settings))
    return out


def markets(feed: str, settings: Settings) -> dict[str, str]:
    """Which market each asset class of a feed is: feeds of the same market are copies (one
    download per span between them); different markets are combined by the composite model."""
    if feed == "massive":
        return {"stock": "sip", "index": "index", "fx": "otc", "crypto": "consolidated", "future": "cme"}
    if feed == "alpaca":
        a = settings.alpaca
        loc = a.crypto_loc if a else "us-1"
        return {"stock": a.feed if a else "sip", "crypto": "alpaca" if loc == "us" else "kraken"}
    if feed == "tiingo":
        return {"stock": "sip"}
    if feed == "cboe":
        return {"index": "index"}
    return {"crypto": feed}  # bitstamp, coinbase, bitfinex: each its own exchange



def build_source(name: str, settings: Settings, request_budget: int | None = None) -> DataSource:
    try:
        return BUILDERS[name](settings, request_budget)
    except KeyError:
        raise ValueError(f"unknown source {name!r}; available: {', '.join(BUILDERS)}") from None


__all__ = ["RESOLUTIONS", "DataSource", "MassiveClient", "MassiveSource", "AlpacaClient", "AlpacaSource",
           "build_source", "feed_profiles", "supports", "BUILDERS", "SUPPORTED_ASSETS", "ARCHIVAL"]
