"""Federal: the US federal agencies and regulators whose actions and releases move or govern
companies and stocks, one plugin (the user, 2026-10-30: "merge everything in to a Federal plugin,
from news feeds to edgar access, to everything that matters"; state agencies go in a plugin of
their own).

Each agency is a part of it (Publisher.part_of, plugin API 1.2), a module here, with its own scrape
checks: its hosts, floor, daily cap, robots.txt, holds, pause and limits recheck, and its own state
kept by its name (gov/gov.db, the kills), as when each was a plugin of its own:
- the Federal Reserve (fed.py): press releases, speeches and testimony; the FOMC's calendar and
  release moments;
- the New York Fed (nyfed.py): the federal funds target range, the effective rate and SOFR;
- the SEC (sec.py): press releases and trading suspensions (waiting for GOV_CONTACT);
- the Bureau of Labor Statistics (bls.py): the employment situation, CPI, PPI and job openings;
- the Federal Register (fedreg.py): rules and notices of the SEC (FINRA's and the exchanges' rule
  filings), the CFTC, the Fed and the Treasury;
- the CFTC (cftc.py): its Commitments of Traders, a dataset (Publisher.datasets).

The plugin's own record (FEDERAL) reads nothing: pausing it pauses every agency in it (publications:
Reader.kill), and the Plugins view lists it once with its agencies. Its design, and what is to be
added (EDGAR's company data, the Treasury, the banks' regulators, the economic calendars, the
sector regulators, Congress): docs/ARCHITECTURE.md, "The Federal plugin"."""
from __future__ import annotations

from ...publisher import Policy, Publisher
from .bls import PUBLISHER as BLS
from .cftc import PUBLISHER as CFTC
from .fed import PUBLISHER as FED
from .fedreg import PUBLISHER as FEDREG
from .nyfed import PUBLISHER as NYFED
from .release import API, VERSION
from .sec import PUBLISHER as SEC

FEDERAL = Publisher(
    version=VERSION, api=API,  # the plugin's release, and the contract it needs (its agencies' part_of)
    name="federal", title="Federal", order=195,
    about="US federal agencies and regulators: the Fed, the New York Fed, the SEC, BLS, the Federal Register "
          "and the CFTC; official feeds and documented APIs only",
    feeds=(), policy=Policy(terms="each agency's own: official feeds and documented APIs, never a page"),
)

PUBLISHERS = (FEDERAL, FED, NYFED, SEC, BLS, FEDREG, CFTC)
