"""Maths on stored data: tick aggregation, per-exchange stats, returns and indicators.

Functions take and return pandas objects so they compose: load ticks, build
bars at any resolution, then apply indicators or risk statistics.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# -- ticks -> bars -------------------------------------------------------------

def trades_to_bars(trades: pd.DataFrame, rule: str = "1s", by_exchange: bool = False) -> pd.DataFrame:
    """Aggregate ticks (columns time, price, size[, exchange_name]) into OHLCV bars.

    `rule` is any pandas frequency: "1s", "5s", "1min", "1h", "1D".
    Empty intervals are dropped rather than forward-filled.
    """
    if trades.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap", "trades"])
    df = trades.set_index("time")
    df = df.assign(notional=df["price"] * df["size"])
    keys = [pd.Grouper(freq=rule)]
    if by_exchange:
        keys.insert(0, "exchange_name")
    g = df.groupby(keys)
    bars = pd.DataFrame({
        "open": g["price"].first(),
        "high": g["price"].max(),
        "low": g["price"].min(),
        "close": g["price"].last(),
        "volume": g["size"].sum(),
        "trades": g["price"].count(),
        "notional": g["notional"].sum(),
    })
    bars = bars[bars["trades"] > 0]
    bars["vwap"] = bars.pop("notional") / bars["volume"]
    return bars


def exchange_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    """Volume, trade count, market share and VWAP for each exchange."""
    if trades.empty:
        return pd.DataFrame(columns=["trades", "volume", "volume_share", "vwap"])
    df = trades.assign(
        notional=trades["price"] * trades["size"],
        exchange_name=trades["exchange_name"].fillna(trades["exchange"].astype("string")),
    )
    g = df.groupby("exchange_name")
    out = pd.DataFrame({"trades": g.size(), "volume": g["size"].sum(), "notional": g["notional"].sum()})
    out["volume_share"] = out["volume"] / out["volume"].sum()
    out["vwap"] = out.pop("notional") / out["volume"]
    return out.sort_values("volume", ascending=False)


# -- returns -------------------------------------------------------------------

def returns(prices: pd.Series | pd.DataFrame, log: bool = False):
    return np.log(prices / prices.shift(1)).dropna(how="all") if log else prices.pct_change().dropna(how="all")


def cumulative_returns(prices: pd.Series | pd.DataFrame):
    return prices / prices.iloc[0] - 1


def drawdown(prices: pd.Series | pd.DataFrame):
    return prices / prices.cummax() - 1


def max_drawdown(prices: pd.Series | pd.DataFrame):
    return drawdown(prices).min()


def volatility(rets: pd.Series | pd.DataFrame, periods_per_year: float = TRADING_DAYS):
    """Annualised standard deviation of returns."""
    return rets.std() * np.sqrt(periods_per_year)


def realized_volatility(prices: pd.Series, freq: str = "1D") -> pd.Series:
    """Per-period realised volatility from intraday log returns (sqrt of summed squares)."""
    r = np.log(prices / prices.shift(1)).dropna()
    return np.sqrt((r**2).groupby(pd.Grouper(freq=freq)).sum())


def sharpe(rets: pd.Series | pd.DataFrame, risk_free: float = 0.0, periods_per_year: float = TRADING_DAYS):
    excess = rets - risk_free / periods_per_year
    return excess.mean() / excess.std() * np.sqrt(periods_per_year)


def beta(rets: pd.Series, benchmark: pd.Series) -> float:
    aligned = pd.concat([rets, benchmark], axis=1).dropna()
    cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
    return float(cov[0, 1] / cov[1, 1])


def correlation(rets: pd.DataFrame) -> pd.DataFrame:
    return rets.corr()


# -- indicators ----------------------------------------------------------------

def sma(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window).mean()


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(s: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's relative strength index (0-100)."""
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def bollinger(s: pd.Series, window: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, window)
    sd = s.rolling(window).std()
    return pd.DataFrame({"middle": mid, "upper": mid + k * sd, "lower": mid - k * sd})


def vwap(bars: pd.DataFrame, reset: str | None = "1D") -> pd.Series:
    """Cumulative VWAP from bars (uses vwap*volume if present, else close), reset each day."""
    px = bars["vwap"].fillna(bars["close"]) if "vwap" in bars else bars["close"]
    notional = px * bars["volume"]
    if reset is None:
        return notional.cumsum() / bars["volume"].cumsum()
    grp = pd.Grouper(freq=reset)
    return notional.groupby(grp).cumsum() / bars["volume"].groupby(grp).cumsum()


# -- summary -------------------------------------------------------------------

def summary(prices: pd.Series, periods_per_year: float = TRADING_DAYS) -> pd.Series:
    """Headline statistics for one price series."""
    prices = prices.dropna()
    r = returns(prices)
    years = len(r) / periods_per_year if len(r) else np.nan
    total = prices.iloc[-1] / prices.iloc[0] - 1 if len(prices) > 1 else np.nan
    return pd.Series({
        "start": prices.index[0] if len(prices) else None,
        "end": prices.index[-1] if len(prices) else None,
        "observations": len(prices),
        "first": prices.iloc[0] if len(prices) else np.nan,
        "last": prices.iloc[-1] if len(prices) else np.nan,
        "total_return": total,
        "cagr": (1 + total) ** (1 / years) - 1 if years and years > 0 and total > -1 else np.nan,
        "volatility": volatility(r, periods_per_year),
        "sharpe": sharpe(r, periods_per_year=periods_per_year),
        "max_drawdown": max_drawdown(prices),
    })
