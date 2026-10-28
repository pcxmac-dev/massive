"""Publications: the publisher plugins' feeds (sources/publisher.py) read on each publisher's own
schedule, under its scrape checks, never behind a sync.

The recorder reads them in a thread of its own (live.Recorder: it is always on); the hourly sync
does when no recorder runs. One reader at a time on the machine (LOCK beside the database). Their
state, items and rates are kept in gov/gov.db (gov.GovDB); a vendor's headlines in the main
database's news (the News view and the globe's ticker).

When a feed is read: its cadence (`Feed.every_minutes`), and the publisher's release moments (the
FOMC's statement at 2:00 p.m. ET on decision days), never sooner than its floor (the publisher's
`min_minutes`, its hosts' robots.txt crawl-delay or request-rate, whichever is longest). A feed
that failed is asked again after FAILED_RETRY_MINUTES; one refused (401, 403, 429, robots.txt)
after REFUSED_MINUTES, or the Retry-After when longer.

The scrape checks, before every request (Policy, sources/publisher.py): HTTPS to the publisher's
own hosts only and never a blocked site (BLOCKED_HOSTS: the sites whose terms bar programs); a
redirect to another host not followed (and none at all where the contact is sent); the host's
robots.txt, read weekly (RFC 9309: a 4xx answer allows all, no answer or a 5xx none); conditional
requests; an answer that is not a feed (XML or JSON) refused, never read as a page; at most the
publisher's requests a day (`per_day`, robots.txt included).

Its limits are verified again (recheck) every RECHECK_DAYS and from the Plugins view: robots.txt
read again, each feed asked once with the rate-limit headers its server sends kept; a crawl-delay
or request-rate tighter than the plugin's own becomes its floor.
"""
from __future__ import annotations

import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from . import gov, killswitch
from .sources.publisher import Feed, Publisher

USER_AGENT = gov.USER_AGENT
# Sites whose terms bar programs or AI use (the owner's list): never read by any publisher, whatever
# its plugin names. A host matches itself and its subdomains.
BLOCKED_HOSTS = ("finra.org", "nasdaq.com", "yahoo.com", "stooq.com", "stooq.pl", "binance.com", "binance.us")
FAILED_RETRY_MINUTES = 10  # a feed that failed (no answer, an error) is asked again sooner
REFUSED_MINUTES = 6 * 60  # one refused (401, 403, 429, robots.txt) waits this long, or its Retry-After if longer
RETRY_WAIT_S = 2.0  # a dropped connection is tried once more after this
TIMEOUT_S = 20
ROBOTS_DAYS = 7  # robots.txt read again after this (the recheck reads it at once)
ROBOTS_RETRY_MINUTES = 10  # robots.txt that did not answer: nothing read from the host until it does
RECHECK_DAYS = 7  # each publisher's limits verified again this often
FEED_TYPES = ("xml", "rss", "atom", "json")
LIMIT_HEADERS = ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "RateLimit-Policy",
                 "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")
