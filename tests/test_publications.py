"""The publisher plugins (sources/publisher.py) read by publications.py: each agency a plugin of
its own, a vendor's headlines in its feed's module; their scrape checks (own hosts over HTTPS,
blocked sites, feeds only, robots.txt, floors and daily caps, redirects); their schedules (the
FOMC's 2:00 p.m. statement); the limits recheck; a pause; the recorder's thread."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from massive_collate import gov, killswitch, publications
from massive_collate.gov import GovDB
from massive_collate.sessions import ET
from massive_collate.sources import providers, publishers
from massive_collate.sources.publisher import Feed, Policy, Publisher

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>FOMC statement</title>
<link>https://www.federalreserve.gov/a.htm</link><guid>a</guid><pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item></channel></rss>"""


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.headers = status, headers or {}
        self.content = text.encode("utf-8")

    def json(self):
        raise ValueError("not JSON")


class Site:
    """One site's answers by path; records each URL asked."""

    def __init__(self, answers=None, robots=Resp(404)):
        self.answers, self.robots, self.asked = answers or {}, robots, []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.asked.append(url)
        if url.endswith("/robots.txt"):
            return self.robots
        return self.answers.get(url) or Resp(text=RSS, headers={"Content-Type": "application/rss+xml"})


def pub(feeds=None, **policy):
    feeds = feeds or (Feed("t-press", "T", "Press", "https://news.example.gov/feed.xml", every_minutes=60),)
    return Publisher("t", "Test agency", "test", feeds, Policy(**{"hosts": ("news.example.gov",), "min_minutes": 5,
                                                                    "per_day": 50, **policy}))


def reader(site, gdb=None, store=None, contact=None):
    return publications.Reader(gdb or GovDB(None), store=store, session=site, contact=contact, log=lambda *_: None,
                               sleep=lambda s: None)


NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


# -- the plugins ------------------------------------------------------------------------------------
def test_each_agency_is_a_plugin_of_its_own_and_massives_headlines_sit_in_its_module():
    pubs = publishers()
    assert {"fed", "nyfed", "sec", "bls", "fedreg", "massive-news"} <= set(pubs)
    assert all(p.origin == "built in" for p in pubs.values())
    assert pubs["massive-news"].kind == "vendor" and pubs["massive-news"].account == "massive"
    assert "fed" not in providers() and "massive" in providers()  # publishers are not feeds
    keys = [f.key for p in pubs.values() for f in p.feeds]
    assert len(keys) == len(set(keys))
    for p in pubs.values():
        for f in p.feeds:
            if p.kind == "official":  # every official feed passes its own checks: HTTPS, its hosts, not blocked
                assert publications.Reader(GovDB(None)).refusal(p, f.url) is None, f.url
    assert pubs["sec"].policy.contact and not pubs["fed"].policy.contact


def test_a_module_with_only_a_publisher_loads_and_a_feed_key_taken_is_left_out(monkeypatch):
    from massive_collate.sources import registry

    twin = Publisher("twin", "Twin", "", (Feed("fed-press", "X", "Dup", "https://x.gov/f"),), Policy(hosts=("x.gov",)),
                     order=900)  # after the Fed's: the first in order keeps the key
    monkeypatch.setattr(registry, "_entry_points", lambda: [type("EP", (), {"name": "twin", "value": "pkg:twin",
                                                                              "load": lambda self: twin})()])
    provs, pubs, errors = registry.discover_all()
    assert "twin" not in pubs and any("feed 'fed-press'" in why for _, why in errors)
    assert "fed" in pubs and "massive" in provs


# -- the scrape checks -----------------------------------------------------------------------------------
def test_a_blocked_site_another_host_or_plain_http_is_never_asked():
    site = Site()
    r = reader(site)
    for url, why in (("https://www.finra.org/rules.xml", "terms bar programs"), ("https://quote.yahoo.com/x", "terms bar"),
                     ("https://other.gov/feed.xml", "not one of its hosts"), ("http://news.example.gov/feed.xml", "not HTTPS")):
        p = pub((Feed("t-x", "T", "X", url),), hosts=("news.example.gov", "www.finra.org", "quote.yahoo.com"))
        assert r.read(p, p.feeds[0], NOW) == 0
        assert why in r.gdb.fetched()["t-x"]["message"]
    assert site.asked == []  # not even robots.txt
    assert publications.blocked("api.nasdaq.com") and not publications.blocked("nasdaqtrader.example")


