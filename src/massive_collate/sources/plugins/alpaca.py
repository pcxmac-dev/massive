"""Alpaca (alpaca.markets): US stocks with keys (SIP from 2016, IEX live), crypto without.

Its scan finds the plan: Algo Trader Plus serves the consolidated tape's latest 15 minutes, Basic
refuses them; it sets ALPACA_PLAN where you have not."""
from __future__ import annotations

from dataclasses import replace

from ...config import ALPACA_TIERS, ConfigError
from ..alpaca import TRADING_URLS, AlpacaClient, AlpacaSource
from ..registry import KeyVar, Provider, Scanned, limiter, machine_caps


def build(settings, request_budget):
    """Builds without keys too: crypto needs none, and stock requests are refused before sending."""
    a = settings.alpaca
    if a is None:
        raise ConfigError("Alpaca settings missing")
    client = AlpacaClient(a.key_id, a.secret_key, trading_url=TRADING_URLS[a.paper],
                          rate_limiter=limiter("alpaca", settings, a.key_id), request_budget=request_budget)
    return AlpacaSource(machine_caps(client, "alpaca", settings, a.key_id), feed=a.feed, crypto_loc=a.crypto_loc,
                        realtime=a.tier.name == "alpaca-plus")


def view(base):
    a = base.alpaca
    if a is None:
        return base
    return replace(base, tier=a.tier, max_requests_per_run=a.max_requests_per_run,
                   max_requests_per_day=a.max_requests_per_day, origin=base)


def markets(settings):
    a = settings.alpaca
    loc = a.crypto_loc if a else "us-1"
    return {"stock": a.feed if a else "sip", "crypto": "alpaca" if loc == "us" else "kraken"}


def scan(source) -> list[tuple[str, bool, str]] | Scanned:
    """What the plan gives (one request each): the consolidated tape's minute bars, crypto's, and
    with keys its latest 15 minutes, which tell the plans apart."""
    stocks = source.is_entitled("minute", "AAPL", "stock")
    crypto = source.is_entitled("minute", "BTCUSD", "crypto")
    rows = [("stock minute bars, every exchange (SIP), from 2016", stocks,
             "" if stocks else "refused (keys or plan)"),
            ("crypto minute bars, no key", crypto, "")]
    if not stocks:
        return rows  # no keys, or refused: nothing tells the plan
    recent = source.recent_allowed("AAPL")
    plan = "algo_trader_plus" if recent else "basic"
    rows.append(("stock bars of the latest 15 minutes (Algo Trader Plus)", recent,
                 f"plan: {'Algo Trader Plus' if recent else 'Basic (free)'}"))
    return Scanned(tuple(rows), {"ALPACA_PLAN": plan})


PLUGIN = Provider(
    version="1.0.0", api="1.0",  # its own release, and the plugin contract it was written for
    name="alpaca", title="Alpaca", build=build, assets=frozenset({"stock", "crypto"}), order=10,
    about="US stocks (SIP from 2016, the session up to 15 min ago, IEX live) with keys; crypto without",
    keys=(KeyVar("ALPACA_API_KEY_ID", "key ID", ("APCA_API_KEY_ID",)),
          KeyVar("ALPACA_API_SECRET_KEY", "secret key", ("APCA_API_SECRET_KEY",))),
    keyless=frozenset({"crypto"}), signup="alpaca.markets: a paper account's API keys",
    plan_vars=("ALPACA_PLAN",), limits="Basic (free): 200 requests per minute; Algo Trader Plus: 10,000",
    plan_choices={"ALPACA_PLAN": tuple(ALPACA_TIERS)},
    kind="exchange", kind_by_asset={"stock": "consolidated"},  # its stocks are the consolidated tape (SIP)
    venue_notes={"crypto": "one exchange's book (Kraken)"}, bulk_resolution="minute",
    provides={"stock": ("day", "minute"), "crypto": ("day", "minute")},  # no 1-second bars
    per_request={"day": 10_000, "minute": 10_000},  # PAGE_LIMIT
    streams={"stock": "real time, IEX's trades only (a few per cent of US volume), symbols open on a chart"},
    missing_keys_note="keys not set: US stocks (SIP from 2016, IEX live), options and news wait for "
                      "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY; crypto needs no key and is collected",
    missing_keys_chip="stocks need keys",
    view=view, has_keys=lambda s: bool(s.alpaca and s.alpaca.has_keys), markets=markets, scan=scan,
)