LOCK = "publications.lock"
STEP_MAX_S = 60.0  # the recorder's thread looks again at least this often
PAUSE_DAYS = 3650  # a pause by hand: until the user resumes it


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def blocked(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def _header(r, name: str) -> str | None:
    """A response header by name, whatever its case (a plain dict in tests)."""
    return next((v for k, v in (getattr(r, "headers", None) or {}).items() if k.lower() == name.lower()), None)


def _is_feed(r) -> bool:
    """Whether an answer is a feed (XML, RSS, Atom, JSON) and not a web page: by its Content-Type,
    or, without one, by how it starts."""
    ctype = (_header(r, "Content-Type") or "").lower()
    if "html" in ctype:
        return False
    if any(t in ctype for t in FEED_TYPES):
        return True
    head = (getattr(r, "content", b"") or b"")[:200].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<?xml", b"<rss", b"<feed", b"{", b"["))


def retry_after_minutes(value: str | None) -> float:
    """A Retry-After header in minutes (seconds or an HTTP date); 0 when absent or unreadable."""
    return gov._retry_after_minutes(value)


@dataclass
class Robots:
    """A host's robots.txt as last read: status (HTTP, 0 for no answer), when, its rules."""
    host: str
    status: int
    at: datetime
    body: str = ""

    def _parser(self) -> urllib.robotparser.RobotFileParser:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(self.body.splitlines())
        return rp

    @property
    def answered(self) -> bool:
        """Read, or unavailable (a 4xx); a 429 is the host asking to slow down: not an answer."""
        return self.status == 200 or (400 <= self.status < 500 and self.status != 429)

    def allows(self, url: str) -> bool:
        """RFC 9309: rules when read (200); a 4xx (none published, or refused) allows all; no
        answer, a 5xx or a 429 allows nothing until it answers."""
        if self.status == 200:
            return self._parser().can_fetch(USER_AGENT.split("/")[0], url)
        return self.answered

    def delay_minutes(self) -> float:
        """The pause it asks between requests (crawl-delay, or request-rate), in minutes; 0 for none."""
        if self.status != 200:
            return 0.0
        rp, agent = self._parser(), USER_AGENT.split("/")[0]
        delay = float(rp.crawl_delay(agent) or 0)
        rate = rp.request_rate(agent)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        return delay / 60

    def fresh(self, now: datetime) -> bool:
        return now - self.at < (timedelta(days=ROBOTS_DAYS) if self.answered else timedelta(minutes=ROBOTS_RETRY_MINUTES))


@dataclass
class Context:
    """What a vendor's read gets (Publisher.read): its feed and publisher, the settings, the main
    store, the time, the log, and `count(n)` for its requests."""
    publisher: Publisher
    feed: Feed
    settings: object
    store: object
    now: datetime
    log: Callable[[str], None]
    count: Callable[[int], None]


class Reader:
    """Reads the publishers' due feeds under their checks. `session` answers get() (requests's);
    `store` is the main database (kills, a vendor's headlines); `settings` the contact and keys."""

    def __init__(self, gdb: gov.GovDB, settings=None, store=None, session=None, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, contact: str | None = None):
        self.gdb, self.settings, self.store, self.log, self.sleep = gdb, settings, store, log, sleep
        self.session = session or requests.Session()
        self.contact = contact if contact is not None else getattr(settings, "gov_contact", None)

    # -- set up, paused, its limits -------------------------------------------------------------------
    def set_up(self, pub: Publisher) -> bool:
        """Whether it can read here: an official publisher always; a vendor's only with its feed's keys."""
        if getattr(self.settings, "offline", False):
            return False
        if pub.kind != "vendor":
            return True
        from .sources import providers

        account = providers().get(pub.account)
        return bool(account and self.settings is not None and account.keys_set(self.settings) is not False)

    def kill(self, pub: Publisher, now: datetime) -> dict | None:
        """A pause by hand, or a kill (its own, or its account's: a vendor's feed refused)."""
        if self.store is None:
            return None
        return killswitch.killed(self.store, pub.name, "fetch", now) or (
            killswitch.killed(self.store, pub.account, "fetch", now) if pub.account else None)

    def floor_minutes(self, pub: Publisher, now: datetime) -> float:
        """The least time between two reads of one of its feeds: its own, or its hosts' robots.txt
        when they ask for longer."""
        out = pub.policy.min_minutes
        for host in pub.policy.hosts:
            rb = self._robots_kept(host)
            if rb is not None:
                out = max(out, rb.delay_minutes())
        return out

    def requests_today(self, pub: Publisher, now: datetime) -> int:
        return self.gdb.requests_on(pub.name, now.astimezone(timezone.utc).date().isoformat())

    def _count(self, pub: Publisher, now: datetime, n: int = 1) -> None:
        self.gdb.count_request(pub.name, now.astimezone(timezone.utc).date().isoformat(), n)

    # -- when -------------------------------------------------------------------------------------------
    def next_read(self, pub: Publisher, feed: Feed, now: datetime, row: dict | None = None) -> datetime:
        """When a feed is next due (now or before: due now)."""
        row = row if row is not None else self.gdb.fetched().get(feed.key)
        if not row:
            return now
        last = datetime.fromisoformat(row["at"])
        status, floor = row["status"], timedelta(minutes=self.floor_minutes(pub, now))
        every = timedelta(minutes=feed.every_minutes)
        if status == "waiting":  # the contact it waited for is set now: at once
            return now if self.contact else last + every
        if status == "refused":
            held = re.search(r"wait (\d+) min", row.get("message") or "")
            return last + timedelta(minutes=max(REFUSED_MINUTES, float(held.group(1)) if held else 0))
        if status == "failed":
            return last + max(floor, min(every, timedelta(minutes=FAILED_RETRY_MINUTES)))
        out = last + max(every, floor)
        if pub.moments and feed.moments:
            moment = next_moment(pub, last, out)
            if moment is not None:
                out = min(out, max(moment, last + floor))
        return out

    def due(self, pub: Publisher, now: datetime) -> list[Feed]:
        seen = self.gdb.fetched()
        return [f for f in pub.feeds if self.next_read(pub, f, now, seen.get(f.key)) <= now]

    # -- reading ----------------------------------------------------------------------------------------
    def step(self, now: datetime, publishers: dict[str, Publisher] | None = None, force: bool = False) -> float:
        """Read every set-up, unpaused publisher's due feeds (`force`: every feed, still under its
        floor, cap and refusals); its limits verified first when due. Returns the seconds until the
        next feed is due (at most STEP_MAX_S)."""
        from .sources import publishers as registered

        pubs = publishers if publishers is not None else registered()
        soonest = now + timedelta(seconds=STEP_MAX_S)
        for pub in pubs.values():
            if not self.set_up(pub) or self.kill(pub, now):
                continue
            if pub.policy.contact and not self.contact:  # nothing at all goes to it, robots.txt neither
                seen = self.gdb.fetched()
                for f in pub.feeds:
                    if (seen.get(f.key) or {}).get("status") != "waiting":
                        self.gdb.note(f.key, now, "waiting", f"{f.agency} {gov.WAITING}")
                continue
            last_check = self.gdb.recheck_get(pub.name)
            if last_check is None or now - datetime.fromisoformat(last_check["at"]) >= timedelta(days=RECHECK_DAYS):
                self.recheck(pub, now)
                continue  # the recheck read each feed once
            seen = self.gdb.fetched()
            for feed in pub.feeds:
                row = seen.get(feed.key)
                nxt = self.next_read(pub, feed, now, row)
                if force and row and row["status"] not in ("refused",):
                    nxt = min(nxt, max(now, datetime.fromisoformat(row["at"]) + timedelta(minutes=self.floor_minutes(pub, now))))
                if nxt <= now:
                    self.read(pub, feed, now)
                    nxt = self.next_read(pub, feed, now)
                soonest = min(soonest, nxt)
        return max(1.0, min(STEP_MAX_S, (soonest - now).total_seconds()))

    def read(self, pub: Publisher, feed: Feed, now: datetime) -> int:
        """Read one feed under the checks; returns requests sent."""
        if pub.kind == "vendor" or feed.kind == "headlines":
            return self._read_vendor(pub, feed, now)
        url, host = feed.url, host_of(feed.url)
        why = self.refusal(pub, url)
        if why:
            self.gdb.note(feed.key, now, "refused", f"{why}; wait {REFUSED_MINUTES} min")
            self.log(f"publications: {pub.title} {feed.title}: {why}")
            return 0
        if pub.policy.contact and not self.contact:
            self.gdb.note(feed.key, now, "waiting", f"{feed.agency} {gov.WAITING}")
            return 0
        if self.requests_today(pub, now) >= pub.policy.per_day:
            self.gdb.note(feed.key, now, "failed", f"its {pub.policy.per_day} requests today are spent: read again tomorrow")
            return 0
        sent = 0
        rb, n = self.robots(pub, host, now)
        sent += n
        if not rb.allows(url):
            reason = "robots.txt did not answer: nothing read until it does" if not rb.answered \
                else f"robots.txt of {host} disallows it"
            self.gdb.note(feed.key, now, "failed" if not rb.answered else "refused",
                          reason if not rb.answered else f"{reason}; wait {REFUSED_MINUTES} min")
            return sent
        prev = self.gdb.fetched().get(feed.key, {})
        headers = self._headers(pub)
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("modified"):
            headers["If-Modified-Since"] = prev["modified"]
        r, n, error = self._get(pub, url, headers, now)
        sent += n
        if r is not None and 300 <= r.status_code < 400:  # moved: followed once, and only to its own hosts
            to = urljoin(url, _header(r, "Location") or "")
            why = "the contact it is sent goes nowhere else" if pub.policy.contact else self.refusal(pub, to)
            if why:
                self.gdb.note(feed.key, now, "failed", f"moved to {host_of(to) or 'nowhere'}: not followed ({why})")
                return sent
            r, n, error = self._get(pub, to, headers, now)
            sent += n
        if r is None:
            self.gdb.note(feed.key, now, "failed", f"no answer: {type(error).__name__}")
            return sent
        self._keep_limits(pub, feed, r, now)
        if r.status_code == 304:
            self.gdb.note(feed.key, now, "unchanged")
            return sent
        if r.status_code in (401, 403, 429):  # a refusal: not asked again for hours, never every few minutes
            after = max(retry_after_minutes(_header(r, "Retry-After")), REFUSED_MINUTES)
            self.gdb.note(feed.key, now, "refused", f"HTTP {r.status_code}; wait {after:.0f} min")
            self.log(f"publications: {pub.title} {feed.title}: refused (HTTP {r.status_code}); asked again in {after / 60:.0f} h")
            return sent
        if r.status_code != 200:
            self.gdb.note(feed.key, now, "failed", f"HTTP {r.status_code}")
            self.log(f"publications: {pub.title} {feed.title}: HTTP {r.status_code}")
            return sent
        if not _is_feed(r):
            ctype = _header(r, "Content-Type") or "no content type"
            self.gdb.note(feed.key, now, "failed", f"not a feed ({ctype}): pages are never read")
            self.log(f"publications: {pub.title} {feed.title}: not a feed ({ctype}); not read")
            return sent
        try:
            if feed.kind == "nyfed":
                rate = gov.parse_nyfed(r.json())
                if rate and rate["day"]:
                    self.gdb.save_rate(rate["series"], rate["day"], rate["value"], rate["lo"], rate["hi"], now)
                new = 0
            else:
                items = gov.parse_fedreg(r.json()) if feed.kind == "fedreg" else gov.parse_rss(r.content, feed)
                new = self.gdb.save_items(feed.key, items, now)
        except (ValueError, ET_XML.ParseError) as e:
            self.gdb.note(feed.key, now, "failed", f"unreadable answer: {e}"[:200])
            return sent
        self.gdb.note(feed.key, now, "ok", f"{new} new" if feed.kind != "nyfed" else None,
                      etag=_header(r, "ETag") or "", modified=_header(r, "Last-Modified") or "")
        if new:
            self.log(f"publications: {pub.title} {feed.title}: {new} new")
        return sent

    def refusal(self, pub: Publisher, url: str) -> str | None:
        """Why a URL may not be read by this publisher (None: it may)."""
        parts, host = urlsplit(url), host_of(url)
        if parts.scheme != "https":
            return f"not HTTPS ({parts.scheme or 'no scheme'})"
        if blocked(host):
            return f"{host}'s terms bar programs: never read"
        if host not in pub.policy.hosts:
            return f"{host or 'no host'} is not one of its hosts ({', '.join(pub.policy.hosts) or 'none'})"
        return None

    def _headers(self, pub: Publisher) -> dict:
        # the contact goes only to the publishers that ask automated readers for one
        ua = f"{USER_AGENT} {self.contact}" if self.contact and pub.policy.contact else USER_AGENT
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def _get(self, pub: Publisher, url: str, headers: dict, now: datetime):
        """One GET (a dropped connection once more), no redirect followed. Returns (answer or None,
        requests sent, the error)."""
        sent, error = 0, None
        for attempt in (1, 2):
            if self.requests_today(pub, now) >= pub.policy.per_day:
                break
            sent += 1
            self._count(pub, now)
            try:
                return self.session.get(url, headers=headers, timeout=TIMEOUT_S, allow_redirects=False), sent, None
            except (requests.ConnectionError, requests.Timeout) as e:
                error = e
                if attempt == 1:
                    self.sleep(RETRY_WAIT_S)
            except requests.RequestException as e:
                error = e
                break
        return None, sent, error or RuntimeError("daily cap")

    def _keep_limits(self, pub: Publisher, feed: Feed, r, now: datetime) -> None:
        """The rate-limit headers its server sent, kept with its last recheck's findings."""
        seen = {h: _header(r, h) for h in LIMIT_HEADERS if _header(r, h)}
        if seen:
            self.gdb.limits_seen(pub.name, feed.key, {"at": now.isoformat(timespec="seconds"), **seen})

    # -- robots.txt -------------------------------------------------------------------------------------
    def _robots_kept(self, host: str) -> Robots | None:
        row = self.gdb.robots_get(host)
        return Robots(host, row["status"], datetime.fromisoformat(row["at"]), row["body"] or "") if row else None

    def robots(self, pub: Publisher, host: str, now: datetime, force: bool = False) -> tuple[Robots, int]:
        """The host's robots.txt (read again when stale, or `force`d). Returns (it, requests sent)."""
        kept = self._robots_kept(host)
        if kept is not None and kept.fresh(now) and not force:
            return kept, 0
        if self.requests_today(pub, now) >= pub.policy.per_day:
            return kept or Robots(host, 0, now), 0
        r, sent, _ = self._get(pub, f"https://{host}/robots.txt", self._headers(pub), now)
        status = 0 if r is None else (404 if 300 <= r.status_code < 400 else r.status_code)  # moved: none here
        body = ""
        if status == 200:
            raw = getattr(r, "content", b"") or b""
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        self.gdb.robots_set(host, now, status, body)
        return Robots(host, status, now, body), sent

    # -- a vendor's headlines -----------------------------------------------------------------------------
    def _read_vendor(self, pub: Publisher, feed: Feed, now: datetime) -> int:
        from .errors import NotEntitled, RateLimited, SourceError, Unauthorized

        if pub.read is None or self.store is None:
            return 0
        if self.requests_today(pub, now) >= pub.policy.per_day:
            self.gdb.note(feed.key, now, "failed", f"its {pub.policy.per_day} requests today are spent: read again tomorrow")
            return 0
        sent = [0]

        def count(n: int = 1) -> None:
            sent[0] += n
            self._count(pub, now, n)
        try:
            new = pub.read(Context(pub, feed, self.settings, self.store, now, self.log, count))
        except (RateLimited, Unauthorized) as e:  # its feed's account said stop: that feed is killed, this with it
            killswitch.strike(self.store, pub.account, "fetch", f"{pub.title}: {e}", log=self.log,
                              at_least_s=killswitch.retry_after_s(e))
            self.gdb.note(feed.key, now, "refused", f"{e}; wait {REFUSED_MINUTES} min"[:300])
            return sent[0]
        except NotEntitled as e:
            self.gdb.note(feed.key, now, "refused", f"not on the plan: {e}; wait {REFUSED_MINUTES} min"[:300])
            return sent[0]
        except (SourceError, OSError, ValueError) as e:
            self.gdb.note(feed.key, now, "failed", f"{type(e).__name__}: {e}"[:300])
            return sent[0]
        self.gdb.note(feed.key, now, "ok", f"{new} new")
        return sent[0]

    # -- the limits recheck --------------------------------------------------------------------------------
    def recheck(self, pub: Publisher, now: datetime) -> dict:
        """Verify its limits again: each host's robots.txt read again, each feed asked once (its
        server's rate-limit headers kept), and the floor and daily cap that follow. A vendor's are
        its feed's: its rate window and daily cap, and whether that feed is killed. Kept and returned."""
        findings: dict = {"at": now.isoformat(timespec="seconds"), "hosts": {}, "notes": []}
        if pub.policy.contact and not self.contact:
            findings["notes"].append("waiting for GOV_CONTACT: nothing is sent to it, robots.txt neither")
            return findings
        if pub.kind == "vendor":
            from .sources import providers
            from .sources.registry import rate_window

            account = providers().get(pub.account)
            if account is not None and self.settings is not None:
                n, s = rate_window(pub.account, self.settings)
                findings["account"] = {"name": pub.account, "title": account.title, "rate": [n, s],
                                       "perDay": self.settings.for_source(pub.account).max_requests_per_day,
                                       "published": account.limits}
                findings["notes"].append(f"its requests count against {account.title}'s: its rate window and daily cap "
                                         "(verified by that feed's capability scan)")
        else:
            for host in pub.policy.hosts:
                rb, _ = self.robots(pub, host, now, force=True)
                findings["hosts"][host] = {"status": rb.status, "crawlDelayMinutes": round(rb.delay_minutes(), 2),
                                           "disallows": [f.key for f in pub.feeds if host_of(f.url) == host and not rb.allows(f.url)]}
                if rb.delay_minutes() > pub.policy.min_minutes:
                    findings["notes"].append(f"{host}'s robots.txt asks {rb.delay_minutes():g} min between requests: "
                                             f"longer than its own {pub.policy.min_minutes:g} min, so that is used")
                if 400 <= rb.status < 500:
                    findings["notes"].append(f"{host} publishes no robots.txt to this reader (HTTP {rb.status}): "
                                             "read as allowing all (RFC 9309)")
                elif not rb.answered:
                    findings["notes"].append(f"{host}'s robots.txt did not answer: nothing read from it until it does")
        self.gdb.recheck_set(pub.name, findings)  # kept first: the reads below are not a recheck again
        seen, floor = self.gdb.fetched(), timedelta(minutes=self.floor_minutes(pub, now))
        for feed in pub.feeds:  # each asked once, never within its floor (Recheck pressed twice asks nothing more)
            row = seen.get(feed.key)
            if row and row["status"] != "waiting" and now - datetime.fromisoformat(row["at"]) < floor:
                continue
            self.read(pub, feed, now)
        findings["floorMinutes"] = self.floor_minutes(pub, now)
        findings["limitsSeen"] = self.gdb.limits_kept(pub.name)
        if findings["limitsSeen"]:
            findings["notes"].append("its servers sent rate-limit headers (kept below): "
                                     "the reader keeps under them and honours any Retry-After")
        self.gdb.recheck_set(pub.name, findings)
        self.log(f"publications: {pub.title}: limits verified ({'; '.join(findings['notes']) or 'as declared'})")
        return findings


def next_moment(pub: Publisher, after: datetime, before: datetime) -> datetime | None:
    """The publisher's first release moment after `after` and no later than `before`."""
    from .sessions import ET

    day, last_day = after.astimezone(ET).date(), before.astimezone(ET).date()
    while day <= last_day:
        for m in sorted(pub.moments(day)):
            if after < m <= before:
                return m
        day += timedelta(days=1)
    return None


# -- what the views show ----------------------------------------------------------------------------
STATE_ORDER = ("paused", "refused", "failing", "waiting", "ready", "off")


def view(gdb: gov.GovDB, settings=None, store=None, now: datetime | None = None,
         publishers: dict[str, Publisher] | None = None) -> list[dict]:
    """Each publisher for the Plugins view: its state, its feeds (state, last read, next read), its
    scrape checks and limits as verified, its requests today, its schedule. Reads, never asks."""
    from .sources import publishers as registered

    now = now or datetime.now(timezone.utc)
    pubs = publishers if publishers is not None else registered()
    reader = Reader(gdb, settings, store, session=_NoSession(), log=lambda *_: None)
    seen = gdb.fetched()
    out = []
    for pub in pubs.values():
        kill = reader.kill(pub, now)
        floor = reader.floor_minutes(pub, now)
        feeds = []
        for f in pub.feeds:
            row = seen.get(f.key) or {}
            state = row.get("status") or "not asked yet"
            if pub.policy.contact and not reader.contact:
                state = "waiting"
            feeds.append({"key": f.key, "title": f.title, "agency": f.agency, "url": f.url, "host": host_of(f.url),
                          "every": f.every_minutes, "state": state,
                          "message": f"{f.agency} {gov.WAITING}" if state == "waiting" else row.get("message"),
                          "at": row.get("at"), "okAt": row.get("ok_at"),
                          "next": None if kill or not reader.set_up(pub) or state == "waiting"
                          else reader.next_read(pub, f, now, row or None).isoformat(timespec="seconds")})
        states = {f["state"] for f in feeds}
        state = ("paused" if kill and kill.get("by") == "user" else "refused" if kill else
                 "off" if not reader.set_up(pub) else "refused" if "refused" in states else
                 "failing" if "failed" in states else "waiting" if states == {"waiting"} else "ready")
        robots = []
        for host in pub.policy.hosts:
            rb = reader._robots_kept(host)
            robots.append({"host": host, "status": rb.status if rb else None, "at": rb.at.isoformat(timespec="seconds") if rb else None,
                           "delayMinutes": round(rb.delay_minutes(), 2) if rb else 0,
                           "allows": all(rb.allows(f.url) for f in pub.feeds if host_of(f.url) == host) if rb else None})
        out.append({
            "name": pub.name, "title": pub.title, "about": pub.about, "kind": pub.kind, "origin": pub.origin,
            "account": pub.account, "state": state, "kill": kill,
            "policy": {"hosts": list(pub.policy.hosts), "minMinutes": pub.policy.min_minutes, "perDay": pub.policy.per_day,
                       "contact": pub.policy.contact, "contactSet": bool(reader.contact), "terms": pub.policy.terms,
                       "termsUrl": pub.policy.terms_url, "blocked": list(BLOCKED_HOSTS)},
            "floorMinutes": floor, "requestsToday": reader.requests_today(pub, now),
            "feeds": feeds, "robots": robots, "recheck": gdb.recheck_get(pub.name),
            "calendar": [c for c in (pub.calendar(now.date()) if pub.calendar else [])
                         if datetime.fromisoformat(c["when"]) > now][:4],
            "moments": [m.isoformat(timespec="seconds") for m in _today_moments(pub, now)]})
    return out


def _today_moments(pub: Publisher, now: datetime) -> list[datetime]:
    from .sessions import ET

    return sorted(pub.moments(now.astimezone(ET).date())) if pub.moments else []


class _NoSession:
    """The views never ask: a reader made for them has nothing to send with."""

    def get(self, *a, **k):
        raise requests.ConnectionError("the views send no requests")


# -- running ------------------------------------------------------------------------------------------
def lock_path(main_db: Path | str) -> Path:
    return Path(main_db).with_name(LOCK)


def run_due(settings, log: Callable[[str], None], force: bool = False, now: datetime | None = None) -> float | None:
    """One pass over the publishers' due feeds (the hourly sync's when no recorder runs, and the
    `gov` command), under the machine's lock. Returns the seconds to the next due feed, None when
    another reader holds the lock or the copy is offline."""
    from . import proc
    from .storage import Store

    if getattr(settings, "offline", False):
        return None
    lock = lock_path(settings.db_path)
    if not proc.acquire(lock):
        return None
    gdb = gov.GovDB(gov.db_path(settings.db_path))
    try:
        with Store(settings.db_path) as store:
            return Reader(gdb, settings, store, log=log).step(now or datetime.now(timezone.utc), force=force)
    finally:
        gdb.close()
        proc.release(lock)


def recheck_now(settings, name: str, log: Callable[[str], None], wait_s: float = 30.0) -> dict | None:
    """A publisher's limits verified now (the Plugins view's Recheck): waits up to `wait_s` for the
    machine's lock. Returns its findings, or None (unknown, not set up here, or the lock busy)."""
    from . import proc
    from .sources import publishers as registered
    from .storage import Store

    pub = registered().get(name)
    if pub is None or getattr(settings, "offline", False):
        return None
    lock, until = lock_path(settings.db_path), time.monotonic() + wait_s
    while not proc.acquire(lock):
        if time.monotonic() > until:
            return None
        time.sleep(0.5)
    gdb = gov.GovDB(gov.db_path(settings.db_path))
    try:
        with Store(settings.db_path) as store:
            reader = Reader(gdb, settings, store, log=log)
            return reader.recheck(pub, datetime.now(timezone.utc)) if reader.set_up(pub) else None
    finally:
        gdb.close()
        proc.release(lock)


def pause(store, name: str, on: bool) -> dict | None:
    """Pause a publisher by hand (until resumed) or resume it. Returns the pause, or None."""
    if on:
        return killswitch.strike(store, name, "fetch", "paused by you", minutes=PAUSE_DAYS * 24 * 60, shared=False)
    killswitch.release(store, name, ("fetch",))
    return None