def test_only_feeds_are_read_a_web_page_is_refused():
    url = "https://news.example.gov/feed.xml"
    site = Site({url: Resp(text="<!doctype html><html>…</html>", headers={"Content-Type": "text/html; charset=utf-8"})})
    r = reader(site)
    p = pub()
    r.read(p, p.feeds[0], NOW)
    row = r.gdb.fetched()["t-press"]
    assert row["status"] == "failed" and "not a feed (text/html" in row["message"] and r.gdb.items() == []
    # without a content type: by how it starts
    assert publications._is_feed(Resp(text="﻿<?xml version='1.0'?><rss/>")) and not publications._is_feed(Resp(text="<html>"))


def test_robots_txt_is_honoured_and_its_crawl_delay_becomes_the_floor():
    site = Site(robots=Resp(text="User-agent: massive-collate\nDisallow: /feed.xml\n"))
    r = reader(site)
    p = pub()
    r.read(p, p.feeds[0], NOW)
    assert r.gdb.fetched()["t-press"]["status"] == "refused" and "disallows" in r.gdb.fetched()["t-press"]["message"]
    assert site.asked == ["https://news.example.gov/robots.txt"]  # the feed itself never asked
    # a crawl-delay longer than its own floor is kept
    slow = Site(robots=Resp(text="User-agent: *\nCrawl-delay: 1200\n"))
    r = reader(slow)
    r.read(p, p.feeds[0], NOW)
    assert r.floor_minutes(p, NOW) == 20 and r.gdb.fetched()["t-press"]["status"] == "ok"
    # no answer (or a 5xx, or a 429): nothing read from the host until it answers
    down = Site(robots=Resp(503))
    r = reader(down)
    r.read(p, p.feeds[0], NOW)
    assert down.asked == ["https://news.example.gov/robots.txt"] and "did not answer" in r.gdb.fetched()["t-press"]["message"]
    r.read(p, p.feeds[0], NOW + timedelta(minutes=publications.ROBOTS_RETRY_MINUTES + 1))
    assert down.asked.count("https://news.example.gov/robots.txt") == 2  # asked again, not at once


def test_a_redirect_is_followed_once_and_only_to_its_own_hosts():
    url = "https://news.example.gov/feed.xml"
    away = Site({url: Resp(301, headers={"Location": "https://elsewhere.example/feed.xml"})})
    r = reader(away)
    p = pub()
    r.read(p, p.feeds[0], NOW)
    assert "elsewhere.example" not in " ".join(away.asked) and "not followed" in r.gdb.fetched()["t-press"]["message"]
    home = Site({url: Resp(301, headers={"Location": "/feeds/new.xml"})})
    r = reader(home)
    r.read(p, p.feeds[0], NOW)
    assert home.asked[-1] == "https://news.example.gov/feeds/new.xml" and r.gdb.fetched()["t-press"]["status"] == "ok"
    # where the contact is sent, no redirect at all: the contact would go with it
    r = reader(home, contact="Pat pat@example.com")
    pc = pub(contact=True)
    r.read(pc, pc.feeds[0], NOW)
    assert "contact" in r.gdb.fetched()["t-press"]["message"]


def test_the_daily_cap_stops_it_robots_txt_included():
    site = Site()
    r = reader(site)
    p = pub(per_day=3)
    for i in range(5):
        r.read(p, p.feeds[0], NOW + timedelta(minutes=10 * i))
    assert len(site.asked) == 3 and r.requests_today(p, NOW) == 3  # robots.txt, then the feed twice
    assert "requests today are spent" in r.gdb.fetched()["t-press"]["message"]


def test_rate_limit_headers_are_kept_and_a_429_holds_it_off():
    url = "https://news.example.gov/feed.xml"
    site = Site({url: Resp(429, headers={"Retry-After": "86400", "X-RateLimit-Limit": "60"})})
    r = reader(site)
    p = pub()
    r.read(p, p.feeds[0], NOW)
    assert r.gdb.limits_kept("t")["t-press"]["X-RateLimit-Limit"] == "60"
    assert r.next_read(p, p.feeds[0], NOW) == NOW + timedelta(days=1)  # its Retry-After, longer than 6 h


