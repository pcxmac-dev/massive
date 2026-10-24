"""The rail's watchlists: saved in the database, validated, tickers kept in order without repeats."""
from __future__ import annotations

import pytest

from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "m.db", check_same_thread=False)
    yield ChartApp(store)
    store.close()


def test_lists_round_trip_through_the_database(app):
    assert app.watchlists() == {"lists": []}
    out = app.save_watchlists({"lists": [{"name": " Tech ", "tickers": ["aapl", "MSFT", "AAPL"]},
                                         {"name": "Coins", "tickers": ["BTCUSD"]}]})
    assert out == {"lists": [{"name": "Tech", "tickers": ["AAPL", "MSFT"]}, {"name": "Coins", "tickers": ["BTCUSD"]}]}
    assert ChartApp(app.store).watchlists() == out  # kept, not in memory


@pytest.mark.parametrize("body", [
    {"lists": "Tech"},
    {"lists": [{"name": "", "tickers": []}]},
    {"lists": [{"name": "x" * 41, "tickers": []}]},
    {"lists": [{"name": "A", "tickers": []}, {"name": "A", "tickers": []}]},
    {"lists": [{"name": "A", "tickers": [1]}]},
    {"lists": [{"name": "A", "tickers": "AAPL"}]},
])
def test_bad_lists_are_refused(app, body):
    with pytest.raises(ValueError):
        app.save_watchlists(body)
