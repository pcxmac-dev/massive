"""The SEC: its press releases and trading suspensions, from its RSS feeds.

Its fair-access policy turns away unidentified automated readers and allows at most 10 requests
a second: these wait for GOV_CONTACT (your name and email, sent in the User-Agent to the SEC only;
nothing is sent until you set it), and are read hourly, far inside that rate."""
from __future__ import annotations

from ..publisher import Feed, Policy, Publisher

PUBLISHER = Publisher(
    name="sec", title="SEC", order=220,
    about="press releases and trading suspensions",
    feeds=(
        Feed("sec-press", "SEC", "Press releases", "https://www.sec.gov/news/pressreleases.rss", every_minutes=60),
        Feed("sec-suspensions", "SEC", "Trading suspensions",
             "https://www.sec.gov/enforcement-litigation/trading-suspensions/rss", every_minutes=60),
    ),
    policy=Policy(hosts=("www.sec.gov",), min_minutes=15, per_day=60, contact=True,
                  terms="fair access: identify with your name and email in the User-Agent; "
                        "at most 10 requests a second",
                  terms_url="https://www.sec.gov/about/developer-resources"),
)
