"""Blue Ocean ATS (blueocean-tech.io) through Alpaca: US stocks overnight, 20:00-04:00 ET, Sunday to
Thursday nights; the fourth session (sessions.py). Its own market: the only venue trading then.
It uses Alpaca's keys and shares Alpaca's limits (the same account): one rate window and one daily
cap for both feeds, so together they stay within the free Basic plan. (Tiingo has it too, as a paid
add-on: tiingo.py's scan reports it.)"""
from __future__ import annotations

from ...config import ConfigError
from ..alpaca import TRADING_URLS, AlpacaClient, BoatsSource, PAGE_LIMIT
from ..registry import Provider, limiter, machine_caps
from .alpaca import PLUGIN as ALPACA, view as alpaca_view


def build(settings, request_budget):
    a = settings.alpaca
    if a is None:
        raise ConfigError("Alpaca settings missing")
    # Alpaca's window and day cap, held for the machine under Alpaca's name: one account's limits
    client = AlpacaClient(a.key_id, a.secret_key, trading_url=TRADING_URLS[a.paper],
                          rate_limiter=limiter("alpaca", settings, a.key_id), request_budget=request_budget)
    return BoatsSource(machine_caps(client, "alpaca", settings, a.key_id), realtime=a.tier.name == "alpaca-plus")


def scan(source) -> list[tuple[str, bool, str]]:
    """What the plan gives (one request): last week's overnight minute bars of a busy stock."""
    ok = source.is_entitled("minute", "AAPL", "stock")
    return [("stock minute bars overnight (feed=boats)", ok,
             "Alpaca Basic: history up to 15 minutes ago" if ok else "refused by the plan")]


PLUGIN = Provider(
    name="boats", title="Blue Ocean", build=build, assets=frozenset({"stock"}), order=15, account="alpaca",
    about="US stocks overnight, 20:00-04:00 ET, Blue Ocean ATS through Alpaca (your Alpaca keys and limits)",
    keys=ALPACA.keys, signup=ALPACA.signup, kind="exchange",
    provides={"stock": ("minute",)}, per_request={"minute": PAGE_LIMIT},
    limits="shares Alpaca's: 200 requests per minute (Basic) for both feeds",
    missing_keys_note="keys not set: Blue Ocean's overnight bars come through Alpaca, with ALPACA_API_KEY_ID "
                      "and ALPACA_API_SECRET_KEY",
    view=alpaca_view, rate=lambda settings: ALPACA.rate_window(settings),
    has_keys=lambda s: bool(s.alpaca and s.alpaca.has_keys),
    markets=lambda settings: {"stock": "boats"},
    scan=scan,
)
