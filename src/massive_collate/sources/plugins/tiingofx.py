"""Tiingo's forex (tiingo.com): currency pairs' 1-minute bars from 2020, with the Tiingo key. A feed
of its own (its database file, its watch rows), kept up to date, on Tiingo's limits: one account, so
one rate window and one daily cap for both feeds, as Blue Ocean shares Alpaca's (boats.py). Its data
is Tiingo's own (its own market here), not a copy of Massive's currencies. Taken to spend the paid
plan's allowance, which the stocks' daily bars leave almost all unused (the user, 2026-10-28)."""
from __future__ import annotations

from dataclasses import replace

from ...config import Tier
from ..public import PublicClient, TiingoFxSource
from ..registry import Provider, limiter, machine_caps
from .tiingo import PLUGIN as TIINGO, monthly_symbols, view as tiingo_view

# Its currencies' history: from Tiingo's first forex minute (its plan's other limits are Tiingo's)
FX_TIER = Tier("tiingofx", requests_per_minute=None, history_years=None, history_start=TiingoFxSource.START)


def view(base):
    """Tiingo's plan (its caps and window, TIINGO_PLAN), with the currencies' history from 2020."""
    out = tiingo_view(base)
    return replace(out, tier=replace(out.tier, by_asset=(("fx", FX_TIER),)))


def build(settings, request_budget):
    key = settings.tiingo_api_key
    headers = {"Authorization": f"Token {key}"} if key else {}
    # Tiingo's window and day cap, held for the machine under Tiingo's name: one account's limits
    client = PublicClient("https://api.tiingo.com", headers=headers, request_budget=request_budget,
                          rate_limiter=limiter("tiingo", settings, key))
    # its pairs take places among the plan's different symbols a month, with Tiingo's stocks
    return monthly_symbols(TiingoFxSource(machine_caps(client, "tiingo", settings, key), has_key=bool(key)), settings, key)


PLUGIN = Provider(
    version="1.0.0", api="1.0",  # its own release, and the plugin contract it was written for
    name="tiingofx", title="Tiingo forex", build=build, assets=frozenset({"fx"}), order=61, account="tiingo",
    about="currency pairs' 1-minute bars from 2020 (your Tiingo key and limits)",
    keys=TIINGO.keys, signup=TIINGO.signup, kind="vendor",
    provides={"fx": ("minute",)}, per_request={"minute": TiingoFxSource.WINDOW_DAYS * 1440},
    limits="shares Tiingo's: its plan's requests an hour and a day, for both feeds",
    missing_keys_note="key not set: Tiingo's forex comes with TIINGO_API_KEY",
    view=view, rate=TIINGO.rate, has_keys=lambda s: bool(s.tiingo_api_key),
    markets=lambda settings: {"fx": "tiingofx"},
)
