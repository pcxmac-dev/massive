"""The Federal Reserve: its press releases (monetary policy, regulation, enforcement) and its
speeches and testimony, from the RSS feeds it offers programs (federalreserve.gov/feeds).

Read every 30 minutes (speeches hourly), and at the FOMC's release moments: on a decision day the
statement comes at 2:00 p.m. ET, read at 2:00:20 and a few times after; the minutes come three
weeks after each decision, at 2:00 p.m. too. The decision days are the Fed's own calendar
(federalreserve.gov/monetarypolicy/fomccalendars.htm, read 2026-10-28); a later year is added
here when the Fed publishes it."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ...sessions import ET
from ..publisher import Feed, Policy, Publisher

# The second day of each FOMC meeting: the decision, at 2:00 p.m. ET. True: with the Summary of
# Economic Projections (the dot plot).
FOMC_DECISIONS = {
    date(2026, 1, 28): False, date(2026, 3, 18): True, date(2026, 4, 29): False, date(2026, 6, 17): True,
    date(2026, 7, 29): False, date(2026, 9, 16): True, date(2026, 10, 28): False, date(2026, 12, 9): True,
    date(2027, 1, 27): False, date(2027, 3, 17): True, date(2027, 4, 28): False, date(2027, 6, 9): True,
    date(2027, 7, 28): False, date(2027, 9, 15): True, date(2027, 10, 27): False, date(2027, 12, 8): True,
}
MINUTES_AFTER = timedelta(days=21)  # the minutes: three weeks after the decision, at 2:00 p.m. ET
DECISION_READS = (time(14, 0, 20), time(14, 1, 30), time(14, 5), time(14, 15), time(14, 35))
MINUTES_READS = (time(14, 0, 20), time(14, 2), time(14, 10))


def _minutes_days() -> set[date]:
    return {d + MINUTES_AFTER for d in FOMC_DECISIONS}


def moments(day: date) -> tuple[datetime, ...]:
    """The reads at the FOMC's releases on a day (ET): the statement, or the minutes."""
    reads = DECISION_READS if day in FOMC_DECISIONS else MINUTES_READS if day in _minutes_days() else ()
    return tuple(datetime.combine(day, t, tzinfo=ET) for t in reads)


def calendar(today: date) -> list[dict]:
    """The FOMC's next decisions and minutes, from `today` on."""
    out = [{"when": datetime.combine(d, time(14, 0), tzinfo=ET).isoformat(), "kind": "fomc-decision",
            "what": "FOMC decision" + (" with projections" if sep else ""), "projections": sep}
           for d, sep in FOMC_DECISIONS.items() if d >= today]
    out += [{"when": datetime.combine(d, time(14, 0), tzinfo=ET).isoformat(), "kind": "fomc-minutes",
             "what": "FOMC minutes"} for d in _minutes_days() if d >= today]
    return sorted(out, key=lambda c: c["when"])


PUBLISHER = Publisher(
    name="fed", title="Federal Reserve", order=200,
    about="press releases (monetary policy, regulation, enforcement), speeches and testimony; "
          "read at the FOMC's 2:00 p.m. releases",
    feeds=(
        Feed("fed-press", "Fed", "Press releases", "https://www.federalreserve.gov/feeds/press_all.xml",
             every_minutes=30),
        Feed("fed-speeches", "Fed", "Speeches and testimony",
             "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml", every_minutes=60, moments=False),
    ),
    policy=Policy(hosts=("www.federalreserve.gov",), min_minutes=1, per_day=120,
                  terms="RSS feeds the Fed offers for programs; public domain (a U.S. government work)",
                  terms_url="https://www.federalreserve.gov/feeds/feeds.htm"),
    moments=moments, calendar=calendar,
)
