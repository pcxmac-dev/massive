"""Publications: the publisher plugins' feeds (sources/publisher.py) read on each publisher's own
schedule, under its scrape checks, never behind a sync.

The recorder reads them in a thread of its own (live.Recorder: it is always on); the hourly sync
does when no recorder runs. One reader at a time on the machine (LOCK beside the database). Their
state, items and rates are kept in gov/gov.db (gov.GovDB); a vendor's headlines in the main
database's news (the News view and the globe's ticker).

When a feed is read: its cadence (`Feed.every_minutes`), and the publisher's release moments (the
FOMC's statement at 2:00 p.m. ET on decision days), never sooner than its floor (the publisher's
`min_minutes`, its hosts' robots.txt crawl-delay or request-rate, whichever is longest). A feed
that failed for a while (no answer, a 5xx) is asked again after FAILED_RETRY_MINUTES, doubling
while it keeps failing, up to its cadence; one that failed for good (a 4xx, not a feed, an
unreadable answer) after its cadence. A refusal (401, 403, 429) holds the whole publisher, every
feed and robots.txt, for REFUSED_MINUTES or the Retry-After when longer; a Retry-After on any other
answer holds it that long. A host whose robots.txt asks for a crawl-delay gets that long between
any two requests, whichever feed they are for.

The scrape checks, before every request (Policy, sources/publisher.py): HTTPS to the publisher's
own hosts only and never a blocked site (BLOCKED_HOSTS: the sites whose terms bar programs); a
redirect to another host not followed (and none at all where the contact is sent), one to its own
hosts only where that host's robots.txt allows it; the host's robots.txt, read weekly (RFC 9309: a
4xx answer allows all, no answer, a 5xx or a 429 none; followed through up to five redirects on its
own hosts); conditional requests; an answer that is not a feed (XML or JSON) refused, never read as
a page; at most the publisher's requests a day (`per_day`, robots.txt included). Nothing is sent
while it is paused, killed (its own kill, or its account's), or background fetching is paused or
halted (the chart's button).

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
FAILED_RETRY_MINUTES = 10  # a feed that failed for a while is asked again after this, doubling up to its cadence
REFUSED_MINUTES = 6 * 60  # a refusal (401, 403, 429) holds the publisher this long, or its Retry-After if longer
RETRY_WAIT_S = 2.0  # a dropped connection is tried once more after this (unless its host asks for longer)
TIMEOUT_S = 20
ROBOTS_DAYS = 7  # robots.txt read again after this (the recheck reads it at once)
ROBOTS_RETRY_MINUTES = 10  # robots.txt that did not answer: nothing read from the host until it does
ROBOTS_REDIRECTS = 5  # RFC 9309: robots.txt followed through at most this many redirects
RECHECK_DAYS = 7  # each publisher's limits verified again this often
RECHECK_ROBOTS_MINUTES = 60  # a recheck reads robots.txt again only when the one kept is older than this
RECHECK_AGAIN_MINUTES = 15  # Recheck pressed again within this sends nothing (the last findings stand)
REDIRECTS = (301, 302, 303, 307, 308)
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


def _wait_in(message: str | None) -> float | None:
    """The wait a feed's note gives ("...; wait N min"), in minutes, or None."""
    m = re.search(r"wait (\d+) min", message or "")
    return float(m.group(1)) if m else None


