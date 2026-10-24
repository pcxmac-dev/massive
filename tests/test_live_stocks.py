"""Live stocks: Alpaca's IEX stream (messages, symbols following the chart and the watchlists), the
recorder storing its trades in their own feed, and charts ranking that feed below the whole market."""
from __future__ import annotations

import json

import pytest

from massive_collate import live
from massive_collate.config import load_settings
from massive_collate.gui.bars import BarEngine
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store

T0 = 1_792_764_000_000  # 2026-10-23 14:00 UTC (10:00 in New York), ms


def _trade(i: int, price: float, size: int, iso: str) -> dict:
    return {"T": "t", "S": "AAPL", "i": i, "x": "V", "p": price, "s": size, "c": ["@"], "t": iso, "z": "C"}


def _iex() -> live.AlpacaIexStream:
    return live.AlpacaIexStream("PKTESTKEY", "test-secret")


def test_iex_messages_become_trades_withdrawals_and_refusals():
    s = _iex()
    (auth,) = s.subscribe(())
    assert json.loads(auth) == {"action": "auth", "key": "PKTESTKEY", "secret": "test-secret"}
    assert "test-secret" not in repr(s) and "PKTESTKEY" not in repr(s)  # never printed
    assert s.parse([{"T": "success", "msg": "connected"}, {"T": "success", "msg": "authenticated"}]) == [] and s.authed
    s.parse([{"T": "subscription", "trades": ["AAPL", "MSFT"], "quotes": [], "bars": []}])
    assert s.confirmed == {"AAPL", "MSFT"}

    ((ticker, t),) = s.parse([_trade(1, 230.0, 100, "2026-10-23T14:00:01.123456789Z")])
    assert ticker == "AAPL" and t["sip_timestamp"] == (T0 + 1123) * 1_000_000 + 456_789
    assert t["exchange"] == ord("V") and t["tape"] == 3 and t["sequence_number"] == (1 << 6) | (ord("V") & 63)

    ((_, gone),) = s.parse({"T": "x", "S": "AAPL", "i": 1, "x": "V", "p": 230.0, "s": 100, "a": "C",
                            "t": "2026-10-23T14:00:01.123456789Z", "z": "C"})
    assert gone["withdrawn"] and gone["sequence_number"] == t["sequence_number"]
    was, now = s.parse({"T": "c", "S": "AAPL", "x": "V", "oi": 3, "op": 10.0, "os": 5, "oc": ["@"], "ci": 4,
                        "cp": 10.5, "cs": 5, "cc": ["@"], "t": "2026-10-23T14:00:02Z", "z": "C"})
    assert was[1]["withdrawn"] and was[1]["price"] == 10.0 and not now[1].get("withdrawn") and now[1]["price"] == 10.5

    with pytest.raises(live.Refused) as refused:
        s.parse([{"T": "error", "code": 402, "msg": "auth failed"}])
    assert refused.value.wait == 600  # bad keys: not tried again for ten minutes
    with pytest.raises(live.Notice):
        s.parse([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])  # the connection stays


def test_the_stream_follows_the_symbols_wanted_once_the_keys_are_accepted(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=(), stock_keys=("PKTESTKEY", "test-secret"),
                        log=lambda *_: None, connect=lambda url: None)
    assert [s.source for s in rec.streams] == ["iex"] and "test-secret" not in repr(rec)
    iex = rec.streams[0]

    class WS:
        sent = []

        def send(self, m):
            self.sent.append(json.loads(m))

    ws = WS()
    rec.set_wanted(("AAPL", "MSFT"))
    assert rec._follow(iex, ws) and ws.sent == []  # nothing before the keys are accepted
    iex.parse({"T": "success", "msg": "authenticated"})
    rec._follow(iex, ws)
    assert ws.sent == [{"action": "subscribe", "trades": ["AAPL", "MSFT"]}]
    rec.set_wanted(("MSFT", "TSLA"))
    rec._follow(iex, ws)
    assert ws.sent[1:] == [{"action": "unsubscribe", "trades": ["AAPL"]}, {"action": "subscribe", "trades": ["TSLA"]}]
    assert set(rec._subs["iex"]) == {"MSFT", "TSLA"}
    rec.set_wanted(())
    assert rec._follow(iex, ws) is False  # none wanted: the connection closes


def test_the_stocks_wanted_are_the_open_charts_then_the_watchlists(tmp_path, monkeypatch):
    with Store(tmp_path / "m.db") as store:
        store.watch("massive", ["AAPL", "MSFT", "TSLA", "NVDA"], "minute")
        store.watch("coinbase", ["BTCUSD"], "minute", asset="crypto")
        store.kv_set(live.OPEN_KEY, json.dumps({"NVDA": T0 - 10_000, "TSLA": T0 - 1_000, "MSFT": T0 - 999_999}))
        store.kv_set(live.WATCHLISTS_KEY, json.dumps([{"name": "Tech", "tickers": ["AAPL", "BTCUSD", "ZZZZ", "tsla"]}]))
        # the most recently opened first; MSFT was open too long ago; crypto and unknown symbols are not stocks
        assert live.wanted_stocks(store, T0) == ("TSLA", "NVDA", "AAPL")
        monkeypatch.setattr(live, "MAX_STOCKS", 2)
        assert live.wanted_stocks(store, T0) == ("TSLA", "NVDA")
        store.kv_set(live.OPEN_KEY, "not json")
        assert live.wanted_stocks(store, T0) == ("AAPL", "TSLA")  # the watchlist alone


