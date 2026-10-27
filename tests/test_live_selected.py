"""Nothing is streamed unless it is selected (LIVE_STREAMS=selected, the default; the user,
2026-10-27): every live stream follows the symbols open on a chart, and with none of its symbols
open it is not connected."""
from __future__ import annotations

import json

import pytest

from massive_collate import live
from massive_collate.config import ConfigError, load_settings
from massive_collate.storage import Store

T0 = 1_792_764_000_000  # 2026-10-23 14:00 UTC, ms


class WS:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(json.loads(m))


def test_selected_is_the_default_and_always_keeps_the_old_way():
    assert load_settings(env={}, env_file=None).live_selected_only is True
    assert load_settings(env={"LIVE_STREAMS": "always"}, env_file=None).live_selected_only is False
    with pytest.raises(ConfigError, match="LIVE_STREAMS"):
        load_settings(env={"LIVE_STREAMS": "sometimes"}, env_file=None)


def test_the_coins_streamed_are_the_ones_open_on_a_chart(tmp_path):
    with Store(tmp_path / "m.db") as store:
        assert live.wanted_coins(store, T0, ("BTC", "ETH")) == ()  # nothing open: nothing streamed
        store.kv_set(live.OPEN_KEY, json.dumps({"ETHUSD": T0 - 1_000, "AAPL": T0 - 500, "DOGEUSD": T0 - 10,
                                                "BTCUSD": T0 - live.OPEN_FRESH_MS - 1}))
        assert live.wanted_coins(store, T0, ("BTC", "ETH")) == ("ETHUSD",)  # DOGE not streamed; BTC closed a while ago
        store.kv_set(live.WATCHLISTS_KEY, json.dumps([{"name": "mine", "tickers": ["BTCUSD", "MSFT"]}]))
        store.watch("massive", ["AAPL", "MSFT"], "minute")
        assert live.wanted_stocks(store, T0, watchlists=False) == ("AAPL",)  # a watchlist alone is not selected
        assert live.wanted_stocks(store, T0) == ("AAPL", "MSFT")  # LIVE_STREAMS=always: the watchlists too
        assert live.wanted_coins(store, T0, ("BTC", "ETH")) == ("ETHUSD",)


def test_a_coin_stream_follows_the_charts_and_closes_with_none_open(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC", "ETH"), sources=("coinbase", "bitstamp"),
                        log=lambda *_: None, connect=lambda url: None, selected_only=True)
    coinbase, bitstamp = rec.streams
    assert coinbase.dynamic and bitstamp.dynamic
    assert coinbase.subscribe(()) == [] and bitstamp.subscribe(()) == []  # connected with nothing yet

    cb, bs = WS(), WS()
    rec.set_wanted((), ("BTCUSD",))
    assert rec._follow(coinbase, cb) and rec._follow(bitstamp, bs)
    assert cb.sent == [{"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches", "heartbeat"]}]
    assert bs.sent == [{"event": "bts:subscribe", "data": {"channel": "live_trades_btcusd"}}]

    rec.set_wanted((), ("ETHUSD",))
    rec._follow(coinbase, cb)
    rec._follow(bitstamp, bs)
    assert cb.sent[1:] == [{"type": "unsubscribe", "product_ids": ["BTC-USD"], "channels": ["matches", "heartbeat"]},
                           {"type": "subscribe", "product_ids": ["ETH-USD"], "channels": ["matches", "heartbeat"]}]
    assert bs.sent[1:] == [{"event": "bts:unsubscribe", "data": {"channel": "live_trades_btcusd"}},
                           {"event": "bts:subscribe", "data": {"channel": "live_trades_ethusd"}}]
    assert set(rec._subs["coinbase"]) == {"ETHUSD"}

    rec.set_wanted((), ())
    assert rec._follow(coinbase, cb) is False  # none open: the connection closes


