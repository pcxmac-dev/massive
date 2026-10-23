"""Turn what the user types into a precise instrument spec.

    SPCX, SPCX::NASDAQ, NASDAQ:SPCX     stock (optionally pinned to an exchange)
    IXIC, SPX, RUT, DJI, NDX, I:SPX, ^GSPC   index
    EURUSD, USDEUR, EUR/USD, C:EURUSD, FX:EURUSD   forex pair
    BTCUSD, BTC/USD, BTC-USD, X:BTCUSD, COINBASE:BTCUSD   crypto pair (consolidated across exchanges)
    NQ1!, CL1!, NG1!, NQ2!              continuous futures (1 = most active, 2 = next)

The spec says which asset class it is, which ticker it is stored and shown as,
and exactly what to ask the data source for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import exchange_mic

ASSETS = ("stock", "index", "fx", "crypto", "future")

# Common index names -> Massive index code (without the "I:" prefix)
INDEX_ALIASES = {
    "IXIC": "COMP", "COMP": "COMP", "NASDAQ COMPOSITE": "COMP",
    "SPX": "SPX", "INX": "SPX", "GSPC": "SPX",
    "RUT": "RUT", "DJI": "DJI", "DJIA": "DJI", "NDX": "NDX", "VIX": "VIX",
}

CURRENCIES = (
    "EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK",
    "MXN", "ZAR", "TRY", "PLN", "HUF", "CZK", "INR", "KRW", "BRL", "XAU", "XAG",
)
# Market quoting convention: the earlier currency in this list is the base (EURUSD, not USDEUR).
_FX_RANK = {c: i for i, c in enumerate(("XAU", "XAG", "EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"))}

# Crypto: Massive's aggregates (X:BASEQUOTE) consolidate trades from every exchange it covers,
# so an exchange prefix (COINBASE:BTCUSD as on TradingView) is accepted and dropped.
CRYPTO = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "BCH", "ADA", "AVAX", "DOT", "LINK", "XLM", "UNI", "USDT", "USDC")
CRYPTO_QUOTES = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "USDT", "USDC", "BTC", "ETH")
CRYPTO_EXCHANGES = ("COINBASE", "KRAKEN", "BITSTAMP", "BINANCE", "BITFINEX", "GEMINI", "CRYPTO", "X")

_CONTINUOUS = re.compile(r"^([A-Z0-9]{1,4}?)([1-9])!$")


@dataclass(frozen=True)
class SymbolSpec:
    ticker: str  # how it is stored and shown: SPCX, IXIC, USDEUR, BTCUSD, NQ1!
    asset: str  # stock | index | fx | crypto | future
    source_ticker: str  # what the source is asked for: SPCX, I:COMP, C:EURUSD, X:BTCUSD, NQ (product)
    exchange: str | None = None  # expected primary exchange MIC (stocks)
    invert: bool = False  # fx: shown as the inverse of the source's pair
    rank: int = 1  # futures: 1 = most active contract, 2 = the next one


def _fx(base: str, quote: str, shown: str) -> SymbolSpec:
    rb, rq = _FX_RANK.get(base, 99), _FX_RANK.get(quote, 99)
    if rb <= rq:
        return SymbolSpec(shown, "fx", f"C:{base}{quote}")
    return SymbolSpec(shown, "fx", f"C:{quote}{base}", invert=True)


def _crypto(text: str) -> SymbolSpec | None:
    t = text
    if ":" in t and "::" not in t:
        prefix, rest = t.split(":", 1)
        if prefix not in CRYPTO_EXCHANGES:
            return None
        t = rest
    t = t.split("::")[0].replace("/", "").replace("-", "")
    for base in sorted(CRYPTO, key=len, reverse=True):
        quote = t[len(base):]
        if t.startswith(base) and quote in CRYPTO_QUOTES and quote != base:
            return SymbolSpec(t, "crypto", f"X:{t}")
    return None


def resolve(text: str) -> SymbolSpec:
    raw = text.strip()
    t = raw.upper()

    m = _CONTINUOUS.match(t.split("::")[0])
    if m:
        product, rank = m.group(1), int(m.group(2))
        return SymbolSpec(f"{product}{rank}!", "future", product, rank=rank)

    for prefix in ("I:", "^", "INDEX:", "TVC:"):
        if t.startswith(prefix):
            code = INDEX_ALIASES.get(t[len(prefix):], t[len(prefix):])
            name = next((k for k, v in INDEX_ALIASES.items() if v == code and len(k) <= 5), code)
            return SymbolSpec(name if prefix != "I:" else t[2:], "index", f"I:{code}")
    if t in INDEX_ALIASES:
        return SymbolSpec(t, "index", f"I:{INDEX_ALIASES[t]}")

    crypto = _crypto(t)
    if crypto:
        return crypto

    pair = t
    for prefix in ("C:", "FX:", "OANDA:", "FX_IDC:"):
        if pair.startswith(prefix):
            pair = pair[len(prefix):]
    pair = pair.replace("/", "").replace("-", "")
    if len(pair) == 6 and pair[:3] in CURRENCIES and pair[3:] in CURRENCIES and pair[:3] != pair[3:]:
        return _fx(pair[:3], pair[3:], pair)

    if "::" in raw:
        ticker, exch = raw.split("::", 1)
        return SymbolSpec(ticker.strip().upper(), "stock", ticker.strip().upper(), exchange_mic(exch))
    if ":" in raw:
        exch, ticker = raw.split(":", 1)
        return SymbolSpec(ticker.strip().upper(), "stock", ticker.strip().upper(), exchange_mic(exch))
    return SymbolSpec(t, "stock", t)
