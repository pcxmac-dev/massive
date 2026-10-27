"""Tiingo (tiingo.com): US stock daily bars from 1962 with a free account key; a closed archive.

Its scan (`capabilities`, one request each) asks what else the account's plan gives: IEX's minute
bars (one exchange), forex and crypto bars, and Blue Ocean's overnight bars (a paid add-on,
2026-10-27: $9 a month; Alpaca's free plan has them: boats.py). Only the daily bars are collected."""
from __future__ import annotations

from datetime import date, timedelta

from ...config import PUBLIC_FEEDS
from ...errors import SourceError
from ..public import PublicClient, TiingoSource
from ..registry import KeyVar, Provider, limiter, machine_caps

TIER, PER_RUN, PER_DAY = PUBLIC_FEEDS["tiingo"]
BARS = "open,high,low,close,volume"


def build(settings, request_budget):
    key = settings.tiingo_api_key
    headers = {"Authorization": f"Token {key}"} if key else {}
    client = PublicClient("https://api.tiingo.com", headers=headers, request_budget=request_budget,
                          rate_limiter=limiter("tiingo", settings, key))
    return TiingoSource(machine_caps(client, "tiingo", settings, key), has_key=bool(key))


def probes(today: date) -> list[tuple[str, str, dict]]:
    """(what, path, params) of the scan: a recent weekday of a busy symbol at each endpoint."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    d = day.isoformat()
    return [
        ("stock daily bars, from 1962", "/tiingo/daily/AAPL/prices", {"startDate": d, "endDate": d}),
        ("stock 1-minute bars from IEX (one exchange)", "/iex/AAPL/prices",
         {"startDate": d, "endDate": d, "resampleFreq": "1min", "afterHours": "true", "columns": BARS}),
        ("stock overnight bars from Blue Ocean (20:00-04:00)", "/boats/AAPL/prices",
         {"startDate": d, "endDate": d, "resampleFreq": "1min", "columns": BARS}),
        ("forex 1-minute bars", "/tiingo/fx/prices", {"tickers": "eurusd", "startDate": d, "endDate": d,
                                                      "resampleFreq": "1min"}),
        ("crypto bars", "/tiingo/crypto/prices", {"tickers": "btcusd", "startDate": d, "endDate": d,
                                                  "resampleFreq": "1min"}),
    ]


def rows_of(answer) -> int:
    """Bars in an answer: a list of bars, or crypto's [{priceData: [...]}]."""
    if isinstance(answer, list) and answer and isinstance(answer[0], dict) and "priceData" in answer[0]:
        return sum(len(a.get("priceData") or []) for a in answer)
    return len(answer) if isinstance(answer, list) else 0


def scan(source, today: date | None = None) -> list[tuple[str, bool, str]]:
    out = []
    for what, path, params in probes(today or date.today()):
        try:
            n = rows_of(source.client.get(path, params))
            out.append((what, True, f"{n:,} bars for {params['startDate']}"))
        except SourceError as e:  # NotEntitled included: a refusal names the add-on it needs
            out.append((what, False, str(e)[:200]))
    return out


PLUGIN = Provider(
    name="tiingo", title="Tiingo", build=build, assets=frozenset({"stock"}), order=60,
    about="US stock daily bars from 1962 (a free account key)",
    keys=(KeyVar("TIINGO_API_KEY"),), signup="tiingo.com: a free account's API token",
    archival=True, tier=TIER, per_run=PER_RUN, per_day=PER_DAY,
    limits="free plan: 50 requests per hour, 1,000 per day",
    symbols_per_month=450,  # the free plan's 500 different symbols a month, a little inside
    provides={"stock": ("day",)}, per_request={"day": None},  # one request: a symbol's whole history
    has_keys=lambda s: bool(s.tiingo_api_key),
    rate=lambda settings: (45, 3600.0),  # free plan: 50 an hour
    markets=lambda settings: {"stock": "sip"},
    scan=scan,
)
