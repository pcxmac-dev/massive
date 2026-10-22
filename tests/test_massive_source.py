from __future__ import annotations

from datetime import date

from massive_collate.models import ETF
from massive_collate.sources.massive import MassiveClient, MassiveSource

from .conftest import FakeResponse, FakeSession


def _source(responses):
    session = FakeSession(responses)
    return MassiveSource(MassiveClient("KEY", session=session, sleep=lambda s: None)), session


def test_probe_costs_exactly_one_request():
    src, session = _source([FakeResponse(200, {"results": [{}], "next_url": "https://api.massive.com/n"})])
    assert src.is_entitled("trades", "SPCX") is True
    assert src.requests_made == 1
    url, params, _ = session.calls[0]
    assert url.endswith("/v3/trades/SPCX") and params["limit"] == 1


def test_probe_refused_means_not_entitled():
    src, _ = _source([FakeResponse(403, {"message": "NOT_AUTHORIZED"})])
    assert src.is_entitled("second", "SPCX") is False


def test_bar_request_shape():
    src, session = _source([FakeResponse(200, {"results": [{"t": 1}]})])
    pages = list(src.bar_pages("spcx", date(2025, 1, 1), date(2025, 2, 1), "second", descending=True))
    assert pages == [[{"t": 1}]]
    url, params, _ = session.calls[0]
    assert url == "https://api.massive.com/v2/aggs/ticker/SPCX/range/1/second/2025-01-01/2025-02-01"
    assert params == {"adjusted": "true", "sort": "desc", "limit": 50000}


def test_trade_request_window():
    src, session = _source([FakeResponse(200, {"results": []})])
    list(src.trade_pages("SPCX", gte_ns=10, lt_ns=20, descending=True))
    _, params, _ = session.calls[0]
    assert params["timestamp.gte"] == 10 and params["timestamp.lt"] == 20 and params["order"] == "desc"


EXCHANGES = {"results": [
    {"id": 4, "name": "Nasdaq", "mic": "XNAS", "locale": "us", "type": "exchange"},
    {"id": 12, "name": "NYSE Arca", "mic": "ARCX", "locale": "us", "type": "exchange"},
]}


def test_exchanges_carry_country():
    src, _ = _source([FakeResponse(200, EXCHANGES)])
    ex = src.exchanges()
    assert [e.name for e in ex] == ["Nasdaq", "NYSE Arca"]
    assert ex[0].country.code == "US" and ex[0].timezone == "America/New_York"


def test_instrument_resolves_kind_and_exchange():
    details = {"results": {"ticker": "SPCX", "name": "SPAC ETF", "type": "ETF", "primary_exchange": "ARCX",
                           "locale": "us", "currency_name": "usd"}}
    src, _ = _source([FakeResponse(200, details), FakeResponse(200, EXCHANGES)])
    inst = src.instrument("spcx")
    assert isinstance(inst, ETF)
    assert inst.primary_exchange.name == "NYSE Arca" and inst.country.code == "US" and inst.currency == "USD"
