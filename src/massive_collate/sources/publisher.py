"""Publisher plugins: what an agency or a vendor publishes for programs (news, releases, rates),
read on a schedule of its own by the recorder (publications.py), never behind a sync.

A module in `sources/plugins/` defines `PUBLISHER` (a Publisher; `PUBLISHERS` for several), beside
or instead of a feed's `PLUGIN`: the federal agencies are parts of one plugin (plugins/federal/, a
module each: fed.py, sec.py ...; `part_of`), and a vendor's headlines sit in its feed's module
(massive.py), its requests on that feed's account.

Each declares its scrape checks (`Policy`), which the reader enforces before every request:
- only the hosts it names, over HTTPS, and never a blocked site (publications.BLOCKED_HOSTS: the
  sites whose terms bar programs, FINRA's, Yahoo's, Nasdaq.com's ...); a redirect elsewhere is not
  followed;
- feeds and APIs only: an answer that is not XML or JSON (a web page) is refused, never read;
- the host's robots.txt (read weekly; its crawl-delay kept), conditional requests, Retry-After;
- at most `per_day` requests a day and never a feed more often than `min_minutes`, whatever its
  schedule asks; a refusal holds it off for hours.
Its limits are verified again weekly and from the Plugins view (publications.recheck): robots.txt
read again and the rate-limit headers its servers send, the tighter of theirs and its own used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable


@dataclass(frozen=True)
class Feed:
    """One thing a publisher puts out for programs: an RSS or Atom feed, or a JSON API's answer."""
    key: str  # unique among all publishers' feeds (fed-press): its state and items are kept by it
    agency: str  # the name shown with its items (Fed, SEC ...)
    title: str
    url: str
    kind: str = "rss"  # rss | fedreg | nyfed | headlines (a vendor's, through its feed's client)
    every_minutes: float = 60.0  # read this often
    moments: bool = True  # read at the publisher's release moments too (its speeches: no)


@dataclass(frozen=True)
class Policy:
    """A publisher's scrape checks (see the module's docstring)."""
    hosts: tuple[str, ...] = ()  # the only hosts it reads; empty for a vendor read through its feed's client
    min_minutes: float = 15.0  # never one feed more often than this
    per_day: int = 200  # requests a day, its feeds and robots.txt together
    contact: bool = False  # it asks automated readers to identify themselves (GOV_CONTACT, in the User-Agent)
    terms: str = ""  # what its terms say about programs, in a line
    terms_url: str = ""


@dataclass(frozen=True)
class Publisher:
    name: str
    title: str
    about: str
    feeds: tuple[Feed, ...]
    policy: Policy
    order: int = 100
    kind: str = "official"  # official (an agency) | vendor (a data vendor's headlines)
    # a vendor's: the feed plugin whose key and limits its requests count against; that feed's
    # kill stops it, and it reads nothing without that feed's keys
    account: str = ""
    # extra reads at the publisher's release moments: the times (ET, aware) on a day it publishes
    # at a set time (the FOMC's statement at 2:00 p.m. on decision days)
    moments: Callable[[date], tuple[datetime, ...]] | None = None
    # its scheduled releases from a day on, for the views: [{"when": iso, "what": str}]
    calendar: Callable[[date], list[dict]] | None = None
    # a vendor's read: read(ctx) -> new items (ctx: publications.Context); official feeds are read
    # by the reader itself
    read: Callable[[object], int] | None = None
    # the plugin it is part of, when another publisher's (API 1.2; the Fed: Federal's): listed in it,
    # and stopped while that one is paused; its own scrape checks, state and pause still its own
    part_of: str = ""
    # the datasets it publishes (datasets.py names; API 1.2; the CFTC's Commitments of Traders):
    # fetched by `datasets`, shown with it
    datasets: tuple[str, ...] = ()
    # its own version and the plugin contract it was written for (sources/registry.py: PLUGIN_API)
    version: str = ""
    api: str = "1.0"
    origin: str = "built in"  # set when loaded

    def feed(self, key: str) -> Feed | None:
        return next((f for f in self.feeds if f.key == key), None)
