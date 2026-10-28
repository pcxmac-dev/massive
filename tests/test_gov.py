"""The Gov view's sources (gov.py, read by publications.py): the agencies' feeds read, FINRA's rule
filings tagged from the Federal Register, the New York Fed's rates, the SEC waiting for a contact,
conditional requests on each agency's schedule, and the current regulatory parameters."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from massive_collate import gov, publications
from massive_collate.gov import GovDB, Source
from massive_collate.sources import publishers

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)  # Monday, 2:00 a.m. ET: no release moment near
OFFICIAL = {n: p for n, p in publishers().items() if p.kind == "official"}
FEEDS = [f for p in OFFICIAL.values() for f in p.feeds]

FED_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Fed</title>
<item><title>Federal Reserve issues FOMC statement</title><link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm</link>
<guid>monetary20261028a</guid><pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item>
<item><title>  Agencies issue   proposal  </title><link>https://www.federalreserve.gov/x.htm</link><pubDate>Tue, 27 Oct 2026 14:30:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Speech</title>
<link href="https://example.gov/s"/><id>tag:s1</id><updated>2026-10-20T15:00:00Z</updated></entry></feed>"""

FEDREG = {"results": [
    {"title": "Self-Regulatory Organizations; Financial Industry Regulatory Authority, Inc.; Order Granting Approval To "
              "Amend FINRA Rule 4210 (Margin Requirements) To Replace the Day Trading Margin Provisions",
     "type": "Notice", "publication_date": "2026-04-17", "html_url": "https://www.federalregister.gov/d/2026-1",
     "document_number": "2026-1", "agencies": [{"slug": "securities-and-exchange-commission"}]},
    {"title": "Self-Regulatory Organizations; New York Stock Exchange LLC; Notice of Filing (Clearly Erroneous Executions)",
     "type": "Notice", "publication_date": "2026-10-23", "html_url": "https://www.federalregister.gov/d/2026-2",
     "document_number": "2026-2", "agencies": [{"slug": "securities-and-exchange-commission"}]},
    {"title": "Position Limits for Derivatives", "type": "Proposed Rule", "publication_date": "2026-10-22",
     "html_url": "https://www.federalregister.gov/d/2026-3", "document_number": "2026-3",
     "agencies": [{"slug": "commodity-futures-trading-commission"}]},
]}
EFFR = {"refRates": [{"effectiveDate": "2026-10-22", "type": "EFFR", "percentRate": 3.88, "targetRateFrom": 3.75,
                      "targetRateTo": 4.00}]}
SOFR = {"refRates": [{"effectiveDate": "2026-10-23", "type": "SOFR", "percentRate": 3.91}]}
JSON = {"Content-Type": "application/json"}


class Resp:
    def __init__(self, status=200, text="", body=None, headers=None):
        self.status_code, self.text, self._body, self.headers = status, text, body, headers or {}
        self.content = b"\xef\xbb\xbf" + text.encode("utf-8")  # the Fed's feeds start with a byte-order mark

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class Agencies:
    """Answers each feed's URL (robots.txt: none published, 404); records every request with its headers."""

    def __init__(self, etag_304=False):
        self.calls, self.etag_304 = [], etag_304

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, dict(headers or {})))
        if url.endswith("/robots.txt"):
            return Resp(404)
        if self.etag_304 and (headers or {}).get("If-None-Match"):
            return Resp(304)
        if "federalregister.gov" in url:
            return Resp(body=FEDREG, headers={"ETag": '"fr1"', **JSON})
        if "effr" in url:
            return Resp(body=EFFR, headers=JSON)
        if "sofr" in url:
            return Resp(body=SOFR, headers=JSON)
        return Resp(text=FED_RSS, headers={"ETag": '"rss1"', "Last-Modified": "Wed, 28 Oct 2026 18:00:00 GMT"})

    def feeds_asked(self):
        return [u for u, _ in self.calls if not u.endswith("/robots.txt")]


def read(db, now, http, contact=None, sleep=None, force=False):
    reader = publications.Reader(db, session=http, contact=contact, log=lambda *_: None, sleep=sleep or (lambda s: None))
    return reader.step(now, publishers=OFFICIAL, force=force), reader


def test_feeds_are_read_into_items():
    fed = Source("fed-press", "Fed", "Press releases", "u")
    items = gov.parse_rss(FED_RSS, fed)
    assert [i["title"] for i in items] == ["Federal Reserve issues FOMC statement", "Agencies issue proposal"]
    assert items[0]["id"] == "monetary20261028a" and items[0]["published"] == "2026-10-28T18:00:00+00:00"
    assert items[1]["id"] == "https://www.federalreserve.gov/x.htm" and items[0]["kind"] == "press release"
    atom = gov.parse_rss(ATOM, Source("fed-speeches", "Fed", "Speeches", "u"))
    assert atom == [{"id": "tag:s1", "agency": "Fed", "title": "Speech", "url": "https://example.gov/s",
                     "published": "2026-10-20T15:00:00+00:00", "kind": "speech"}]


