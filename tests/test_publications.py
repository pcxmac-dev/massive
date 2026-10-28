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
    r.recheck(p, NOW + timedelta(minutes=1))  # pressed again at once: robots.txt read within the hour, the feed in its floor
    assert site.asked == []
    r.recheck(p, NOW + timedelta(minutes=61))
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/feed.xml"]
    site.asked.clear()
    r.step(NOW + timedelta(days=publications.RECHECK_DAYS, minutes=62), publishers={"t": p})
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
    monkeypatch.setattr(massive, "build", lambda settings, budget, max_retries=4: fake)
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


# -- the review of 2026-10-28: each finding --------------------------------------------------------------------
def _mark(r, p, now=NOW):
    """Its limits verified already: a step reads due feeds, not a recheck."""
    r.gdb.recheck_set(p.name, {"at": now.isoformat(), "notes": []})


def test_an_unchanged_feed_is_asked_once_not_twice_as_a_redirect():
    site = Site({"https://news.example.gov/feed.xml": Resp(304)})
    r = reader(site)
    p = pub()
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/feed.xml"]
    assert r.gdb.fetched()["t-press"]["status"] == "unchanged"


def test_a_refusal_holds_every_feed_of_the_publisher_and_its_robots_txt():
    two = (Feed("t-a", "T", "A", "https://news.example.gov/a.xml", every_minutes=60),
           Feed("t-b", "T", "B", "https://news.example.gov/b.xml", every_minutes=60))
    site = Site({"https://news.example.gov/a.xml": Resp(429, headers={"Retry-After": str(24 * 3600)})})
    r = reader(site)
    p = pub(two)
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/a.xml"]  # b not asked
    site.asked.clear()
    for hours in (1, 7, 23):  # the Retry-After, a day, is longer than the 6 h of a refusal
        r.step(NOW + timedelta(hours=hours), publishers={"t": p})
    assert site.asked == [] and r.held(p, NOW + timedelta(hours=23))
    assert r.next_read(p, two[1], NOW + timedelta(hours=1)) == NOW + timedelta(hours=24)
    r.recheck(p, NOW + timedelta(hours=2))  # Recheck pressed: held, nothing sent
    assert site.asked == []
    site.answers = {}  # answering again
    r.step(NOW + timedelta(hours=24, minutes=1), publishers={"t": p})
    assert site.asked == ["https://news.example.gov/a.xml", "https://news.example.gov/b.xml"]


def test_a_retry_after_on_any_answer_holds_the_publisher():
    site = Site({"https://news.example.gov/feed.xml": Resp(503, headers={"Retry-After": "7200"})})
    r = reader(site)
    p = pub()
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    site.asked.clear()
    r.step(NOW + timedelta(minutes=30), publishers={"t": p})
    assert site.asked == []
    r.step(NOW + timedelta(hours=2, minutes=1), publishers={"t": p})
    assert site.asked == ["https://news.example.gov/feed.xml"]
    # robots.txt asking to slow down holds it too
    r2 = reader(Site(robots=Resp(429)))
    _mark(r2, p)
    r2.step(NOW, publishers={"t": p})
    assert r2.held(p, NOW + timedelta(hours=5))


def test_a_feed_that_keeps_failing_is_asked_less_and_less_often_never_above_its_cadence():
    site = Site({"https://news.example.gov/feed.xml": Resp(500)})
    r = reader(site)
    p = pub()  # every 60 min, floor 5
    _mark(r, p)
    at, waits = NOW, []
    for _ in range(5):
        r.step(at, publishers={"t": p})
        nxt = r.next_read(p, p.feeds[0], at)
        waits.append(round((nxt - at).total_seconds() / 60))
        at = nxt
    assert waits == [10, 20, 40, 60, 60]
    gone = Site({"https://news.example.gov/feed.xml": Resp(404)})
    r = reader(gone)
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert r.next_read(p, p.feeds[0], NOW) == NOW + timedelta(minutes=60)  # gone for good: its cadence


def test_a_parsers_surprise_is_noted_and_never_stops_the_publishers_after_it():
    class Odd(Resp):
        def json(self):
            return {"refRates": ["x"]}  # JSON of another shape: AttributeError deep in the parser

    nyfed = Publisher("ny", "NY", "", (Feed("ny-r", "NY", "R", "https://markets.example.gov/r.json", "nyfed"),),
                      Policy(hosts=("markets.example.gov",), min_minutes=5, per_day=40), order=1)
    site = Site({"https://markets.example.gov/r.json": Odd(headers={"Content-Type": "application/json"})})
    r = reader(site)
    p = pub()
    for x in (nyfed, p):
        _mark(r, x)
    r.step(NOW, publishers={"ny": nyfed, "t": p})
    assert r.gdb.fetched()["ny-r"]["status"] == "failed" and "https://news.example.gov/feed.xml" in site.asked
    assert r.next_read(nyfed, nyfed.feeds[0], NOW) >= NOW + timedelta(minutes=60)  # not every minute


