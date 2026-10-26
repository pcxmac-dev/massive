"""The Gov view's sources (gov.py): the agencies' feeds read, FINRA's rule filings tagged from the
Federal Register, the New York Fed's rates, the SEC waiting for a contact, conditional and hourly
requests, and the current regulatory parameters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from massive_collate import gov
from massive_collate.gov import GovDB, Source

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)

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


class Resp:
    def __init__(self, status=200, text="", body=None, headers=None):
        self.status_code, self.text, self._body, self.headers = status, text, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class Agencies:
    """Answers each source's URL; records every request with its headers."""

    def __init__(self, etag_304=False):
        self.calls, self.etag_304 = [], etag_304

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if self.etag_304 and (headers or {}).get("If-None-Match"):
            return Resp(304)
        if "federalregister.gov" in url:
            return Resp(body=FEDREG, headers={"ETag": '"fr1"'})
        if "effr" in url:
            return Resp(body=EFFR)
        if "sofr" in url:
            return Resp(body=SOFR)
        return Resp(text=FED_RSS, headers={"ETag": '"rss1"', "Last-Modified": "Wed, 28 Oct 2026 18:00:00 GMT"})


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


def test_a_fetch_reads_every_source_once_the_sec_waits_for_a_contact():
    db, http = GovDB(None), Agencies()
    sent = gov.fetch(db, NOW, contact=None, log=lambda *_: None, session=http)
    asked = [u for u, _ in http.calls]
    assert not any("sec.gov" in u for u in asked)  # nothing sent to the SEC without a contact
    assert sent == len(gov.SOURCES) - 2 == len(asked)
    states = db.fetched()
    assert states["sec-press"]["status"] == "waiting" and "GOV_CONTACT" in states["sec-press"]["message"]
    assert all(h["User-Agent"] == gov.USER_AGENT for _, h in http.calls)
    assert db.rate("EFFR") == {"day": "2026-10-22", "value": 3.88, "lo": 3.75, "hi": 4.0}
    assert {i["source"] for i in db.items()} >= {"fed-press", "fedreg", "bls-cpi"}
    # within the hour nothing is asked again
    assert gov.fetch(db, NOW + timedelta(minutes=30), log=lambda *_: None, session=http) == 0
    # a contact set: the SEC is read too, with it in the User-Agent
    http2 = Agencies()
    gov.fetch(db, NOW + timedelta(minutes=61), contact="Pat Example pat@example.com", log=lambda *_: None, session=http2)
    sec = [h for u, h in http2.calls if "sec.gov" in u]
    assert len(sec) == 2 and all(h["User-Agent"].endswith("Pat Example pat@example.com") for h in sec)


def test_requests_are_conditional_and_an_unchanged_feed_keeps_its_items():
    db = GovDB(None)
    gov.fetch(db, NOW, log=lambda *_: None, session=Agencies())
    held = len(db.items())
    http = Agencies(etag_304=True)
    gov.fetch(db, NOW + timedelta(hours=2), log=lambda *_: None, session=http)
    fed = next(h for u, h in http.calls if u.endswith("press_all.xml"))
    assert fed["If-None-Match"] == '"rss1"' and fed["If-Modified-Since"] == "Wed, 28 Oct 2026 18:00:00 GMT"
    assert db.fetched()["fed-press"]["status"] == "unchanged" and len(db.items()) == held


def test_the_parameters_have_their_source_and_date_and_the_newest_rule_document():
    db = GovDB(None)
    gov.fetch(db, NOW, log=lambda *_: None, session=Agencies())
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
    assert all("finra.org" not in s.url for s in gov.SOURCES)  # FINRA's site is linked, never read