class CapSpent(Exception):
    """The publisher's requests for today are spent: nothing sent."""


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
    `store` is the main database (kills, the fetching pause, a vendor's headlines); `settings` the
    contact and keys."""

    def __init__(self, gdb: gov.GovDB, settings=None, store=None, session=None, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, contact: str | None = None):
        self.gdb, self.settings, self.store, self.log, self.sleep = gdb, settings, store, log, sleep
        self.session = session or requests.Session()
        self.contact = contact if contact is not None else getattr(settings, "gov_contact", None)

    # -- set up, stopped, held, its limits --------------------------------------------------------------
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
        """Why nothing is sent for it now, with `whose`: "own" (a pause by hand, or its own kill),
        "plugin" (the plugin it is part of paused or killed: Federal's agencies), "account" (its
        account's kill: a vendor's feed refused), "fetching" (background fetching paused or halted,
        the chart's button). None when it may read."""
        if self.store is None:
            return None
        own = killswitch.killed(self.store, pub.name, "fetch", now)
        if own:
            return {**own, "whose": "own"}
        plugin = killswitch.killed(self.store, pub.part_of, "fetch", now) if pub.part_of else None
        if plugin:
            return {**plugin, "whose": "plugin"}
        account = killswitch.killed(self.store, pub.account, "fetch", now) if pub.account else None
        if account:
            return {**account, "whose": "account"}
        from .collector import pause_state

        paused = pause_state(self.store, now)
        if paused:
            return {"by": "user", "why": "background fetching is paused (the chart's pause button)",
                    "until": paused.get("until"), "whose": "fetching"}
        return None

    def held(self, pub: Publisher, now: datetime) -> dict | None:
        """The hold on it still running (a refusal, a Retry-After): {until, why}, or None."""
        h = self.gdb.hold_get(pub.name)
        return h if h and datetime.fromisoformat(h["until"]) > now else None

    def _hold(self, pub: Publisher, now: datetime, minutes: float, why: str) -> None:
        self.gdb.hold_set(pub.name, now + timedelta(minutes=minutes), why)
        self.log(f"publications: {pub.title}: nothing sent for {minutes / 60:.1f} h ({why})")

    def _host_free(self, host: str, now: datetime) -> datetime:
        """When the host may be asked again (its robots.txt's crawl-delay since the last request)."""
        h = self.gdb.hold_get("host:" + host)
        return max(now, datetime.fromisoformat(h["until"])) if h else now

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

    def _spent(self, pub: Publisher, feed: Feed, now: datetime) -> bool:
        """Its requests today are spent: noted on the feed, asked again after the UTC midnight."""
        if self.requests_today(pub, now) < pub.policy.per_day:
            return False
        utc = now.astimezone(timezone.utc)
        midnight = datetime(utc.year, utc.month, utc.day, tzinfo=timezone.utc) + timedelta(days=1)
        self.gdb.note(feed.key, now, "failed", f"its {pub.policy.per_day} requests today are spent; "
                                               f"wait {max(1, round((midnight - utc).total_seconds() / 60))} min")
        return True

    # -- when -------------------------------------------------------------------------------------------
    def next_read(self, pub: Publisher, feed: Feed, now: datetime, row: dict | None = None) -> datetime:
        """When a feed is next due (now or before: due now); never inside a hold on its publisher."""
        out = self._next(pub, feed, now, row if row is not None else self.gdb.fetched().get(feed.key))
        held = self.held(pub, now)
        return max(out, datetime.fromisoformat(held["until"])) if held else out

    def _next(self, pub: Publisher, feed: Feed, now: datetime, row: dict | None) -> datetime:
        if not row:
            return now
        last = datetime.fromisoformat(row["at"])
        status, floor = row["status"], timedelta(minutes=self.floor_minutes(pub, now))
        every = timedelta(minutes=feed.every_minutes)
        wait = _wait_in(row.get("message"))
        if status == "waiting":  # the contact it waited for is set now: at once
            return now if self.contact else last + every
        if status == "refused":
            return last + timedelta(minutes=max(REFUSED_MINUTES, wait or 0))
        if status == "failed":
            return last + max(floor, timedelta(minutes=wait) if wait is not None
                              else min(every, timedelta(minutes=FAILED_RETRY_MINUTES)))
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
        """Read every set-up, unstopped, unheld publisher's due feeds (`force`: every feed, still
        under its floor, cap, holds and refusals); its limits verified first when due. One
        publisher's trouble never stops the others. Returns the seconds until the next feed is due
        (at most STEP_MAX_S)."""
        from .sources import publishers as registered

        pubs = publishers if publishers is not None else registered()
        soonest = now + timedelta(seconds=STEP_MAX_S)
        for pub in pubs.values():
            try:
                soonest = min(soonest, self._step_one(pub, now, force))
            except Exception as e:  # noqa: BLE001 - a plugin's surprise: logged, the others still read
                self.log(f"publications: {pub.title}: {type(e).__name__}: {e}")
        return max(1.0, min(STEP_MAX_S, (soonest - now).total_seconds()))

    def _step_one(self, pub: Publisher, now: datetime, force: bool) -> datetime:
        later = now + timedelta(seconds=STEP_MAX_S)
        if not pub.feeds:  # a plugin's own record (Federal), or a part with datasets only: nothing to read
            return later
        if not self.set_up(pub) or self.kill(pub, now):
            return later
        if pub.policy.contact and not self.contact:  # nothing at all goes to it, robots.txt neither
            seen = self.gdb.fetched()
            for f in pub.feeds:
                if (seen.get(f.key) or {}).get("status") != "waiting":
                    self.gdb.note(f.key, now, "waiting", f"{f.agency} {gov.WAITING}")
            return later
        held = self.held(pub, now)
        if held:
            return min(later, datetime.fromisoformat(held["until"]))
        last_check = self.gdb.recheck_get(pub.name)
        if last_check is None or now - datetime.fromisoformat(last_check["at"]) >= timedelta(days=RECHECK_DAYS):
            self.recheck(pub, now)
            return later  # the recheck read each feed once
        soonest, seen = later, self.gdb.fetched()
        for feed in pub.feeds:
            if self.held(pub, now):  # refused in this pass: the rest wait with it
                break
            row = seen.get(feed.key)
            nxt = self.next_read(pub, feed, now, row)
            if force and row and row["status"] != "refused":
                nxt = min(nxt, max(now, datetime.fromisoformat(row["at"]) + timedelta(minutes=self.floor_minutes(pub, now))))
            if nxt <= now:
                free = now if pub.kind == "vendor" else self._host_free(host_of(feed.url), now)
                if free > now:  # its host's crawl-delay since the last request: asked on a later pass
                    nxt = free
                else:
                    self.read(pub, feed, now)
                    nxt = self.next_read(pub, feed, now)
            soonest = min(soonest, nxt)
        return soonest

    def read(self, pub: Publisher, feed: Feed, now: datetime) -> int:
        """Read one feed under the checks; returns requests sent. Never raises: whatever goes wrong
        (a plugin's or a parser's surprise) is noted on the feed and asked again after its cadence."""
        try:
            if pub.kind == "vendor" or feed.kind == "headlines":
                return self._read_vendor(pub, feed, now)
            return self._read(pub, feed, now)
        except Exception as e:  # noqa: BLE001 - noted, never re-asked every pass
            self._failed(pub, feed, now, f"{type(e).__name__}: {e}", lasting=True)
            self.log(f"publications: {pub.title} {feed.title}: {type(e).__name__}: {e}")
            return 0

    def _failed(self, pub: Publisher, feed: Feed, now: datetime, why: str, lasting: bool = False) -> None:
        """A failure noted with when it is asked again: one that lasts (a 4xx, not a feed, an
        unreadable answer, a plugin's error) after its cadence; a passing one (no answer, a 5xx)
        after FAILED_RETRY_MINUTES, doubling while it keeps failing, up to its cadence (a feed that
        stays down never costs more than one that answers)."""
        floor = self.floor_minutes(pub, now)
        cadence = max(feed.every_minutes, floor)
        wait = cadence
        if not lasting:
            prev = self.gdb.fetched().get(feed.key) or {}
            was = _wait_in(prev.get("message")) if prev.get("status") == "failed" else None
            wait = max(floor, min(cadence, 2 * was if was else FAILED_RETRY_MINUTES))
        self.gdb.note(feed.key, now, "failed", f"{why[:240]}; wait {wait:.0f} min")

    def _read(self, pub: Publisher, feed: Feed, now: datetime) -> int:
        url, host = feed.url, host_of(feed.url)
        why = self.refusal(pub, url)
        if why:
            self.gdb.note(feed.key, now, "refused", f"{why}; wait {REFUSED_MINUTES} min")
            self.log(f"publications: {pub.title} {feed.title}: {why}")
            return 0
        if pub.policy.contact and not self.contact:
            self.gdb.note(feed.key, now, "waiting", f"{feed.agency} {gov.WAITING}")
            return 0
        if self.held(pub, now) or self._spent(pub, feed, now):
            return 0
        rb, sent = self.robots(pub, host, now)
        if self.held(pub, now):  # its robots.txt asked it to wait
            return sent
        if not rb.allows(url):
            if rb.answered:
                self.gdb.note(feed.key, now, "refused", f"robots.txt of {host} disallows it; wait {REFUSED_MINUTES} min")
            else:
                self._failed(pub, feed, now, "robots.txt did not answer: nothing read until it does")
            return sent
        if self._host_free(host, now) > now:  # its crawl-delay since the last request: a later pass
            return sent
        prev = self.gdb.fetched().get(feed.key, {})
        headers = self._headers(pub)
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("modified"):
            headers["If-Modified-Since"] = prev["modified"]
        r, n, error = self._get(pub, url, headers, now)
        sent += n
        if r is not None and r.status_code in REDIRECTS:  # moved: followed once, to its own hosts where allowed
            to = urljoin(url, _header(r, "Location") or "")
            why = "the contact it is sent goes nowhere else" if pub.policy.contact else self.refusal(pub, to)
            if not why:
                to_rb, n = self.robots(pub, host_of(to), now)
                sent += n
                if not to_rb.allows(to):
                    why = f"robots.txt of {host_of(to)} {'disallows it' if to_rb.answered else 'did not answer'}"
                elif self._host_free(host_of(to), now) > now:
                    why = f"{host_of(to)} asks for a pause between requests"
            if why:
                self._failed(pub, feed, now, f"moved to {host_of(to) or 'nowhere'}: not followed ({why})", lasting=True)
                return sent
            r, n, error = self._get(pub, to, headers, now)
            sent += n
            if r is not None and r.status_code in REDIRECTS:
                self._failed(pub, feed, now, "moved again: not followed", lasting=True)
                return sent
        if r is None:
            if isinstance(error, CapSpent):
                self._spent(pub, feed, now)
            else:
                self._failed(pub, feed, now, f"no answer: {type(error).__name__}")
            return sent
        self._keep_limits(pub, feed, r, now)
        if r.status_code == 304:
            self.gdb.note(feed.key, now, "unchanged")
            return sent
        asked = retry_after_minutes(_header(r, "Retry-After"))
        if r.status_code in (401, 403, 429):  # a refusal: the whole publisher waits hours, never minutes
            after = max(asked, REFUSED_MINUTES)
            self._hold(pub, now, after, f"{feed.title}: refused, HTTP {r.status_code}")
            self.gdb.note(feed.key, now, "refused", f"HTTP {r.status_code}; wait {after:.0f} min")
            return sent
        if r.status_code != 200:
            if asked:  # asked to wait (a 503's Retry-After): the whole publisher does
                self._hold(pub, now, max(asked, FAILED_RETRY_MINUTES), f"{feed.title}: HTTP {r.status_code} with a Retry-After")
            self._failed(pub, feed, now, f"HTTP {r.status_code}", lasting=400 <= r.status_code < 500)
            self.log(f"publications: {pub.title} {feed.title}: HTTP {r.status_code}")
            return sent
        if not _is_feed(r):
            ctype = _header(r, "Content-Type") or "no content type"
            self._failed(pub, feed, now, f"not a feed ({ctype}): pages are never read", lasting=True)
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
        except (ValueError, ET_XML.ParseError, AttributeError, KeyError, TypeError, IndexError) as e:
            self._failed(pub, feed, now, f"unreadable answer: {type(e).__name__}: {e}", lasting=True)
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

    def _get(self, pub: Publisher, url: str, headers: dict, now: datetime, spaced: bool = True):
        """One GET within the daily cap, no redirect followed; a dropped connection tried once more
        unless its host asks for longer between requests. `spaced`: the host's crawl-delay starts
        again from it (robots.txt is not spaced). Returns (answer or None, requests sent, the error)."""
        host = host_of(url)
        rb = self._robots_kept(host)
        delay_s = rb.delay_minutes() * 60 if rb else 0.0
        sent, error, r = 0, None, None
        for attempt in (1, 2):
            if self.requests_today(pub, now) >= pub.policy.per_day:
                error = error or CapSpent(f"{pub.policy.per_day} requests today")
                break
            sent += 1
            self._count(pub, now)
            try:
                r, error = self.session.get(url, headers=headers, timeout=TIMEOUT_S, allow_redirects=False), None
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                error = e
                if attempt == 1 and delay_s <= RETRY_WAIT_S:
                    self.sleep(RETRY_WAIT_S)
                    continue
                break
            except requests.RequestException as e:
                error = e
                break
        if spaced and sent and delay_s:
            self.gdb.hold_set("host:" + host, now + timedelta(seconds=delay_s), "its robots.txt's crawl-delay")
        return (r if error is None else None), sent, error

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
        """The host's robots.txt (read again when stale, or `force`d), followed through up to
        ROBOTS_REDIRECTS redirects on its own hosts (one elsewhere, or any where the contact is
        sent: not followed, so nothing is read from the host). A 429, or a Retry-After on an answer
        that is not one, holds the publisher. Returns (it, requests sent)."""
        kept = self._robots_kept(host)
        if kept is not None and kept.fresh(now) and not force:
            return kept, 0
        if self.requests_today(pub, now) >= pub.policy.per_day:
            return kept or Robots(host, 0, now), 0
        url, sent, r = f"https://{host}/robots.txt", 0, None
        for hop in range(ROBOTS_REDIRECTS + 1):
            r, n, _ = self._get(pub, url, self._headers(pub), now, spaced=False)
            sent += n
            if r is None or r.status_code not in REDIRECTS:
                break
            to = urljoin(url, _header(r, "Location") or "")
            why = "the contact it is sent goes nowhere else" if pub.policy.contact else self.refusal(pub, to)
            if why or hop == ROBOTS_REDIRECTS:
                self._hold(pub, now, REFUSED_MINUTES, f"robots.txt of {host} moved to {host_of(to) or 'nowhere'}, "
                                                      f"not followed ({why or 'too many redirects'})")
                r = None
                break
            url = to
        status = 0 if r is None else r.status_code
        body = ""
        if status == 200:
            raw = getattr(r, "content", b"") or b""
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        asked = retry_after_minutes(_header(r, "Retry-After")) if r is not None else 0
        if status == 429 or (asked and status != 200):
            self._hold(pub, now, max(asked, REFUSED_MINUTES if status == 429 else FAILED_RETRY_MINUTES),
                       f"robots.txt of {host}: HTTP {status}")
        self.gdb.robots_set(host, now, status, body)
        return Robots(host, status, now, body), sent

    # -- a vendor's headlines -----------------------------------------------------------------------------
    def _read_vendor(self, pub: Publisher, feed: Feed, now: datetime) -> int:
        from .errors import BudgetExhausted, NotEntitled, RateLimited, SourceError, Unauthorized

        if pub.read is None or self.store is None:
            return 0
        if self._spent(pub, feed, now):
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
            self.gdb.note(feed.key, now, "refused", f"{e}"[:240] + f"; wait {REFUSED_MINUTES} min")
            return sent[0]
        except NotEntitled as e:
            self.gdb.note(feed.key, now, "refused", f"not on the plan: {e}"[:240] + f"; wait {REFUSED_MINUTES} min")
            return sent[0]
        except (SourceError, BudgetExhausted, OSError, ValueError) as e:
            self._failed(pub, feed, now, f"{type(e).__name__}: {e}")
            return sent[0]
        self.gdb.note(feed.key, now, "ok", f"{new} new")
        return sent[0]

    # -- the limits recheck --------------------------------------------------------------------------------
    def recheck(self, pub: Publisher, now: datetime) -> dict:
        """Verify its limits again: each host's robots.txt read again (unless read within
        RECHECK_ROBOTS_MINUTES), each feed asked once (never within its floor, while refused or
        held, or inside its host's crawl-delay; its server's rate-limit headers kept), and the floor
        and daily cap that follow. A vendor's are its feed's: its rate window and daily cap. Nothing
        is sent while it is stopped or held (the findings kept stand). Kept and returned."""
        findings: dict = {"at": now.isoformat(timespec="seconds"), "hosts": {}, "notes": []}
        if not pub.feeds:  # a plugin's own record, or a part with datasets only: robots.txt neither
            return {**findings, "notes": ["no feed of its own to read: nothing sent"]}
        if pub.policy.contact and not self.contact:
            findings["notes"].append("waiting for GOV_CONTACT: nothing is sent to it, robots.txt neither")
            self.gdb.recheck_set(pub.name, findings)
            return findings
        stop = self.kill(pub, now) or self.held(pub, now)
        if stop:
            self.log(f"publications: {pub.title}: not rechecked: {stop.get('why')} (until {stop.get('until')})")
            return {**findings, "notes": [f"not rechecked: {stop.get('why')}"], "stopped": True}
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
                kept = self._robots_kept(host)
                recent = kept is not None and now - kept.at < timedelta(minutes=RECHECK_ROBOTS_MINUTES)
                rb, _ = self.robots(pub, host, now, force=not recent)
                findings["hosts"][host] = {"status": rb.status, "crawlDelayMinutes": round(rb.delay_minutes(), 2),
                                           "disallows": [f.key for f in pub.feeds if host_of(f.url) == host and not rb.allows(f.url)]}
                if rb.delay_minutes() > pub.policy.min_minutes:
                    findings["notes"].append(f"{host}'s robots.txt asks {rb.delay_minutes():g} min between requests: "
                                             f"longer than its own {pub.policy.min_minutes:g} min, so that is used")
                if rb.answered and rb.status != 200:
                    findings["notes"].append(f"{host} publishes no robots.txt to this reader (HTTP {rb.status}): "
                                             "read as allowing all (RFC 9309)")
                elif not rb.answered:
                    findings["notes"].append(f"{host}'s robots.txt did not answer (HTTP {rb.status or 'none'}): "
                                             "nothing read from it until it does")
        self.gdb.recheck_set(pub.name, findings)  # kept first: the reads below are not a recheck again
        seen, floor = self.gdb.fetched(), timedelta(minutes=self.floor_minutes(pub, now))
        for feed in pub.feeds:  # each asked once, never within its floor, refused or held
            if self.held(pub, now):
                break
            row = seen.get(feed.key)
            if row and row["status"] != "waiting" and now - datetime.fromisoformat(row["at"]) < floor:
                continue
            if row and row["status"] == "refused" and self.next_read(pub, feed, now, row) > now:
                continue
            if pub.kind != "vendor" and self._host_free(host_of(feed.url), now) > now:
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


def _calendar(pub: Publisher, now: datetime) -> list[dict]:
    """Its next releases (at most four), from its calendar; a plugin's calendar that fails shows none."""
    if not pub.calendar:
        return []
    try:
        out = [c for c in pub.calendar(now.date()) if datetime.fromisoformat(c["when"]) > now]
    except (TypeError, ValueError, KeyError):  # a naive time, a malformed entry: the plugin's to fix
        return []
    return out[:4]


def view(gdb: gov.GovDB, settings=None, store=None, now: datetime | None = None,
         publishers: dict[str, Publisher] | None = None) -> list[dict]:
    """Each publisher for the Plugins view: its state, its feeds (state, last read, next read), its
    scrape checks and limits as verified, its requests today, its schedule. Reads, never asks. A
    publisher whose plugin fails here is shown with the error, the others as they are."""
    from .sources import publishers as registered

    now = now or datetime.now(timezone.utc)
    pubs = publishers if publishers is not None else registered()
    reader = Reader(gdb, settings, store, session=_NoSession(), log=lambda *_: None)
    out = []
    for pub in pubs.values():
        try:
            out.append(_publisher_view(reader, gdb, pub, now))
        except Exception as e:  # noqa: BLE001 - one plugin's fault never takes the view down
            out.append({"name": pub.name, "title": pub.title, "about": pub.about, "kind": pub.kind, "origin": pub.origin,
                        "version": pub.version, "api": pub.api,
                        "account": pub.account, "state": "off", "error": f"{type(e).__name__}: {e}", "kill": None,
                        "held": None, "policy": {"hosts": list(pub.policy.hosts), "minMinutes": pub.policy.min_minutes,
                                                 "perDay": pub.policy.per_day, "contact": pub.policy.contact,
                                                 "contactSet": bool(reader.contact), "terms": pub.policy.terms,
                                                 "termsUrl": pub.policy.terms_url, "blocked": list(BLOCKED_HOSTS)},
                        "floorMinutes": pub.policy.min_minutes, "requestsToday": 0, "feeds": [], "robots": [],
                        "recheck": None, "calendar": [], "moments": [], "partOf": pub.part_of,
                        "datasets": list(pub.datasets), "parts": []})
    by = {p["name"]: p for p in out}
    for p in out:
        if p["partOf"] not in by or p["partOf"] == p["name"]:
            p["partOf"] = ""  # part of no publisher here: one of its own
    for p in out:  # a plugin of several publishers (Federal): its parts, their requests, its state from theirs
        parts = [q for q in out if q["partOf"] == p["name"] and q is not p]
        if not parts:
            continue
        p["parts"] = [q["name"] for q in parts]
        p["requestsToday"] += sum(q["requestsToday"] for q in parts)
        states = {q["state"] for q in parts}
        if p["state"] not in ("paused", "refused", "off"):
            p["state"] = ("failing" if states & {"failing", "refused"} else "ready" if "ready" in states
                          else "waiting" if "waiting" in states else "off" if states <= {"off"} else p["state"])
    return out


def _publisher_view(reader: Reader, gdb: gov.GovDB, pub: Publisher, now: datetime) -> dict:
    seen = gdb.fetched()
    kill, held = reader.kill(pub, now), reader.held(pub, now)
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
    paused = kill is not None and kill.get("by") == "user" and kill.get("whose") in ("own", "plugin", "fetching")
    state = ("paused" if paused else "refused" if kill or held else
             "off" if not reader.set_up(pub) else "refused" if "refused" in states else
             "failing" if "failed" in states else "waiting" if states == {"waiting"} else "ready")
    robots = []
    for host in pub.policy.hosts:
        rb = reader._robots_kept(host)
        robots.append({"host": host, "status": rb.status if rb else None, "at": rb.at.isoformat(timespec="seconds") if rb else None,
                       "answered": rb.answered if rb else None, "delayMinutes": round(rb.delay_minutes(), 2) if rb else 0,
                       "allows": all(rb.allows(f.url) for f in pub.feeds if host_of(f.url) == host) if rb else None})
    return {
        "name": pub.name, "title": pub.title, "about": pub.about, "kind": pub.kind, "origin": pub.origin,
        "version": pub.version, "api": pub.api, "account": pub.account, "state": state, "kill": kill, "held": held,
        "policy": {"hosts": list(pub.policy.hosts), "minMinutes": pub.policy.min_minutes, "perDay": pub.policy.per_day,
                   "contact": pub.policy.contact, "contactSet": bool(reader.contact), "terms": pub.policy.terms,
                   "termsUrl": pub.policy.terms_url, "blocked": list(BLOCKED_HOSTS)},
        "floorMinutes": floor, "requestsToday": reader.requests_today(pub, now),
        "feeds": feeds, "robots": robots, "recheck": gdb.recheck_get(pub.name),
        "calendar": _calendar(pub, now),
        "moments": [m.isoformat(timespec="seconds") for m in _today_moments(pub, now)],
        "partOf": pub.part_of, "datasets": list(pub.datasets), "parts": []}


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
    machine's lock. Pressed again within RECHECK_AGAIN_MINUTES of the last, nothing is sent (the
    findings kept are returned). Returns its findings, or None (unknown, not set up here, or the
    lock busy)."""
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
            reader, now = Reader(gdb, settings, store, log=log), datetime.now(timezone.utc)
            if not reader.set_up(pub):
                return None
            last = gdb.recheck_get(pub.name)
            if last and now - datetime.fromisoformat(last["at"]) < timedelta(minutes=RECHECK_AGAIN_MINUTES):
                log(f"publications: {pub.title}: verified {(now - datetime.fromisoformat(last['at'])).seconds // 60} min ago: "
                    "nothing sent again")
                return last
            return reader.recheck(pub, now)
    finally:
        gdb.close()
        proc.release(lock)


def pause(store, name: str, on: bool) -> dict | None:
    """Pause a publisher by hand (until resumed) or resume it. Returns the pause, or None."""
    if on:
        return killswitch.strike(store, name, "fetch", "paused by you", minutes=PAUSE_DAYS * 24 * 60, shared=False)
    killswitch.release(store, name, ("fetch",))
    return None