def test_iex_trades_are_stored_in_their_own_feed_and_withdrawals_rebuild_the_bars(tmp_path):
    rec = live.Recorder(tmp_path / "m.db", coins=(), stock_keys=("PKTESTKEY", "test-secret"),
                        log=lambda *_: None, connect=lambda url: None)
    iex = rec.streams[0]
    iex.confirmed = frozenset({"AAPL"})
    rec._since["iex"] = T0
    rec._subs["iex"] = {"AAPL": T0 + 1000, "MSFT": T0}  # MSFT asked for, not confirmed by the server

    def receive(msgs):
        for ticker, t in iex.parse(msgs):
            rec._buffer[("iex", ticker)].append(t)

    receive([_trade(1, 230.0, 100, "2026-10-23T14:00:01.5Z"), _trade(2, 231.0, 50, "2026-10-23T14:00:03.25Z")])
    with Store(tmp_path / "m.db") as store:
        assert rec.flush(store, now_ms=T0 + 10_000) == 2
        assert store.coverage("AAPL", "trades")["iex"] == [(T0 + 1000, T0 + 10_000)]  # from its subscription on
        assert "iex" not in store.coverage("MSFT", "trades")
        conn = store._bars("iex")
        assert conn.execute("SELECT timespan, COUNT(*) FROM built_bars GROUP BY timespan").fetchall() == [
            ("minute", 1), ("second", 2)]
        (d,) = store.derivations(kind="bars-from-ticks")
        assert "IEX" in d["settings"]["method"] and "rule_count" in d["settings"]

        receive({"T": "x", "S": "AAPL", "i": 2, "x": "V", "p": 231.0, "s": 50, "a": "C",
                 "t": "2026-10-23T14:00:03.25Z", "z": "C"})  # trade 2 canceled
        assert rec.flush(store, now_ms=T0 + 12_000) == 0
        assert store.trade_edges("iex", "AAPL") == ((T0 + 1500) * 1_000_000, (T0 + 1500) * 1_000_000)
        assert conn.execute("SELECT timespan, COUNT(*) FROM built_bars GROUP BY timespan").fetchall() == [
            ("minute", 1), ("second", 1)]  # the second it traded in is gone
        assert conn.execute("SELECT high, close, volume FROM built_bars WHERE timespan='minute'").fetchone() == (
            230.0, 230.0, 100.0)
        status = json.loads(store.kv_get(live.STATUS_KEY))
        assert status["streams"]["iex"]["symbols"] == ["AAPL"] and status["streams"]["iex"]["pairs"]["AAPL"]["trades"] == 1


def test_a_merged_chart_ranks_iex_below_the_whole_market(tmp_path):
    with Store(tmp_path / "m.db") as store:
        store.upsert_bars("massive", "AAPL", "minute", 1, [{"t": T0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 10}])
        store.upsert_trades("iex", "AAPL", [{"sip_timestamp": (T0 + 60_000) * 1_000_000, "sequence_number": 1,
                                             "price": 2.0, "size": 1, "exchange": ord("V"), "tape": 3}])
        store.upsert_trades("alpaca", "AAPL", [{"sip_timestamp": T0 * 1_000_000, "sequence_number": 1,
                                                "price": 1.0, "size": 1, "exchange": ord("Q"), "tape": 3}])
        # ticks rank a feed first, but never a feed of one exchange's share of the market
        assert BarEngine(store).sources_for("AAPL") == ["alpaca", "massive", "iex"]


def test_the_page_notes_its_open_symbol_for_the_recorder(tmp_path):
    store = Store(tmp_path / "m.db", check_same_thread=False)
    app = ChartApp(store)
    app.note_open("aapl", now_ms=1_000)
    assert json.loads(store.kv_get(live.OPEN_KEY)) == {"AAPL": 1_000}
    app.note_open("AAPL", now_ms=2_000)  # within OPEN_NOTE_SECONDS: not written again
    assert json.loads(store.kv_get(live.OPEN_KEY)) == {"AAPL": 1_000}
    app.note_open("MSFT", now_ms=1_000 + live.OPEN_FRESH_MS + 1)  # AAPL no longer noted: dropped
    assert json.loads(store.kv_get(live.OPEN_KEY)) == {"MSFT": 1_000 + live.OPEN_FRESH_MS + 1}
    store.close()


def test_stocks_stream_only_with_keys_online_and_not_turned_off():
    keys = {"ALPACA_API_KEY_ID": "PKTESTKEY", "ALPACA_API_SECRET_KEY": "test-secret"}
    assert live.stock_keys(load_settings(env=keys, env_file=None)) == ("PKTESTKEY", "test-secret")
    assert live.stock_keys(load_settings(env={**keys, "LIVE_STOCKS": "off"}, env_file=None)) is None
    assert live.stock_keys(load_settings(env={**keys, "MASSIVE_OFFLINE": "1"}, env_file=None)) is None
    assert live.stock_keys(load_settings(env={}, env_file=None)) is None