def test_only_the_pairs_the_exchange_confirmed_are_claimed_as_recorded(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC", "ETH"), sources=("coinbase", "bitstamp"),
                        log=lambda *_: None, connect=lambda url: None, selected_only=True)
    coinbase, bitstamp = rec.streams
    coinbase.parse({"type": "subscriptions", "channels": [{"name": "matches", "product_ids": ["BTC-USD"]},
                                                          {"name": "heartbeat", "product_ids": ["BTC-USD", "ETH-USD"]}]})
    assert coinbase.confirmed == {"BTCUSD"}
    bitstamp.parse({"event": "bts:subscription_succeeded", "channel": "live_trades_ethusd", "data": {}})
    bitstamp.parse({"event": "bts:subscription_succeeded", "channel": "live_trades_btcusd", "data": {}})
    bitstamp.parse({"event": "bts:unsubscription_succeeded", "channel": "live_trades_btcusd", "data": {}})
    assert bitstamp.confirmed == {"ETHUSD"}

    rec._since["coinbase"] = T0
    rec._heard["coinbase"] = T0 + 10_000
    rec._subs["coinbase"] = {"BTCUSD": T0 + 1_000, "ETHUSD": T0}  # ETH asked for, not confirmed
    with Store(tmp_path / "m.db", ticks=True) as store:
        rec.flush(store, now_ms=T0 + 10_000)
        assert store.coverage("BTCUSD", "trades")["coinbase"] == [(T0 + 1_000, T0 + 10_000)]
        assert "coinbase" not in store.coverage("ETHUSD", "trades")
        status = json.loads(store.kv_get(live.STATUS_KEY))
    assert status["selectedOnly"] is True and status["coins"] == []


def test_an_idle_stream_does_not_connect_and_says_why(tmp_path):
    connects = []
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC",), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: connects.append(url), selected_only=True)
    (coinbase,) = rec.streams
    rec.stop.wait = lambda seconds: rec.stop.set()  # one turn of the loop

    with Store(tmp_path / "m.db") as store:
        rec._stream_loop(coinbase, store)
    assert connects == []  # nothing open on a chart: no connection at all
    assert rec._status["coinbase"]["idle"] is True and rec._status["coinbase"]["connected"] is False


def test_always_streams_every_coin_from_the_start(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC", "ETH"), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None)
    (coinbase,) = rec.streams
    assert not coinbase.dynamic
    assert json.loads(coinbase.subscribe(rec.coins)[0])["product_ids"] == ["BTC-USD", "ETH-USD"]
    assert rec._streamed(coinbase, T0, {}) == {"BTCUSD": T0, "ETHUSD": T0}


def test_the_recorder_builds_only_what_it_recorded(tmp_path, monkeypatch):
    """With nothing streamed it reads nothing: it rebuilt every coin's frames each minute, so it read
    what the sync's backfill changed (0.5 to 1 GB a minute with no stream connected)."""
    from massive_collate import derive

    calls = []
    monkeypatch.setattr(derive, "run", lambda store, sources=None, tickers=None, log=None: calls.append(tickers) or 7)
    rec = live.Recorder(tmp_path / "m.db", coins=("BTC", "ETH"), sources=("coinbase",), log=lambda *_: None,
                        connect=lambda url: None, selected_only=True)
    with Store(tmp_path / "m.db", ticks=True) as store:
        assert rec.derive_recorded(store) == 0 and calls == []  # nothing recorded: nothing read
        rec._buffer[("coinbase", "BTCUSD")].append(live._trade((T0 + 1_000) * 1_000_000, 1, "100.5", "0.1", 1))
        rec.flush(store, now_ms=T0 + 5_000)
        assert rec.derive_recorded(store) == 7 and calls == [{"BTCUSD"}]  # only the pair it streamed
        assert rec.derive_recorded(store) == 0 and len(calls) == 1


def test_the_status_says_idle_not_down(tmp_path, capsys):
    from massive_collate import cli

    with Store(tmp_path / "m.db") as store:
        store.kv_set(live.STATUS_KEY, json.dumps({"streams": {
            "coinbase": {"connected": False, "idle": True, "error": None, "pairs": {}},
            "iex": {"connected": False, "error": "alpaca 406: connection limit exceeded", "pairs": {}}}}))
    settings = load_settings(env={"MASSIVE_DB": str(tmp_path / "m.db")}, env_file=None)
    from types import SimpleNamespace

    assert cli.cmd_live(SimpleNamespace(action="status"), settings) == 0
    out = capsys.readouterr().out
    assert "coinbase  idle: nothing of it open on a chart" in out and "iex       down: alpaca 406" in out