# -- when ---------------------------------------------------------------------------------------------------
def test_the_fed_is_read_at_2pm_on_fomc_days_and_otherwise_every_30_minutes():
    from massive_collate.sources.plugins import fed

    p = publishers()["fed"]
    press, speeches = p.feed("fed-press"), p.feed("fed-speeches")
    r = reader(Site())
    last = datetime(2026, 12, 9, 13, 50, tzinfo=ET)  # decision day, with projections
    row = {"at": last.isoformat(), "status": "ok"}
    assert r.next_read(p, press, last, row) == datetime(2026, 12, 9, 14, 0, 20, tzinfo=ET)
    assert r.next_read(p, speeches, last, row) == last + timedelta(minutes=60)  # speeches: not at the moments
    after = {"at": datetime(2026, 12, 9, 14, 0, 20, tzinfo=ET).isoformat(), "status": "ok"}
    assert r.next_read(p, press, last, after) == datetime(2026, 12, 9, 14, 1, 30, tzinfo=ET)
    plain = {"at": datetime(2026, 12, 10, 13, 50, tzinfo=ET).isoformat(), "status": "ok"}
    assert r.next_read(p, press, last, plain) == datetime(2026, 12, 10, 14, 20, tzinfo=ET)  # an ordinary day
    assert fed.moments(date(2026, 11, 18))  # the October minutes: three weeks after the decision
    assert not fed.moments(date(2026, 11, 19))
    assert set(fed.FOMC_DECISIONS) >= {date(2026, 10, 28), date(2026, 12, 9), date(2027, 6, 9)}


def test_a_moment_never_comes_within_the_floor():
    moment = datetime(2026, 10, 26, 9, 0, tzinfo=ET)
    p = Publisher("t", "T", "", (Feed("t-press", "T", "P", "https://news.example.gov/f.xml", every_minutes=60),),
                  Policy(hosts=("news.example.gov",), min_minutes=30), moments=lambda d: (moment,))
    r = reader(Site())
    row = {"at": (moment - timedelta(minutes=10)).isoformat(), "status": "ok"}
    assert r.next_read(p, p.feeds[0], moment, row) == moment + timedelta(minutes=20)  # 30 min after the last read


# -- the limits recheck ---------------------------------------------------------------------------------------
def test_the_limits_are_verified_weekly_and_a_recheck_never_asks_within_the_floor():
    site = Site(robots=Resp(text="User-agent: *\nCrawl-delay: 600\n"))
    r = reader(site)
    p = pub()
    r.step(NOW, publishers={"t": p})  # the first pass is the recheck: robots.txt, each feed once
    found = r.gdb.recheck_get("t")
    assert found["hosts"]["news.example.gov"]["crawlDelayMinutes"] == 10 and found["floorMinutes"] == 10
    assert any("longer than its own" in n for n in found["notes"])
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/feed.xml"]
    site.asked.clear()
    r.recheck(p, NOW + timedelta(minutes=1))  # pressed again at once: robots.txt again, the feed not
    assert site.asked == ["https://news.example.gov/robots.txt"]
    site.asked.clear()
    r.step(NOW + timedelta(days=publications.RECHECK_DAYS, minutes=1), publishers={"t": p})
    assert site.asked[0].endswith("robots.txt")  # a week on: verified again


def test_nothing_goes_to_a_publisher_that_wants_a_contact_until_it_is_set():
    site = Site()
    r = reader(site)
    p = pub(contact=True)
    r.step(NOW, publishers={"t": p})
    assert site.asked == [] and r.gdb.fetched()["t-press"]["status"] == "waiting"
    assert "nothing is sent" in r.recheck(p, NOW)["notes"][0]
    r = reader(site, gdb=r.gdb, contact="Pat pat@example.com")
    r.step(NOW + timedelta(minutes=1), publishers={"t": p})
    assert site.asked[-1] == "https://news.example.gov/feed.xml"


