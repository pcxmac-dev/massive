"""The New York Fed: the federal funds target range, the effective federal funds rate and SOFR,
from its Markets Data API (documented for programs, no key). It publishes each business day:
SOFR at about 8:00 a.m. ET and the effective rate at about 9:00 a.m., read just after those, and
every four hours besides."""
from __future__ import annotations

from datetime import date, datetime, time

from ....sessions import ET
from ...publisher import Feed, Policy, Publisher
from .release import API, VERSION


def moments(day: date) -> tuple[datetime, ...]:
    if day.weekday() >= 5:
        return ()
    return tuple(datetime.combine(day, t, tzinfo=ET) for t in (time(8, 5), time(9, 5)))


PUBLISHER = Publisher(
    version=VERSION, api=API, part_of="federal",  # the Federal plugin's release and contract (federal/__init__.py)
    name="nyfed", title="New York Fed", order=210,
    about="the federal funds target range, the effective federal funds rate and SOFR",
    feeds=(
        Feed("nyfed-effr", "New York Fed", "Effective federal funds rate and target range",
             "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json", "nyfed", every_minutes=240),
        Feed("nyfed-sofr", "New York Fed", "SOFR",
             "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json", "nyfed", every_minutes=240),
    ),
    policy=Policy(hosts=("markets.newyorkfed.org",), min_minutes=30, per_day=40,
                  terms="the Markets Data API, documented for programs; no key",
                  terms_url="https://markets.newyorkfed.org/static/docs/markets-api.html"),
    moments=moments,
)
