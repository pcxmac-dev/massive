"""The guarantees that keep usage inside the plan: rate limit, request cap, refusals."""
from __future__ import annotations

import pytest

from massive_collate.config import ConfigError, load_settings
from massive_collate.errors import BudgetExhausted, NotEntitled, RateLimited, SourceError
from massive_collate.ratelimit import RateLimiter
from massive_collate.sources.massive import MassiveClient

from .conftest import FakeResponse, FakeSession


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_never_exceeds_window():
    clock = FakeClock()
    limiter = RateLimiter(5, 60.0, clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(12):
        limiter.acquire()
        stamps.append(clock.now)
    for i, t in enumerate(stamps):
        assert sum(1 for s in stamps if t <= s < t + 60) <= 5, i
    assert stamps[5] >= 60


def test_unlimited_limiter_does_not_sleep():
    clock = FakeClock()
    limiter = RateLimiter(None, clock=clock, sleep=clock.sleep)
    for _ in range(100):
        limiter.acquire()
    assert clock.sleeps == []


def _client(responses, **kw):
    session = FakeSession(responses)
    return MassiveClient("KEY", session=session, sleep=lambda s: None, **kw), session


def test_key_sent_in_header_not_url():
    client, session = _client([FakeResponse(200, {"results": []})])
    client.get("/v2/x", {"a": 1})
    url, params, headers = session.calls[0]
    assert headers["Authorization"] == "Bearer KEY"
    assert "KEY" not in url and "KEY" not in str(params)


def test_request_cap_is_hard():
    client, session = _client([FakeResponse(200, {})] * 5, request_budget=3)
    for _ in range(3):
        client.get("/x")
    with pytest.raises(BudgetExhausted):
        client.get("/x")
    assert len(session.calls) == 3


def test_retries_count_against_cap():
    client, session = _client([FakeResponse(429, {})] * 5, request_budget=2, max_retries=4)
    with pytest.raises(BudgetExhausted):
        client.get("/x")
    assert len(session.calls) == 2


def test_429_retries_then_raises():
    client, session = _client([FakeResponse(429, {"message": "slow down"})] * 3, max_retries=2)
    with pytest.raises(RateLimited):
        client.get("/x")
    assert len(session.calls) == 3


def test_403_is_not_retried():
    client, session = _client([FakeResponse(403, {"message": "NOT_AUTHORIZED"})])
    with pytest.raises(NotEntitled):
        client.get("/x")
    assert len(session.calls) == 1


def test_pagination_follows_next_url_on_same_host_only():
    client, session = _client([
        FakeResponse(200, {"results": [1], "next_url": "https://api.massive.com/v3/next?cursor=a"}),
        FakeResponse(200, {"results": [2]}),
    ])
    assert list(client.pages("/v3/x")) == [[1], [2]]

    client, _ = _client([FakeResponse(200, {"results": [1], "next_url": "https://evil.example/steal"})])
    with pytest.raises(SourceError):
        list(client.pages("/v3/x"))


def test_settings_defaults_to_free_plan(tmp_path):
    s = load_settings(env={}, env_file=None)
    assert s.tier.name == "free" and s.tier.requests_per_minute == 5 and s.tier.history_years == 2
    assert s.max_requests_per_run == 250 and s.max_requests_per_day == 2000


def test_settings_rejects_unknown_tier():
    with pytest.raises(ConfigError):
        load_settings(env={"MASSIVE_TIER": "platinum"}, env_file=None)


def test_env_file_does_not_override_environment(tmp_path):
    f = tmp_path / ".env"
    f.write_text("MASSIVE_API_KEY=fromfile\nMASSIVE_TIER=starter\n")
    s = load_settings(env={"MASSIVE_API_KEY": "fromenv"}, env_file=f)
    assert s.api_key == "fromenv" and s.tier.name == "starter"
