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

All are official feeds made for programs, keyless. Each agency is a part of the Federal plugin
(sources/plugins/federal/: fed.py, nyfed.py, sec.py, bls.py, fedreg.py), read on its own schedule
under its own scrape checks by publications.py (the recorder's thread); this module parses what
they send and keeps it. FINRA's own website terms bar robots and use with AI programs, so nothing is read from
finra.org (publications.BLOCKED_HOSTS): its rule changes arrive through the Federal Register and
the Gov view links to finra.org. Stored in a file of its own (gov/gov.db beside the main
database), with each feed's state, each publisher's requests a day, the hosts' robots.txt and each
publisher's last limits recheck.

The current regulatory parameters (parameters) are the rates above, each with its date, and rule
parameters with the rule they come from; a rule change shows as the newest Federal Register
document for that rule.
"""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET_XML
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .sources.publisher import Feed

USER_AGENT = "massive-collate/1 (personal research)"
KEEP = 200  # items kept per source, newest first
Source = Feed  # a feed an agency publishes (sources/publisher.py)

FEDREG_AGENCIES = {  # the Federal Register's agency slugs -> the name shown
    "securities-and-exchange-commission": "SEC", "commodity-futures-trading-commission": "CFTC",
    "federal-reserve-system": "Fed", "treasury-department": "Treasury",
}
FINRA_TITLE = "Financial Industry Regulatory Authority"
WAITING = "asks automated readers to identify themselves: set GOV_CONTACT (your name and email) to read it"


FEDREG_URL = ("https://www.federalregister.gov/api/v1/documents.json?per_page=100&order=newest"
              + "".join(f"&conditions[agencies][]={a}" for a in FEDREG_AGENCIES)
              + "".join(f"&fields[]={f}" for f in ("title", "type", "publication_date", "html_url", "agencies",
                                                    "document_number")))

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
CREATE TABLE IF NOT EXISTS requests (   -- each publisher's requests a day (UTC), robots.txt included
    publisher TEXT NOT NULL,
    day       TEXT NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (publisher, day)
);
CREATE TABLE IF NOT EXISTS robots (     -- each host's robots.txt as last read (status 0: no answer)
    host      TEXT PRIMARY KEY,
    at        TEXT NOT NULL,
    status    INTEGER NOT NULL,
    body      TEXT
);
CREATE TABLE IF NOT EXISTS rechecks (   -- each publisher's last limits recheck (JSON findings)
    publisher TEXT PRIMARY KEY,
    at        TEXT NOT NULL,
    findings  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS limits_seen ( -- the rate-limit headers each feed's server last sent (JSON)
    publisher TEXT NOT NULL,
    feed      TEXT NOT NULL,
    seen      TEXT NOT NULL,
    PRIMARY KEY (publisher, feed)
);
CREATE TABLE IF NOT EXISTS holds (      -- nothing sent until: a publisher refused or asked to wait, a host's crawl-delay
    name      TEXT PRIMARY KEY,         -- a publisher's name, or host:<host>
    until     TEXT NOT NULL,
    why       TEXT
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

    # -- the publishers' requests, robots.txt and limits (publications.py) ---------------------------
    def count_request(self, publisher: str, day: str, n: int = 1) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO requests VALUES (?, ?, ?) ON CONFLICT (publisher, day) "
                              "DO UPDATE SET n = n + excluded.n", (publisher, day, n))

    def requests_on(self, publisher: str, day: str) -> int:
        r = self.conn.execute("SELECT n FROM requests WHERE publisher=? AND day=?", (publisher, day)).fetchone()
        return int(r[0]) if r else 0

    def robots_get(self, host: str) -> dict | None:
        r = self.conn.execute("SELECT at, status, body FROM robots WHERE host=?", (host,)).fetchone()
        return {"at": r[0], "status": r[1], "body": r[2]} if r else None

    def robots_set(self, host: str, at: datetime, status: int, body: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO robots VALUES (?, ?, ?, ?)", (host, at.isoformat(), status, body))

    def recheck_get(self, publisher: str) -> dict | None:
        r = self.conn.execute("SELECT findings FROM rechecks WHERE publisher=?", (publisher,)).fetchone()
        return json.loads(r[0]) if r else None

    def recheck_set(self, publisher: str, findings: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO rechecks VALUES (?, ?, ?)",
                              (publisher, findings["at"], json.dumps(findings)))

    def limits_seen(self, publisher: str, feed: str, seen: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO limits_seen VALUES (?, ?, ?)", (publisher, feed, json.dumps(seen)))

    def limits_kept(self, publisher: str) -> dict[str, dict]:
        return {r[0]: json.loads(r[1]) for r in
                self.conn.execute("SELECT feed, seen FROM limits_seen WHERE publisher=?", (publisher,))}

    def hold_get(self, name: str) -> dict | None:
        r = self.conn.execute("SELECT until, why FROM holds WHERE name=?", (name,)).fetchone()
        return {"until": r[0], "why": r[1]} if r else None

    def hold_set(self, name: str, until: datetime, why: str) -> None:
        """Hold until then, or the longer hold already kept (a later, shorter wait never cuts one short)."""
        with self.conn:
            self.conn.execute("INSERT INTO holds VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
                              "until=excluded.until, why=excluded.why WHERE excluded.until > holds.until",
                              (name, until.astimezone(timezone.utc).isoformat(timespec="seconds"), why))


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


# -- the answers --------------------------------------------------------------------------------
def _retry_after_minutes(value: str | None) -> float:
    """A Retry-After header in minutes (seconds or an HTTP date); 0 when absent or unreadable."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value) / 60)
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds() / 60)
        except (TypeError, ValueError):
            return 0.0


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


