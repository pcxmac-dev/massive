"""The CFTC: its Commitments of Traders, futures positioning each week (open interest; commercial,
non-commercial and small traders' positions), a dataset (datasets.py: cftc-cot-legacy, from 1986,
fetched with `datasets fetch` and refreshed on its own cadence). Its rules and notices arrive through
the Federal Register; its own news feeds are off (the CFTC disabled them all in October 2026), so it
has no feed to read here yet."""
from __future__ import annotations

from ...publisher import Policy, Publisher
from .release import API, VERSION

PUBLISHER = Publisher(
    version=VERSION, api=API, part_of="federal",  # the Federal plugin's release and contract (federal/__init__.py)
    name="cftc", title="CFTC", order=250,
    about="Commitments of Traders: futures positioning, weekly from 1986 (a dataset); its rules come "
          "through the Federal Register",
    feeds=(), datasets=("cftc-cot-legacy",),
    policy=Policy(hosts=("www.cftc.gov",), min_minutes=60, per_day=30,
                  terms="its reports are public domain (a U.S. government work); its news feeds are off "
                        "(October 2026)",
                  terms_url="https://www.cftc.gov/webpolicy/index.htm"),
)