def test_the_federal_register_tags_finras_and_the_exchanges_filings():
    docs = {d["id"]: d for d in gov.parse_fedreg(FEDREG)}
    assert docs["2026-1"]["agency"] == "SEC" and docs["2026-1"]["tags"] == ["FINRA"]
    assert docs["2026-2"]["tags"] == ["SRO"] and docs["2026-3"]["agency"] == "CFTC"
    assert docs["2026-3"]["kind"] == "proposed rule" and docs["2026-3"]["published"] == "2026-10-22T00:00:00+00:00"


def test_a_pass_reads_every_feed_once_the_sec_waits_for_a_contact():
    db, http = GovDB(None), Agencies()
    read(db, NOW, http)
    assert not any("sec.gov" in u for u, _ in http.calls)  # nothing sent to the SEC without a contact, robots.txt neither
    assert sorted(http.feeds_asked()) == sorted(f.url for f in FEEDS if "sec.gov" not in f.url)
    states = db.fetched()
    assert states["sec-press"]["status"] == "waiting" and "GOV_CONTACT" in states["sec-press"]["message"]
    assert all(h["User-Agent"] == gov.USER_AGENT for _, h in http.calls)
    assert db.rate("EFFR") == {"day": "2026-10-22", "value": 3.88, "lo": 3.75, "hi": 4.0}
    assert {i["source"] for i in db.items()} >= {"fed-press", "fedreg", "bls-cpi"}
    # within each feed's time nothing is asked again
    http.calls.clear()
    read(db, NOW + timedelta(minutes=20), http)
    assert http.calls == []
    # a contact set: the SEC is read too, with it in the User-Agent; the agencies that do not ask
    # for one never get it
    http2 = Agencies()
    read(db, NOW + timedelta(minutes=61), http2, contact="Pat Example pat@example.com")
    sec = [h for u, h in http2.calls if "sec.gov" in u and not u.endswith("robots.txt")]
    assert len(sec) == 2 and all(h["User-Agent"].endswith("Pat Example pat@example.com") for h in sec)
    assert all(h["User-Agent"] == gov.USER_AGENT for u, h in http2.calls if "sec.gov" not in u)


def test_no_redirect_is_followed_by_the_client():
    class Recording(Agencies):
        def __init__(self):
            super().__init__()
            self.follows = set()

        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            self.follows.add(allow_redirects)
            return super().get(url, headers, timeout)

    http = Recording()
    read(GovDB(None), NOW, http, contact="Pat Example pat@example.com")
    assert http.follows == {False}  # the reader follows a redirect itself, only to the publisher's own hosts


def test_requests_are_conditional_and_an_unchanged_feed_keeps_its_items():
    db = GovDB(None)
    read(db, NOW, Agencies())
    held = len(db.items())
    http = Agencies(etag_304=True)
    read(db, NOW + timedelta(hours=2), http)
    fed = next(h for u, h in http.calls if u.endswith("press_all.xml"))
    assert fed["If-None-Match"] == '"rss1"' and fed["If-Modified-Since"] == "Wed, 28 Oct 2026 18:00:00 GMT"
    assert db.fetched()["fed-press"]["status"] == "unchanged" and len(db.items()) == held


def test_the_parameters_have_their_source_and_date_and_the_newest_rule_document():
    db = GovDB(None)
    read(db, NOW, Agencies())
    params = {p["name"]: p for p in gov.parameters(db, spx_close=("2026-10-23", 6000.0))}
    assert params["Federal funds target range"]["value"] == "3.75% – 4.00%"
    assert params["Federal funds target range"]["asOf"] == "2026-10-22" and params["SOFR"]["value"] == "3.91%"
    cb = params["Market-wide circuit breakers"]
    assert [l["at"] for l in cb["levels"]] == [5580.0, 5220.0, 4800.0] and cb["levelsFrom"]["day"] == "2026-10-23"
    assert params["Day trading margin"]["latest"]["url"] == "https://www.federalregister.gov/d/2026-1"
    assert "levels" not in {p["name"]: p for p in gov.parameters(db)}["Market-wide circuit breakers"]


def test_the_gov_view_says_which_sources_wait_and_why(store):
    from massive_collate.gui.server import ChartApp

    app = ChartApp(store)
    out = app.gov()
    states = {s["key"]: s for s in out["sources"]}
    assert states["sec-press"]["state"] == "waiting" and "GOV_CONTACT" in states["sec-press"]["message"]
    assert states["fed-press"]["state"] == "not asked yet" and out["items"] == []
    assert any(p["name"] == "Settlement cycle" for p in out["parameters"])
    assert all("finra.org" not in f.url for f in FEEDS)  # FINRA's site is linked, never read