def test_a_crawl_delay_spaces_every_request_to_the_host():
    feeds = tuple(Feed(f"t-{i}", "T", str(i), f"https://news.example.gov/{i}.xml", every_minutes=60) for i in range(3))
    site = Site(robots=Resp(text="User-agent: *\nCrawl-delay: 600\n"))
    r = reader(site)
    p = pub(feeds)
    _mark(r, p)
    wait = r.step(NOW, publishers={"t": p})
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/0.xml"] and wait == 60
    r.step(NOW + timedelta(minutes=5), publishers={"t": p})
    assert len(site.asked) == 2
    r.step(NOW + timedelta(minutes=10), publishers={"t": p})
    assert site.asked[-1] == "https://news.example.gov/1.xml" and len(site.asked) == 3


def test_a_redirect_is_followed_only_where_its_robots_txt_allows():
    site = Site({"https://news.example.gov/feed.xml": Resp(301, headers={"Location": "/private/feed.xml"})},
                robots=Resp(text="User-agent: *\nDisallow: /private/\n"))
    r = reader(site)
    p = pub()
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert "https://news.example.gov/private/feed.xml" not in site.asked
    assert "disallows" in r.gdb.fetched()["t-press"]["message"]


def test_robots_txt_is_followed_through_its_own_redirects_and_a_move_elsewhere_reads_nothing():
    class Moved(Site):
        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            self.asked.append(url)
            if url == "https://news.example.gov/robots.txt":
                return Resp(301, headers={"Location": "/r/robots.txt"})
            if url == "https://news.example.gov/r/robots.txt":
                return Resp(text="User-agent: *\nDisallow: /\n")
            return super().get(url, headers, timeout)

    site = Moved()
    r = reader(site)
    p = pub()
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert site.asked == ["https://news.example.gov/robots.txt", "https://news.example.gov/r/robots.txt"]  # disallowed
    away = Site(robots=Resp(301, headers={"Location": "https://elsewhere.example.com/robots.txt"}))
    r = reader(away)
    _mark(r, p)
    r.step(NOW, publishers={"t": p})
    assert away.asked == ["https://news.example.gov/robots.txt"] and r.held(p, NOW)


def test_a_recheck_sends_nothing_while_paused_killed_or_fetching_is_paused(store):
    from massive_collate import collector

    site = Site()
    r = reader(site, store=store)
    p = pub()
    publications.pause(store, "t", True)
    assert r.recheck(p, NOW).get("stopped") and site.asked == []
    publications.pause(store, "t", False)
    store.kv_set(collector.PAUSE_KEY, '{"at": "2026-10-26T00:00:00+00:00", "until": "9999-12-31T00:00:00+00:00"}')
    assert r.kill(p, NOW)["whose"] == "fetching"
    r.recheck(p, NOW)
    r.step(NOW, publishers={"t": p})
    assert site.asked == []


def test_an_account_kill_shows_as_stopped_with_it_not_as_paused_by_you(store):
    from massive_collate.gui.server import ChartApp

    killswitch.strike(store, "massive", "fetch", "by hand", minutes=240, shared=False)
    app = ChartApp(store)
    news = {p["name"]: p for p in app.plugins()["publishers"]}["massive-news"]
    assert news["kill"]["whose"] == "account" and news["state"] != "paused"


def test_one_publishers_failing_calendar_never_takes_the_plugins_view_down(store, monkeypatch):
    from dataclasses import replace

    from massive_collate.gui.server import ChartApp

    bad = replace(publishers()["fed"], calendar=lambda today: [{"when": "2026-11-05T08:30:00", "what": "naive"}])
    monkeypatch.setattr(publications, "_today_moments", lambda pub, now: [])
    out = publications.view(GovDB(None), None, store, publishers={"fed": bad})
    assert out[0]["calendar"] == []  # a naive time: none shown, no error
    assert gov.upcoming({"fed": bad}, NOW.date(), NOW) == []
    broken = replace(publishers()["fed"], calendar=lambda today: 1 / 0)
    out = publications.view(GovDB(None), None, store, publishers={"fed": broken, "bls": publishers()["bls"]})
    assert "ZeroDivisionError" in out[0]["error"] and out[1]["name"] == "bls"
    app = ChartApp(store)
    assert app.plugins()["plugins"]  # the providers still there


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