def upcoming(publishers, today, now: datetime | None = None) -> list[dict]:
    """The publishers' next scheduled releases (their calendars: the FOMC's decisions and minutes),
    soonest first: [{when, what, publisher}]; with `now`, only those still to come."""
    out = []
    for p in publishers.values():
        if not p.calendar:
            continue
        try:  # one plugin's calendar that fails (a naive time, a malformed entry) shows none, the others still do
            out += [{**c, "publisher": p.title} for c in p.calendar(today)
                    if now is None or datetime.fromisoformat(c["when"]) > now]
        except (TypeError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda c: datetime.fromisoformat(c["when"]))


def view(db: GovDB, contact_set: bool, spx_close: tuple[str, float] | None = None, publishers=None,
         today=None) -> dict:
    """Everything the Gov view shows: items newest first, parameters (with the next FOMC decision),
    each agency feed's state, the next scheduled releases, links. `publishers`: the official
    publisher plugins (sources.publishers(); their feeds are the sources)."""
    from .sources import publishers as registered

    pubs = {n: p for n, p in (publishers if publishers is not None else registered()).items() if p.kind == "official"}
    now = None if today else datetime.now(timezone.utc)  # a day given (tests): that day's whole calendar
    today = today or now.date()
    seen = db.fetched()
    sources = []
    for p in pubs.values():
        for s in p.feeds:
            f = seen.get(s.key)
            state = "waiting" if p.policy.contact and not contact_set else (f or {}).get("status") or "not asked yet"
            sources.append({"key": s.key, "agency": s.agency, "title": s.title, "state": state, "publisher": p.name,
                            "every": s.every_minutes,
                            "message": f"{s.agency} {WAITING}" if state == "waiting" else (f or {}).get("message"),
                            "at": (f or {}).get("at"), "okAt": (f or {}).get("ok_at")})
    next_ = upcoming(pubs, today, now)
    params = parameters(db, spx_close)
    fomc = next((c for c in next_ if c.get("kind") == "fomc-decision"), None)
    if fomc:
        when = datetime.fromisoformat(fomc["when"])
        params.insert(min(1, len(params)), {
            "name": "Next FOMC decision", "value": f"{when:%a} {when.day} {when:%b %Y}, 2:00 p.m. ET"
            + (" (with projections)" if fomc.get("projections") else ""),
            "source": "Federal Reserve: FOMC calendar", "link": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"})
    return {"items": db.items(), "parameters": params, "sources": sources, "upcoming": next_[:6],
            "links": [{"title": t, "url": u} for t, u in LINKS]}
