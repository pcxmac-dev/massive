"""Fixes from the code review (CODE_REVIEW.md) in the sources and live feeds: a refused key, server
waits and redirects, the live streams' failures, the shared request window, closed archives and
the continuous futures roll."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from massive_collate import cli, killswitch
from massive_collate.errors import NotEntitled, Paused, RateLimited, SourceError, Unauthorized
from massive_collate.sources.alpaca import AlpacaClient
from massive_collate.sources.massive import RETRY_WAIT_MAX, MassiveClient, MassiveSource, SameHostSession, _retry_delay
from massive_collate.storage import Store

from .conftest import FakeResponse, FakeSession


def _client(responses, **kw):
    session = FakeSession(responses)
    kw.setdefault("sleep", lambda s: None)
    return MassiveClient("KEY", session=session, **kw), session


# -- a refused key (finding 2) ---------------------------------------------------------------------

def test_a_401_is_the_key_refused_not_a_plan_and_nothing_more_is_sent():
    client, session = _client([FakeResponse(401, {"status": "ERROR", "error": "Unknown API Key"})] * 3)
    with pytest.raises(Unauthorized) as first:
        client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
    assert not isinstance(first.value, NotEntitled) and killswitch.is_refusal(first.value, first.value.status)
    with pytest.raises(Unauthorized, match="nothing sent"):
        client.get("/v2/aggs/ticker/MSFT/range/1/minute/2026-10-01/2026-10-02")
    assert len(session.calls) == client.requests_made == 1  # not retried, and not asked again


def test_a_probe_with_a_refused_key_is_not_an_answer_about_the_plan():
    src = MassiveSource(_client([FakeResponse(401, {"error": "Unknown API Key"})])[0])
    with pytest.raises(Unauthorized):  # "not on your plan" would be remembered for a week
        src.is_entitled("second", "AAPL")
    src = MassiveSource(_client([FakeResponse(403, {"message": "NOT_AUTHORIZED"})])[0])
    assert src.is_entitled("second", "AAPL") is False  # a 403 is the plan


def test_a_revoked_key_stops_the_sync_after_one_request_and_kills_the_feed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "data" / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "FAKE")
    sent = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        sent.append(url)
        return FakeResponse(401, {"status": "ERROR", "error": "Unknown API Key"})

    monkeypatch.setattr(requests.Session, "get", get)
    for ticker in ("AAPL", "MSFT", "NVDA", "AMZN"):
        assert cli.main(["watch", "add", ticker]) == 0
    cli.main(["sync"])
    assert len([u for u in sent if "api.massive.com" in u]) == 1  # not one 401 per symbol until the cap
    with Store(tmp_path / "data" / "m.db") as s:
        k = killswitch.killed(s, "massive", "fetch")
        assert k and "401" in k["why"]


def test_alpaca_keys_the_trading_api_refuses_lose_the_reference_record_only():
    session = FakeSession([FakeResponse(401, {"message": "unauthorized."}), FakeResponse(200, {"bars": {}}),
                           FakeResponse(401, {"message": "unauthorized."})])
    client = AlpacaClient("KID", "SECRET", session=session, sleep=lambda s: None)
    with pytest.raises(NotEntitled, match="ALPACA_PAPER"):  # paper keys on the live API, or the reverse
        client.get(f"{client.trading_url}/v2/assets/AAPL")
    with pytest.raises(NotEntitled, match="nothing sent"):
        client.get(f"{client.trading_url}/v2/assets/MSFT")
    assert client.get("/v2/stocks/bars") == {"bars": {}}  # the data API still answers
    with pytest.raises(Unauthorized):  # the data API refusing them is the keys refused
        client.get("/v2/stocks/bars")
    assert len(session.calls) == 3


# -- server waits and redirects (low) --------------------------------------------------------------

def test_retry_after_as_an_http_date_and_past_the_longest_wait():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    when = FakeResponse(503, headers={"Retry-After": "Mon, 26 Oct 2026 12:00:45 GMT"})
    assert _retry_delay(when, 0, now=now) == 45.0
    assert _retry_delay(FakeResponse(503, headers={"Retry-After": "nan"}), 1) == 10.0  # unreadable: own backoff
    assert _retry_delay(FakeResponse(503, headers={"Retry-After": "Mon, 26 Oct 2026 11:00:00 GMT"}), 0, now=now) == 0.0

    slept = []
    client, session = _client([FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": str(RETRY_WAIT_MAX + 1)}),
                               FakeResponse(200, {})], sleep=slept.append)
    with pytest.raises(RateLimited):  # not waited out for hours, not asked again sooner than told
        client.get("/x")
    assert len(session.calls) == 1 and slept == []


def test_a_pause_ends_a_long_wait_for_the_server():
    slept = []
    client, session = _client([FakeResponse(503, {}, headers={"Retry-After": "120"}), FakeResponse(200, {})],
                              sleep=slept.append)
    client.should_stop = lambda: "fetching paused by you" if len(slept) >= 3 else None
    with pytest.raises(Paused):
        client.get("/x")
    assert len(session.calls) == 1 and sum(slept) < 120  # stopped part way, in slices


def _redirect(location: str, url: str = "https://data.alpaca.markets/v2/stocks/bars") -> requests.Response:
    r = requests.Response()
    r.status_code, r.url = 302, url
    r.headers["Location"] = location
    return r


def test_a_redirect_is_followed_on_the_same_host_only():
    s = SameHostSession()
    assert s.get_redirect_target(_redirect("/v2/stocks/bars?page=2")) == "/v2/stocks/bars?page=2"
    assert s.get_redirect_target(_redirect("https://evil.test/collect")) is None  # the key headers stay home
    assert s.get_redirect_target(_redirect("http://data.alpaca.markets/v2/x")) is None  # nor in clear text
    client, _ = _client([FakeResponse(302, {}, headers={"Location": "https://evil.test/collect"})])
    with pytest.raises(SourceError, match="redirected to evil.test; not followed"):
        client.get("/x")
    assert isinstance(MassiveClient("K")._session, SameHostSession)
    assert isinstance(AlpacaClient("KID", "SECRET")._session, SameHostSession)


def test_each_asset_classs_window_keeps_the_margin_of_a_few_a_minute():
    from massive_collate.config import load_settings
    from massive_collate.sources import build_source

    free = build_source("massive", load_settings(env={"MASSIVE_API_KEY": "FAKE"}, env_file=None)).client
    assert free._limiter.period == 62.0
    # Advanced stocks (unlimited), the other classes on the free plan: 5 a minute each, the same margin
    client = build_source("massive", load_settings(env={"MASSIVE_API_KEY": "FAKE", "MASSIVE_TIER": "advanced"},
                                                   env_file=None)).client
    assert client.class_limiters["crypto"].max_calls == 5
    assert all(lim.period == 62.0 for lim in client.class_limiters.values())


def test_the_futures_day_window_ends_with_the_last_days_session():
    client, session = _client([FakeResponse(200, {"results": []})])
    list(MassiveSource(client).bar_pages("ESZ6", date(2026, 10, 19), date(2026, 10, 23), "day", asset="future"))
    params = session.calls[0][1]
    # the session settling on the 19th opened on the 18th's evening; the one opening on the 23rd's
    # evening settles on the 26th: not asked for
    assert params["window_start.gte"] == "2026-10-18" and params["window_start.lt"] == "2026-10-23"
