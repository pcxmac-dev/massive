from __future__ import annotations

import numpy as np
import pandas as pd

from massive_collate import analysis
from massive_collate.models import ETF, Exchange, Stock, country, instrument_class


def _ticks() -> pd.DataFrame:
    t0 = pd.Timestamp("2026-10-20 09:30:00", tz="America/New_York")
    return pd.DataFrame({
        "time": [t0, t0 + pd.Timedelta("200ms"), t0 + pd.Timedelta("1.5s"), t0 + pd.Timedelta("3s")],
        "price": [10.0, 11.0, 9.0, 10.5],
        "size": [100, 300, 100, 100],
        "exchange": [4, 12, 4, 4],
        "exchange_name": ["Nasdaq", "NYSE Arca", "Nasdaq", "Nasdaq"],
    })


def test_trades_to_bars_ohlcv_and_vwap():
    bars = analysis.trades_to_bars(_ticks(), "1s")
    first = bars.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (10.0, 11.0, 10.0, 11.0)
    assert first["volume"] == 400 and first["trades"] == 2
    assert np.isclose(first["vwap"], (10 * 100 + 11 * 300) / 400)
    assert len(bars) == 3  # the empty 2s interval is dropped


def test_exchange_breakdown_shares_sum_to_one():
    b = analysis.exchange_breakdown(_ticks())
    assert np.isclose(b["volume_share"].sum(), 1.0)
    assert b.loc["NYSE Arca", "volume"] == 300


def test_indicators_on_trend():
    s = pd.Series(np.arange(1, 101, dtype=float))
    assert analysis.rsi(s).dropna().iloc[-1] == 100
    assert np.isclose(analysis.sma(s, 10).iloc[-1], np.mean(np.arange(91, 101)))
    bb = analysis.bollinger(s)
    assert (bb["upper"].dropna() >= bb["lower"].dropna()).all()
    assert analysis.max_drawdown(s) == 0


def test_summary_and_beta():
    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    rng = np.random.default_rng(0)
    bench = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)), idx)
    stock = bench * 1.0
    stats = analysis.summary(stock)
    assert stats["observations"] == 300
    assert np.isclose(analysis.beta(analysis.returns(stock), analysis.returns(bench)), 1.0)


def test_instrument_hierarchy():
    us = country("US")
    arca = Exchange("massive", 12, "NYSE Arca", us, mic="ARCX")
    etf = instrument_class("ETF")("SPCX", "SPAC and New Issue ETF", arca)
    assert isinstance(etf, ETF) and etf.country.code == "US"
    assert "etf" in etf.describe() and instrument_class("CS") is Stock
