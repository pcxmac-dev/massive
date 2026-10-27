"""Alpaca (alpaca.markets): US stocks with keys (SIP from 2016, IEX live), crypto without."""
from __future__ import annotations

from dataclasses import replace

from ...config import ConfigError
from ..alpaca import TRADING_URLS, AlpacaClient, AlpacaSource
from ..registry import KeyVar, Provider, limiter, machine_caps


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


PLUGIN = Provider(
    name="alpaca", title="Alpaca", build=build, assets=frozenset({"stock", "crypto"}), order=10,
    about="US stocks (SIP from 2016, the session up to 15 min ago, IEX live) with keys; crypto without",
    keys=(KeyVar("ALPACA_API_KEY_ID", "key ID", ("APCA_API_KEY_ID",)),
          KeyVar("ALPACA_API_SECRET_KEY", "secret key", ("APCA_API_SECRET_KEY",))),
    keyless=frozenset({"crypto"}), signup="alpaca.markets: a paper account's API keys",
    plan_vars=("ALPACA_PLAN",), limits="Basic (free): 200 requests per minute; Algo Trader Plus: 10,000",
    kind="exchange", kind_by_asset={"stock": "consolidated"},  # its stocks are the consolidated tape (SIP)
    venue_notes={"crypto": "one exchange's book (Kraken)"}, bars_per_request=10_000, bulk_resolution="minute",
    missing_keys_note="keys not set: US stocks (SIP from 2016, IEX live), options and news wait for "
                      "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY; crypto needs no key and is collected",
    missing_keys_chip="stocks need keys",
    view=view, has_keys=lambda s: bool(s.alpaca and s.alpaca.has_keys), markets=markets,
)
