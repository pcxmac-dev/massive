"""Public historical sources as closed feeds: request shapes, split adjustment, empty history heads,
and closing (filled once, then never asked again)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from massive_collate.collector import ET, sync
from massive_collate.config import ALPACA_TIERS, PUBLIC_FEEDS, TIERS
from massive_collate.coverage import FeedProfile, ms, plan
from massive_collate.errors import NotEntitled
from massive_collate.sources import supports
from massive_collate.sources.public import BitstampSource, CboeSource, PublicClient, TiingoSource, split_adjust
from massive_collate.symbols import resolve

from .conftest import FakeResponse, FakeSession, FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)


def _log(_):
    pass


def _client(responses):
    session = FakeSession(responses)
    return PublicClient("https://x.test/api", session=session, sleep=lambda s: None), session


def _ohlc(ts, v="1.0"):
    return {"timestamp": str(ts), "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": v}


def test_bitstamp_pages_newest_first_and_drops_empty_minutes():
    t0 = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
    page1 = {"data": {"ohlc": [_ohlc(t0 + 60 * i, "0.0" if i % 2 else "2.0") for i in range(5, 10)]}}
    page2 = {"data": {"ohlc": [_ohlc(t0 + 60 * i) for i in range(0, 5)]}}
    client, session = _client([FakeResponse(200, page1), FakeResponse(200, page2), FakeResponse(200, page2)])
    src = BitstampSource(client)
    src.PAGE = 5
    pages = list(src.bar_pages("X:BTCUSD", datetime.fromtimestamp(t0, timezone.utc),
                               datetime.fromtimestamp(t0 + 600, timezone.utc), "minute", descending=True))
    assert [b["t"] // 1000 - t0 for b in pages[0]] == [480, 360]  # zero-volume minutes dropped, newest first
    assert len(pages[1]) == 5
    url, params, headers = session.calls[0]
    assert url.endswith("/ohlc/btcusd/") and params == {"step": 60, "limit": 5, "end": t0 + 540}
    assert session.calls[1][1]["end"] == t0 + 240 and "massive-collate" in headers["User-Agent"]
    assert len(session.calls) == 2  # stops once a page reaches the window's start


def test_cboe_reads_one_file_for_all_history():
    csv_text = "DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.24,17.24,17.24\n01/03/1990,18.19,18.5,18.0,18.19\n"
    resp = FakeResponse(200, None)
    resp.text = csv_text
    client, session = _client([resp])
    src = CboeSource(client)
    bars = next(src.bar_pages("I:VIX", date(1990, 1, 1), date(1990, 1, 2), "day"))
    assert [b["c"] for b in bars] == [17.24] and bars[0]["t"] == ms(datetime(1990, 1, 2, tzinfo=ET))
    assert len(next(src.bar_pages("VIX", date(1990, 1, 1), date(1990, 1, 31), "day"))) == 2
    assert len(session.calls) == 1 and session.calls[0][0].endswith("/VIX_History.csv")  # cached
    with pytest.raises(NotEntitled):
        next(src.bar_pages("I:COMP", date(1990, 1, 1), date(1990, 1, 2), "day"))


def test_tiingo_is_split_adjusted_only_and_needs_a_key():
    rows = [{"date": "2020-08-27T00:00:00.000Z", "open": 500.0, "high": 510.0, "low": 490.0, "close": 500.0,
             "volume": 100, "splitFactor": 1.0, "divCash": 0.8},
            {"date": "2020-08-31T00:00:00.000Z", "open": 127.0, "high": 131.0, "low": 126.0, "close": 129.0,
             "volume": 400, "splitFactor": 4.0, "divCash": 0.0}]
    out = split_adjust(rows)
    assert out[0]["c"] == 125.0 and out[0]["v"] == 400.0  # before the 4:1 split: /4 prices, x4 volume
    assert out[1]["c"] == 129.0 and out[1]["v"] == 400.0  # the split day is already in new shares
    assert out[0]["t"] == ms(datetime(2020, 8, 27, tzinfo=ET))
    client, session = _client([FakeResponse(200, rows)])
    keyed = TiingoSource(PublicClient("https://api.tiingo.com", headers={"Authorization": "Token K"},
                                      session=session, sleep=lambda s: None), has_key=True)
    bars = next(keyed.bar_pages("BRK.B", date(2020, 1, 1), date(2020, 12, 31), "day"))
    assert len(bars) == 2 and session.calls[0][0].endswith("/tiingo/daily/BRK-B/prices")
    assert session.calls[0][2]["Authorization"] == "Token K"
    keyless = TiingoSource(_client([])[0], has_key=False)
    assert not keyless.serves("stock") and keyless.resolutions("stock") == ()
    with pytest.raises(NotEntitled):
        next(keyless.bar_pages("AAPL", date(2020, 1, 1), date(2020, 1, 2), "day"))


def test_supports():
    assert supports("bitstamp", resolve("BTCUSD")) and not supports("bitstamp", resolve("AAPL"))
    assert supports("cboe", resolve("I:VIX")) and not supports("cboe", resolve("IXIC"))
    assert supports("tiingo", resolve("AAPL")) and not supports("tiingo", resolve("EURUSD"))


class Archive(FakeSource):
    name = "bitstamp"
    archival = True

    def covers(self, ticker):
        return True

    def resolutions(self, asset):
        return ("minute",)


def _minutes(day, n=3, price=100.0):
    t0 = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    return [{"t": ms(t0 + timedelta(minutes=i)), "o": price, "h": price, "l": price, "c": price, "v": 1} for i in range(n)]


def _feeds():
    return {"alpaca": FeedProfile("alpaca", ALPACA_TIERS["basic"], frozenset({"crypto"}), 1),
            "bitstamp": FeedProfile("bitstamp", PUBLIC_FEEDS["bitstamp"][0], frozenset({"crypto"}), 2)}


def test_a_feeds_empty_history_head_is_left_to_others_and_the_archive_closes(store):
    for feed in ("alpaca", "bitstamp"):
        store.watch_spec(feed, resolve("BTCUSD"), "minute")
    # Alpaca was asked from 2016 but its history starts in 2021: that head is 'none' for it
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, _minutes(date(2021, 1, 1)))
    store.add_coverage("alpaca", "BTCUSD", "minute", ms(datetime(2016, 1, 1, tzinfo=ET)), ms(NOW))
    assert store.mark_empty_heads() == 1
    assert store.coverage("BTCUSD", "minute")["alpaca"][0][0] == _minutes(date(2021, 1, 1))[0]["t"]
    gaps = plan(store, _feeds()["bitstamp"], _feeds(), "BTCUSD", "crypto", "minute", date(2011, 8, 1), NOW)
    assert gaps and gaps[-1][1] == _minutes(date(2021, 1, 1))[0]["t"]  # up to where Alpaca's data begins
    archive = Archive(entitled=("minute",), bars={"minute": _minutes(date(2014, 6, 1)), "day": []})
    sync(archive, store, PUBLIC_FEEDS["bitstamp"][0], NOW, log=_log, feeds=_feeds())
    assert archive.bar_calls and all(e <= datetime(2021, 1, 1, 12, tzinfo=timezone.utc) for _, s, e, ts, _ in archive.bar_calls)
    row = next(w for w in store.watchlist("bitstamp") if w["ticker"] == "BTCUSD")
    assert row["closed_at"] is not None  # archive complete
    # closed: later runs send nothing at all, whatever the date
    again = Archive(entitled=("minute",), bars={"minute": _minutes(date(2014, 6, 2))})
    sync(again, store, PUBLIC_FEEDS["bitstamp"][0], NOW + timedelta(days=30), log=_log, feeds=_feeds())
    assert again.requests_made == 0 and not getattr(again, "bar_calls", [])


def test_closed_feeds_are_not_started_and_can_be_reopened(tmp_path, monkeypatch, capsys):
    from massive_collate import cli
    from massive_collate.storage import Store

    db = tmp_path / "m.db"
    monkeypatch.setenv("MASSIVE_DB", str(db))
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    settings = cli.load_settings(env_file=None)
    with Store(db) as s:
        s.watch_spec("tiingo", resolve("AAPL"))
        s.set_closed("tiingo", "AAPL", NOW)
    assert cli._feed_closed(settings, "tiingo") and cli._feed_closed(settings, "cboe")  # nothing watched: closed too
    assert not cli._feed_closed(settings, "bitstamp")  # an exchange: live
    assert not cli._feed_closed(settings, "massive")
    assert cli.main(["feeds"]) == 0
    out = capsys.readouterr().out
    assert "tiingo    closed feed (public archive)" in out and "closed: archive complete" in out
    assert cli.main(["feeds", "reopen", "tiingo", "AAPL"]) == 0
    assert not cli._feed_closed(settings, "tiingo")