def test_the_gov_view_shows_the_next_fomc_decision_and_the_releases_scheduled():
    out = gov.view(GovDB(None), contact_set=False, publishers=OFFICIAL, today=date(2026, 10, 29))
    nxt = {p["name"]: p for p in out["parameters"]}["Next FOMC decision"]
    assert nxt["value"] == "Wed 9 Dec 2026, 2:00 p.m. ET (with projections)" and "fomccalendars" in nxt["link"]
    assert [c["what"] for c in out["upcoming"][:2]] == ["FOMC minutes", "FOMC decision with projections"]  # Nov 18, Dec 9


def test_the_gov_command_lists_each_sources_state(tmp_path, monkeypatch, capsys):
    from massive_collate import cli

    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    asked = []
    monkeypatch.setattr(cli, "_fetch_gov", lambda settings, log, force=False: asked.append(force))  # no real requests
    assert cli.main(["gov"]) == 0
    out = capsys.readouterr().out
    assert asked == [False] and "Press releases" in out and "waiting  (SEC asks automated readers" in out
    assert cli.main(["gov", "--all"]) == 0 and asked[-1] is True  # every feed its floor allows
    monkeypatch.setenv("MASSIVE_OFFLINE", "1")
    asked.clear()
    assert cli.main(["gov"]) == 0 and not asked and "nothing is fetched" in capsys.readouterr().out


def test_a_feed_with_a_byte_order_mark_and_no_charset_is_read():
    data = b"\xef\xbb\xbf" + FED_RSS.replace("Agencies", "Agencías").encode("utf-8")
    items = gov.parse_rss(data, Source("fed-press", "Fed", "Press releases", "u"))
    assert [i["title"] for i in items][1] == "Agencías issue proposal"  # not mojibake from Latin-1


def test_a_dropped_connection_is_tried_once_more_and_a_failure_again_in_ten_minutes():
    import requests

    class Flaky(Agencies):
        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            if "bls.gov" in url and not url.endswith("robots.txt"):
                self.calls.append((url, dict(headers or {})))
                raise requests.ConnectionError("dropped")
            if "effr" in url and not any("effr" in u for u, _ in self.calls):
                self.calls.append((url, dict(headers or {})))
                raise requests.ConnectionError("dropped once")
            return super().get(url, headers, timeout)

    db, http, waits = GovDB(None), Flaky(), []
    _, reader = read(db, NOW, http, sleep=waits.append)
    states = db.fetched()
    assert states["nyfed-effr"]["status"] == "ok" and db.rate("EFFR")  # the second try answered
    assert states["bls-cpi"]["status"] == "failed" and sum("cpi.rss" in u for u, _ in http.calls) == 2
    assert waits and all(w == publications.RETRY_WAIT_S for w in waits)
    due = {f.key for p in OFFICIAL.values() for f in reader.due(p, NOW + timedelta(minutes=11))}
    assert due == {k for k, f in states.items() if f["status"] == "failed"}  # only the failed ones, after ten minutes


def test_a_refusal_is_not_asked_again_for_hours():
    class Refusing(Agencies):
        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            if url.endswith("robots.txt"):
                return super().get(url, headers, timeout)
            if "bls.gov" in url:
                self.calls.append((url, dict(headers or {})))
                return Resp(403)
            if "federalregister" in url:
                self.calls.append((url, dict(headers or {})))
                return Resp(429, headers={"Retry-After": str(12 * 3600)})
            return super().get(url, headers, timeout)

    db = GovDB(None)
    site = Refusing()
    _, reader = read(db, NOW, site)
    states = db.fetched()
    bls = [k for k in states if k.startswith("bls-")]
    assert [states[k]["status"] for k in bls] == ["refused"] and states["fedreg"]["status"] == "refused"
    # the refusal holds the whole publisher: its other feeds were not asked in the same pass
    assert sum("bls.gov" in u and not u.endswith("robots.txt") for u, _ in site.calls) == 1
    assert db.hold_get("bls")["why"].endswith("HTTP 403")

    def due(at):
        return {f.key for p in OFFICIAL.values() for f in reader.due(p, at)}
    later = due(NOW + timedelta(hours=2))
    assert not any(k.startswith("bls-") for k in later) and "fed-press" in later  # hours for a refusal
    assert "bls-cpi" in due(NOW + timedelta(hours=7))
    assert "fedreg" not in due(NOW + timedelta(hours=7))  # its Retry-After: 12 h
