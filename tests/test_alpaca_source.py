"""Alpaca source: request shapes, the 15-minute block, keys never sent where they should not be."""
from datetime import date, datetime, timedelta, timezone

import pytest

from massive_collate.config import load_settings
from massive_collate.errors import NotEntitled
from massive_collate.sources import build_source
from massive_collate.sources.alpaca import AlpacaClient, AlpacaSource, normalize_trade, _ns

from .conftest import FakeResponse, FakeSession

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)  # 12:00 ET


def _source(responses, keys=True, **kw):
    session = FakeSession(responses)
    client = AlpacaClient("KID", "SECRET", session=session, sleep=lambda s: None) if keys else \
        AlpacaClient(None, None, session=session, sleep=lambda s: None)
    return AlpacaSource(client, now=lambda: NOW, **kw), session


def test_stocks_without_keys_send_nothing():
    src, session = _source([], keys=False)
    with pytest.raises(NotEntitled):
        next(iter(src.bar_pages("AAPL", date(2026, 10, 20), date(2026, 10, 21), "minute")))
    assert session.calls == [] and not src.serves("stock") and src.serves("crypto")
    assert src.is_entitled("minute", "AAPL") is False and session.calls == []


def test_crypto_needs_no_key_and_asks_kraken_by_default():
    src, session = _source([FakeResponse(200, {"bars": {"BTC/USD": [
        {"t": "2026-10-20T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3, "n": 4, "vw": 1.2}]},
        "next_page_token": None})], keys=False)
    pages = list(src.bar_pages("X:BTCUSD", date(2026, 10, 20), date(2026, 10, 20), "day", asset="crypto"))
    assert pages == [[{"t": 1792454400000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3, "vw": 1.2, "n": 4}]]
    url, params, headers = session.calls[0]
    assert url == "https://data.alpaca.markets/v1beta3/crypto/us-1/bars"
    assert params["symbols"] == "BTC/USD" and params["timeframe"] == "1Day" and not headers
    assert params["start"] == "2026-10-20T00:00:00.000000Z" and params["end"] == "2026-10-21T00:00:00.000000Z"


def test_stock_window_stops_before_the_latest_15_minutes_and_pages_follow_tokens():
    src, session = _source([
        FakeResponse(200, {"bars": {"AAPL": [{"t": "2026-10-21T13:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1,
                                              "n": 1, "vw": 1}]}, "next_page_token": "TOKEN"}),
        FakeResponse(200, {"bars": {}, "next_page_token": None}),
    ])
    pages = list(src.bar_pages("AAPL", date(2026, 10, 21), date(2026, 10, 21), "minute", descending=True))
    assert len(pages) == 2 and len(session.calls) == 2
    (_, p1, h1), (_, p2, _) = session.calls
    assert h1 == {"APCA-API-KEY-ID": "KID", "APCA-API-SECRET-KEY": "SECRET"}
    assert "KID" not in str(p1) and "SECRET" not in str(p1)  # keys only ever in headers
    assert p1["end"] == "2026-10-21T15:44:00.000000Z"  # now - 16 min (plan blocks the latest 15)
    assert p1["feed"] == "sip" and p1["adjustment"] == "split" and p1["sort"] == "desc" and p1["limit"] == 10000
    assert p2["page_token"] == "TOKEN"


def test_window_entirely_inside_the_block_sends_nothing():
    src, session = _source([])
    assert list(src.bar_pages("AAPL", NOW - timedelta(minutes=5), NOW, "minute")) == []
    assert session.calls == []


def test_realtime_plan_has_no_block():
    src, session = _source([FakeResponse(200, {"bars": {}, "next_page_token": None})], realtime=True)
    list(src.bar_pages("AAPL", NOW - timedelta(minutes=5), NOW, "minute"))
    assert session.calls[0][1]["end"] == "2026-10-21T16:00:00.000000Z"


def test_trades_keep_nanoseconds_and_drop_withdrawn_ones():
    assert _ns("2022-01-03T09:00:00.086175744Z") == 1641200400086175744
    t = normalize_trade({"t": "2022-01-03T09:00:00.086175744Z", "x": "V", "p": 178.2, "s": 100, "c": ["@", "I"],
                         "i": 52983525034, "z": "C"})
    assert t["sip_timestamp"] == 1641200400086175744 and t["exchange"] == ord("V") and t["tape"] == 3
    other = normalize_trade({"t": "2022-01-03T09:00:00.086175744Z", "x": "Q", "p": 1, "s": 1, "i": 52983525034})
    assert other["sequence_number"] != t["sequence_number"]  # same id on another exchange stays distinct
    assert normalize_trade({"t": "2022-01-03T09:00:00Z", "x": "V", "p": 1, "s": 1, "i": 1, "u": "canceled"}) is None


def test_reference_uses_the_trading_api_host_and_crypto_needs_none():
    src, session = _source([FakeResponse(200, {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ",
                                               "status": "active"})])
    inst = src.instrument("aapl")
    assert inst.name == "Apple Inc." and inst.primary_exchange.mic == "XNAS" and inst.active
    assert session.calls[0][0] == "https://paper-api.alpaca.markets/v2/assets/AAPL"
    pair = src.instrument("X:BTCUSD")
    assert pair.name == "BTC/USD on Kraken (US)" and len(session.calls) == 1


def test_settings_and_builder(monkeypatch):
    s = load_settings(env={"ALPACA_API_KEY_ID": "abc", "ALPACA_API_SECRET_KEY": "xyz", "ALPACA_PAPER": "0"},
                      env_file=None)
    assert "abc" not in repr(s.alpaca) and "xyz" not in repr(s.alpaca)
    feed = s.for_source("alpaca")
    assert feed.tier.name == "alpaca-basic" and feed.max_requests_per_day == 30000 and s.tier.name == "free"
    src = build_source("alpaca", s, request_budget=5)
    assert src.client.trading_url == "https://api.alpaca.markets" and src.client.request_budget == 5
    assert src.feed == "sip" and src.crypto_loc == "us-1" and not src.realtime
    with pytest.raises(Exception):
        load_settings(env={"ALPACA_FEED": "nasdaq"}, env_file=None)
