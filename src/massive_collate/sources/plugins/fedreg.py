"""The Federal Register: rules, proposed rules and notices of the SEC (with FINRA's and the
exchanges' rule filings, tagged), the CFTC, the Fed and the Treasury, from its documented JSON API
(no key). It publishes each business day at 6:00 a.m. ET, read just after, and every three hours
besides. FINRA's own site is never read (its terms bar programs): its rule filings arrive here."""
from __future__ import annotations

from datetime import date, datetime, time

from ...gov import FEDREG_URL
from ...sessions import ET
from ..publisher import Feed, Policy, Publisher


def moments(day: date) -> tuple[datetime, ...]:
    return () if day.weekday() >= 5 else (datetime.combine(day, time(6, 5), tzinfo=ET),)


PUBLISHER = Publisher(
    version="1.0.0", api="1.0",  # its own release, and the plugin contract it was written for
    name="fedreg", title="Federal Register", order=240,
    about="rules and notices of the SEC (FINRA's and the exchanges' filings), the CFTC, the Fed and the Treasury",
    feeds=(Feed("fedreg", "Federal Register", "Rules and notices: SEC (FINRA, exchanges), CFTC, Fed, Treasury",
                FEDREG_URL, "fedreg", every_minutes=180),),
    policy=Policy(hosts=("www.federalregister.gov",), min_minutes=30, per_day=30,
                  terms="the Federal Register API, documented for programs; no key",
                  terms_url="https://www.federalregister.gov/developers/documentation/api/v1"),
    moments=moments,
)
