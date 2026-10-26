"""Government and regulators: what they publish that moves or governs the markets.

- The Federal Reserve: press releases (monetary policy, regulation, enforcement) and speeches and
  testimony, from its RSS feeds.
- The SEC: press releases and trading suspensions, from its RSS feeds. Its fair-access policy
  turns away unidentified automated readers, so these wait for GOV_CONTACT (your name and email,
  sent in the User-Agent to the agencies that ask for one; nothing is sent until you set it).
- BLS: the market-moving releases (employment situation, CPI, PPI, job openings), from its feeds.
- The Federal Register: rules, proposed rules and notices of the SEC (with FINRA's and the
  exchanges' rule filings, tagged), the CFTC (its own feeds are off as of October 2026), the Fed
  and the Treasury, from its documented JSON API.
- The New York Fed: the federal funds target range, the effective rate and SOFR, from its
  markets API.

All are official feeds made for programs, keyless; each is asked at most every REFRESH_MINUTES,
with conditional requests, by the sync's closing pass (cli.after_sync). FINRA's own website terms
bar robots and use with AI programs, so nothing is read from finra.org: its rule changes arrive
through the Federal Register and the Gov view links to finra.org. Stored in a file of its own
(gov/gov.db beside the main database).

The current regulatory parameters (parameters) are the rates above, each with its date, and rule
parameters with the rule they come from; a rule change shows as the newest Federal Register
document for that rule.
"""
from __future__ import annotations

import json
import sqlite3
import time
import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

USER_AGENT = "massive-collate/1 (personal research)"
REFRESH_MINUTES = 60  # each source at most this often
FAILED_RETRY_MINUTES = 10  # a source that failed (no answer, an error) is asked again sooner
RETRY_WAIT_S = 2.0  # a dropped connection is tried once more after this
KEEP = 200  # items kept per source, newest first
TIMEOUT_S = 20

FEDREG_AGENCIES = {  # the Federal Register's agency slugs -> the name shown
    "securities-and-exchange-commission": "SEC", "commodity-futures-trading-commission": "CFTC",
    "federal-reserve-system": "Fed", "treasury-department": "Treasury",
}
FINRA_TITLE = "Financial Industry Regulatory Authority"
WAITING = "asks automated readers to identify themselves: set GOV_CONTACT (your name and email) to read it"


@dataclass(frozen=True)
class Source:
    key: str
    agency: str
    title: str
    url: str
    kind: str = "rss"  # rss | fedreg | nyfed
    contact: bool = False  # the agency asks automated readers to identify themselves (GOV_CONTACT)


FEDREG_URL = ("https://www.federalregister.gov/api/v1/documents.json?per_page=100&order=newest"
              + "".join(f"&conditions[agencies][]={a}" for a in FEDREG_AGENCIES)
              + "".join(f"&fields[]={f}" for f in ("title", "type", "publication_date", "html_url", "agencies",
                                                    "document_number")))
SOURCES = (
    Source("fed-press", "Fed", "Press releases", "https://www.federalreserve.gov/feeds/press_all.xml"),
    Source("fed-speeches", "Fed", "Speeches and testimony",
           "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"),
    Source("sec-press", "SEC", "Press releases", "https://www.sec.gov/news/pressreleases.rss", contact=True),
    Source("sec-suspensions", "SEC", "Trading suspensions",
           "https://www.sec.gov/enforcement-litigation/trading-suspensions/rss", contact=True),
    Source("bls-empsit", "BLS", "Employment Situation", "https://www.bls.gov/feed/empsit.rss"),
    Source("bls-cpi", "BLS", "Consumer Price Index", "https://www.bls.gov/feed/cpi.rss"),
    Source("bls-ppi", "BLS", "Producer Price Index", "https://www.bls.gov/feed/ppi.rss"),
    Source("bls-jolts", "BLS", "Job openings and labor turnover", "https://www.bls.gov/feed/jolts.rss"),
    Source("fedreg", "Federal Register", "Rules and notices: SEC (FINRA, exchanges), CFTC, Fed, Treasury",
           FEDREG_URL, "fedreg"),
    Source("nyfed-effr", "New York Fed", "Effective federal funds rate and target range",
           "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json", "nyfed"),
    Source("nyfed-sofr", "New York Fed", "SOFR", "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json",
           "nyfed"),
)

