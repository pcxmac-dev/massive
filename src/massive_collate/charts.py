"""Interactive charts (Plotly), written as standalone HTML files."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

from . import analysis


def _plotly():
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    return go, make_subplots


def candlestick(bars: pd.DataFrame, title: str, sma_windows: Sequence[int] = (), show_vwap: bool = False):
    """Candles + volume. `bars` is indexed by time with open/high/low/close/volume."""
    go, make_subplots = _plotly()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
    fig.add_trace(
        go.Candlestick(
            x=bars.index, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"], name="price"
        ),
        row=1, col=1,
    )
    for w in sma_windows:
        fig.add_trace(go.Scatter(x=bars.index, y=analysis.sma(bars["close"], w), name=f"SMA {w}"), row=1, col=1)
    if show_vwap:
        fig.add_trace(go.Scatter(x=bars.index, y=analysis.vwap(bars), name="VWAP"), row=1, col=1)
    fig.add_trace(go.Bar(x=bars.index, y=bars["volume"], name="volume"), row=2, col=1)
    fig.update_layout(title=title, xaxis_rangeslider_visible=False, hovermode="x unified")
    return fig


def tick_scatter(trades: pd.DataFrame, title: str):
    """Every trade as a point, coloured by exchange."""
    go, _ = _plotly()
    fig = go.Figure()
    for name, grp in trades.groupby(trades["exchange_name"].fillna("unknown")):
        fig.add_trace(go.Scattergl(
            x=grp["time"], y=grp["price"], mode="markers", name=str(name),
            marker={"size": 4}, customdata=grp["size"], hovertemplate="%{y} x %{customdata}<extra>%{fullData.name}</extra>",
        ))
    fig.update_layout(title=title, yaxis_title="price")
    return fig


def exchange_share(breakdown: pd.DataFrame, title: str):
    go, _ = _plotly()
    fig = go.Figure(go.Bar(x=breakdown.index.astype(str), y=breakdown["volume_share"] * 100))
    fig.update_layout(title=title, yaxis_title="% of volume")
    return fig


def compare(prices: pd.DataFrame, title: str):
    """Cumulative return of several series on one chart."""
    go, _ = _plotly()
    cum = analysis.cumulative_returns(prices) * 100
    fig = go.Figure([go.Scatter(x=cum.index, y=cum[c], name=str(c)) for c in cum.columns])
    fig.update_layout(title=title, yaxis_title="% return", hovermode="x unified")
    return fig


def save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs="cdn")
    return path