# -- a vendor's headlines, a pause --------------------------------------------------------------------------
class FakeNews:
    def __init__(self, fail=None):
        self.client, self.fail, self.asked = type("C", (), {"on_request": None})(), fail, []

    def news(self, since, limit):
        self.asked.append(since)
        if self.client.on_request:
            self.client.on_request(1)
        if self.fail:
            raise self.fail
        return [{"id": "n1", "published_ms": 1_761_000_000_000, "title": "Headline", "publisher": "Wire",
                 "url": "https://x/1", "tickers": ["AAPL"]}]


@pytest.fixture
def massive_news(monkeypatch):
    from massive_collate.config import load_settings
    from massive_collate.sources.plugins import massive

    fake = FakeNews()
    monkeypatch.setattr(massive, "build", lambda settings, budget: fake)
    settings = load_settings(env={"MASSIVE_API_KEY": "k"}, env_file=None)
    return fake, settings, publishers()["massive-news"]


def test_massives_headlines_are_read_on_its_key_and_counted_on_its_day(store, massive_news):
    fake, settings, p = massive_news
    r = publications.Reader(GovDB(None), settings, store, session=Site(), log=lambda *_: None)
    assert r.set_up(p)
    r.read(p, p.feeds[0], NOW)
    assert [n["title"] for n in store.news("massive")] == ["Headline"] and store.kv_get("news:massive")
    assert store.requests_today("massive", NOW) == 1 and r.requests_today(p, NOW) == 1
    assert not publications.Reader(GovDB(None), None, store).set_up(p)  # no settings, no key: not set up


def test_a_429_on_massives_headlines_kills_massives_fetching(store, massive_news):
    from massive_collate.errors import RateLimited

    fake, settings, p = massive_news
    fake.fail = RateLimited(429, "too many", 300)
    r = publications.Reader(GovDB(None), settings, store, session=Site(), log=lambda *_: None)
    r.read(p, p.feeds[0], NOW)
    assert killswitch.killed(store, "massive", "fetch") and r.gdb.fetched()["massive-news"]["status"] == "refused"
    fake.asked.clear()
    r.step(datetime.now(timezone.utc), publishers={"massive-news": p})
    assert fake.asked == []  # its feed killed: not asked


def test_a_publisher_paused_by_hand_is_not_read_until_resumed(store):
    site = Site()
    r = reader(site, store=store)
    p = pub()
    publications.pause(store, "t", True)
    r.step(datetime.now(timezone.utc), publishers={"t": p})
    assert site.asked == []
    publications.pause(store, "t", False)
    r.step(datetime.now(timezone.utc), publishers={"t": p})
    assert site.asked


# -- the views, the recorder ------------------------------------------------------------------------------------
def test_the_plugins_view_lists_the_publishers_with_their_checks(store):
    from massive_collate.gui.server import ChartApp

    app = ChartApp(store)
    out = {p["name"]: p for p in app.plugins()["publishers"]}
    fed = out["fed"]
    assert fed["policy"]["hosts"] == ["www.federalreserve.gov"] and "finra.org" in fed["policy"]["blocked"]
    assert fed["feeds"][0]["state"] == "not asked yet" and fed["floorMinutes"] == 1
    assert out["sec"]["state"] == "waiting" and out["massive-news"]["kind"] == "vendor"
    app.pause_publisher("fed", True)
    assert {p["name"]: p for p in app.plugins()["publishers"]}["fed"]["state"] == "paused"
    with pytest.raises(LookupError):
        app.pause_publisher("nobody", True)


def test_the_recorder_reads_the_publications_in_a_thread_of_its_own(tmp_path, monkeypatch):
    from massive_collate import live

    passes = []
    rec = live.Recorder(tmp_path / "m.db", coins=(), publications=object())

    class OnePass:
        def __init__(self, gdb, settings, store, log=print):
            passes.append(settings)

        def step(self, now):
            rec.stop.set()
            return 1.0

    monkeypatch.setattr(publications, "Reader", OnePass)
    rec._publications_loop()
    assert len(passes) == 1 and not publications.lock_path(tmp_path / "m.db").exists()  # the lock let go
    assert live.Recorder(tmp_path / "m.db", coins=())._start_publications() is None  # not asked for: none