LINKS = (  # read by people, never fetched here
    ("FINRA rules", "https://www.finra.org/rules-guidance/rulebooks/finra-rules"),
    ("FINRA Rule 4210 (margin)", "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210"),
    ("SEC rulemaking", "https://www.sec.gov/rules-regulations"),
    ("FOMC calendar", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("BLS release calendar", "https://www.bls.gov/schedule/news_release/"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source    TEXT NOT NULL,
    id        TEXT NOT NULL,
    agency    TEXT NOT NULL,
    title     TEXT NOT NULL,
    url       TEXT,
    published TEXT,            -- ISO time (UTC) the agency published it
    kind      TEXT,            -- release, speech, rule, proposed rule, notice ...
    tags      TEXT,            -- JSON list (FINRA, SRO ...)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS items_published ON items (published);
CREATE TABLE IF NOT EXISTS rates (
    series    TEXT NOT NULL,   -- EFFR, SOFR
    day       TEXT NOT NULL,
    value     REAL,
    lo        REAL,            -- the target range, for the federal funds rate
    hi        REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (series, day)
);
CREATE TABLE IF NOT EXISTS fetches (
    source    TEXT PRIMARY KEY,
    at        TEXT NOT NULL,   -- last asked (or skipped)
    status    TEXT NOT NULL,   -- ok | unchanged | waiting | failed
    message   TEXT,
    etag      TEXT,
    modified  TEXT,
    ok_at     TEXT             -- last answered
);
"""


def db_path(main_db: Path | str) -> Path | None:
    """gov/gov.db beside the main database (None for an in-memory one)."""
    p = Path(main_db)
    return None if str(main_db) == ":memory:" else p.parent / "gov" / "gov.db"


class GovDB:
    """The file of what the agencies published (gov/gov.db)."""

    def __init__(self, path: Path | None):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def fetched(self) -> dict[str, dict]:
        cols = ("source", "at", "status", "message", "etag", "modified", "ok_at")
        return {r[0]: dict(zip(cols, r)) for r in self.conn.execute(f"SELECT {', '.join(cols)} FROM fetches")}

    def note(self, source: str, at: datetime, status: str, message: str | None = None,
             etag: str | None = None, modified: str | None = None) -> None:
        prev = self.fetched().get(source, {})
        ok_at = at.isoformat() if status in ("ok", "unchanged") else prev.get("ok_at")
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO fetches VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (source, at.isoformat(), status, message, etag if etag is not None else prev.get("etag"),
                               modified if modified is not None else prev.get("modified"), ok_at))

    def save_items(self, source: str, items: list[dict], now: datetime) -> int:
        """New items (ones already held keep their row). Returns how many were new."""
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT OR IGNORE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(source, it["id"], it["agency"], it["title"], it.get("url"), it.get("published"), it.get("kind"),
                  json.dumps(it.get("tags") or []), now.isoformat()) for it in items])
            new = self.conn.total_changes - before
            self.conn.execute(  # the newest KEEP of the source
                "DELETE FROM items WHERE source=? AND id NOT IN (SELECT id FROM items WHERE source=? "
                "ORDER BY published DESC LIMIT ?)", (source, source, KEEP))
        return new

    def save_rate(self, series: str, day: str, value: float | None, lo: float | None, hi: float | None,
                  now: datetime) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO rates VALUES (?, ?, ?, ?, ?, ?)",
                              (series, day, value, lo, hi, now.isoformat()))

    def items(self, limit: int = 400) -> list[dict]:
        cols = ("source", "id", "agency", "title", "url", "published", "kind", "tags")
        out = []
        for r in self.conn.execute(f"SELECT {', '.join(cols)} FROM items ORDER BY published DESC LIMIT ?", (limit,)):
            row = dict(zip(cols, r))
            row["tags"] = json.loads(row["tags"] or "[]")
            out.append(row)
        return out

    def rate(self, series: str) -> dict | None:
        r = self.conn.execute("SELECT day, value, lo, hi FROM rates WHERE series=? ORDER BY day DESC LIMIT 1",
                              (series,)).fetchone()
        return {"day": r[0], "value": r[1], "lo": r[2], "hi": r[3]} if r else None


# -- reading the feeds -------------------------------------------------------------------------
def _iso(text: str | None) -> str | None:
    if not text:
        return None
    try:
        t = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        try:
            t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_rss(data: bytes | str, source: Source) -> list[dict]:
    """An RSS 2.0 (or Atom) feed's items: id (guid or link), title, link, when published. Read
    from the bytes, so the XML's own declaration (and a byte-order mark) sets the encoding: the
    Fed's feeds start with one and their HTTP header names no charset (decoded as Latin-1, the
    first character was unreadable)."""
    root = ET_XML.fromstring(data)
    out = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.rsplit("}", 1)[-1]: c for c in it}
        title = (get["title"].text or "").strip() if "title" in get else ""
        link = get.get("link")
        url = ((link.text or "").strip() or link.get("href")) if link is not None else None
        guid = (get["guid"].text or "").strip() if "guid" in get else (get["id"].text or "").strip() if "id" in get else ""
        when = next((get[k].text for k in ("pubDate", "published", "updated", "date") if k in get), None)
        if title:
            out.append({"id": guid or url or title, "agency": source.agency, "title": " ".join(title.split()),
                        "url": url, "published": _iso(when), "kind": _kind(source)})
    return out


def _kind(source: Source) -> str:
    return {"fed-speeches": "speech", "sec-suspensions": "trading suspension"}.get(
        source.key, "data release" if source.agency == "BLS" else "press release")


def parse_fedreg(data: dict) -> list[dict]:
    """The Federal Register's documents: the agency shown (SEC, CFTC, Fed, Treasury), the
    document type, and FINRA's and the exchanges' rule filings tagged."""
    out = []
    for d in data.get("results") or []:
        slugs = [a.get("slug") for a in d.get("agencies") or [] if a.get("slug")]
        agency = next((FEDREG_AGENCIES[s] for s in slugs if s in FEDREG_AGENCIES), "Federal Register")
        title = " ".join((d.get("title") or "").split())
        tags = []
        if FINRA_TITLE in title:
            tags.append("FINRA")
        elif title.startswith("Self-Regulatory Organizations"):
            tags.append("SRO")
        day = d.get("publication_date")
        out.append({"id": d.get("document_number") or d.get("html_url") or title, "agency": agency, "title": title,
                    "url": d.get("html_url"), "published": f"{day}T00:00:00+00:00" if day else None,
                    "kind": (d.get("type") or "document").lower(), "tags": tags})
    return out


def parse_nyfed(data: dict) -> dict | None:
    """The New York Fed's newest reference rate: {series, day, value, lo, hi} (lo/hi: the
    federal funds target range, on EFFR only)."""
    rows = data.get("refRates") or []
    if not rows:
        return None
    r = rows[0]
    return {"series": r.get("type"), "day": r.get("effectiveDate"), "value": r.get("percentRate"),
            "lo": r.get("targetRateFrom"), "hi": r.get("targetRateTo")}


# -- fetching ----------------------------------------------------------------------------------
def due(db: GovDB, now: datetime, minutes: int = REFRESH_MINUTES) -> list[Source]:
    """The sources not asked within `minutes` (FAILED_RETRY_MINUTES for one whose last try failed)."""
    seen = db.fetched()

    def wait(f: dict) -> timedelta:
        return timedelta(minutes=min(minutes, FAILED_RETRY_MINUTES) if f["status"] == "failed" else minutes)
    return [s for s in SOURCES if s.key not in seen or now - datetime.fromisoformat(seen[s.key]["at"]) >= wait(seen[s.key])]


def fetch(db: GovDB, now: datetime, contact: str | None = None, log: Callable[[str], None] = print,
          session: requests.Session | None = None, minutes: int = REFRESH_MINUTES,
          sleep: Callable[[float], None] = time.sleep) -> int:
    """Ask every due source once (conditional requests: an unchanged feed sends nothing back).
    Sources that want a contact wait without one. Returns requests sent."""
    session = session or requests.Session()
    seen, sent, new = db.fetched(), 0, 0
    for s in due(db, now, minutes):
        if s.contact and not contact:
            db.note(s.key, now, "waiting", f"{s.agency} {WAITING}")
            continue
        headers = {"User-Agent": f"{USER_AGENT} {contact}" if contact else USER_AGENT,
                   "Accept-Encoding": "gzip, deflate"}
        prev = seen.get(s.key, {})
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("modified"):
            headers["If-Modified-Since"] = prev["modified"]
        r = None
        for attempt in (1, 2):  # a dropped connection: once more
            sent += 1
            try:
                r = session.get(s.url, headers=headers, timeout=TIMEOUT_S)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                error = e
                if attempt == 1:
                    sleep(RETRY_WAIT_S)
            except requests.RequestException as e:
                error = e
                break
        if r is None:
            db.note(s.key, now, "failed", f"no answer: {type(error).__name__}")
            continue
        if r.status_code == 304:
            db.note(s.key, now, "unchanged")
            continue
        if r.status_code != 200:
            db.note(s.key, now, "failed", f"HTTP {r.status_code}")
            log(f"gov: {s.agency} {s.title}: HTTP {r.status_code}")
            continue
        try:
            if s.kind == "nyfed":
                rate = parse_nyfed(r.json())
                if rate and rate["day"]:
                    db.save_rate(rate["series"], rate["day"], rate["value"], rate["lo"], rate["hi"], now)
                n = 0
            else:
                items = parse_fedreg(r.json()) if s.kind == "fedreg" else parse_rss(r.content, s)
                n = db.save_items(s.key, items, now)
        except (ValueError, ET_XML.ParseError) as e:
            db.note(s.key, now, "failed", f"unreadable answer: {e}"[:200])
            continue
        new += n
        db.note(s.key, now, "ok", f"{n} new" if s.kind != "nyfed" else None,
                etag=r.headers.get("ETag") or "", modified=r.headers.get("Last-Modified") or "")
    if sent:
        log(f"gov: {sent} requests, {new} new items")
    return sent


# -- what the Gov view shows -------------------------------------------------------------------
RULES = (  # rule parameters: (name, value, rule, link, the Federal Register words that would change it)
    ("Settlement cycle", "T+1", "SEC Rule 15c6-1, since 28 May 2024",
     "https://www.ecfr.gov/current/title-17/section-240.15c6-1", "15c6-1"),
    ("Initial margin (Reg T)", "50% of the purchase price", "Regulation T, 12 CFR 220.12",
     "https://www.ecfr.gov/current/title-12/section-220.12", "Regulation T"),
    ("Maintenance margin, long", "25% of market value", "FINRA Rule 4210(c)",
     "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210", "Rule 4210"),
    ("Day trading margin", "the pattern day trader provisions are being replaced by intraday margin standards "
     "(SEC approval 17 Apr 2026; in force from the date FINRA sets)", "FINRA Rule 4210(f)(8)",
     "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210", "Day Trading"),
    ("Market-wide circuit breakers", "S&P 500 down 7% / 13% / 20% from the prior close",
     "Exchange rules (NYSE Rule 7.12 and its equivalents): levels 1 and 2 halt trading for 15 minutes "
     "(before 3:25 p.m. ET), level 3 for the rest of the day", None, "Extraordinary Market Volatility"),
)


def parameters(db: GovDB, spx_close: tuple[str, float] | None = None) -> list[dict]:
    """The current regulatory parameters: the policy rates (from the New York Fed, with their
    date), the circuit breakers' levels when the S&P 500's prior close is held (derived), and the
    rule parameters, each with the newest Federal Register document that names its rule."""
    out = []
    effr, sofr = db.rate("EFFR"), db.rate("SOFR")
    if effr:
        if effr["lo"] is not None and effr["hi"] is not None:
            out.append({"name": "Federal funds target range", "value": f"{effr['lo']:.2f}% – {effr['hi']:.2f}%",
                        "source": "New York Fed (FOMC)", "asOf": effr["day"]})
        if effr["value"] is not None:
            out.append({"name": "Effective federal funds rate", "value": f"{effr['value']:.2f}%",
                        "source": "New York Fed", "asOf": effr["day"]})
    if sofr and sofr["value"] is not None:
        out.append({"name": "SOFR", "value": f"{sofr['value']:.2f}%", "source": "New York Fed", "asOf": sofr["day"]})
    items = db.items(2000)
    for name, value, rule, link, words in RULES:
        row = {"name": name, "value": value, "source": rule, "link": link}
        if name.startswith("Market-wide") and spx_close:
            day, close = spx_close
            row["levels"] = [{"level": i + 1, "down": p, "at": round(close * (1 - p / 100), 2)}
                             for i, p in enumerate((7, 13, 20))]
            row["levelsFrom"] = {"day": day, "close": close}
        newest = next((it for it in items if it["source"] == "fedreg" and words.lower() in it["title"].lower()), None)
        if newest:
            row["latest"] = {k: newest[k] for k in ("title", "url", "published", "kind")}
        out.append(row)
    return out


def view(db: GovDB, contact_set: bool, spx_close: tuple[str, float] | None = None) -> dict:
    """Everything the Gov view shows: items newest first, parameters, each source's state, links."""
    seen = db.fetched()
    sources = []
    for s in SOURCES:
        f = seen.get(s.key)
        state = "waiting" if s.contact and not contact_set else (f or {}).get("status") or "not asked yet"
        sources.append({"key": s.key, "agency": s.agency, "title": s.title, "state": state,
                        "message": f"{s.agency} {WAITING}" if state == "waiting" else (f or {}).get("message"),
                        "at": (f or {}).get("at"), "okAt": (f or {}).get("ok_at")})
    return {"items": db.items(), "parameters": parameters(db, spx_close), "sources": sources,
            "links": [{"title": t, "url": u} for t, u in LINKS], "refreshMinutes": REFRESH_MINUTES}
