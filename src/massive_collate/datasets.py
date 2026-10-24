"""Research datasets and reference sets: free public files, downloaded whole and kept locally.

Unlike the feeds these are not price bars of watched symbols: factor and portfolio returns,
market-level history, futures positioning, commodity prices, index membership with sector
classes. Each file is fetched once; a file that will not change again (a finished year, an
archive) is then closed and never fetched again, the others refresh on their own cadence (only
once they have been fetched the first time with `datasets fetch`). Each provider's values are kept
in a file of their own (datasets/<provider>.db), and each dataset has a quality prior by provenance
(an official publisher, the researcher's own library, or a mirror of someone else's data).

- S&P 500 constituents with GICS sector and sub-industry (datasets/s-and-p-500-companies, ODC
  PDDL): the market map's sectors for S&P 500 members.
- Kenneth R. French Data Library (Tuck, Dartmouth): Fama/French 3 and 5 factors, 49 industry
  portfolios; daily and monthly, from 1926 (1963 for 5 factors). Returns in percent.
- Robert J. Shiller's US stock market data (S&P composite, dividends, earnings, CPI, rates, CAPE),
  monthly from 1871, via the datasets/s-and-p-500 CSV (ODC PDDL).
- IMF primary commodity prices (53 commodities, 10 indexes, 2005 = 100), monthly 1980-2017, via
  datasets/commodity-prices: a mirror frozen since 2017, so an archive (closed once imported). IMF
  terms: personal, non-commercial use.
- CFTC Commitments of Traders, legacy futures-only report, weekly from 1986: open interest and
  commercial / non-commercial / small trader positions for every market.

Not included: Norgate and CRSP (paid / licensed), Kaggle datasets (need a Kaggle login).
"""
from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
USER_AGENT = "massive-collate/1 (personal research; historical archive)"
FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
CFTC = "https://www.cftc.gov/files/dea/history/"


@dataclass
class Parsed:
    values: list[tuple[str, int, float]] = field(default_factory=list)  # (series, ts ms, value)
    series: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # series -> (title, unit, freq)
    constituents: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Dataset:
    name: str
    title: str
    licence: str
    files: Callable[[date], list[tuple[str, bool]]]  # today -> [(url, final: closes once imported)]
    parse: Callable[[str, bytes], Parsed]
    refresh_days: int  # how often the files that are not final are fetched again
    provider: str      # whose data it is: names its file, datasets/<provider>.db
    kind: str          # provenance for the quality prior (composite.PRIORS): official | research | mirror


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def _num(cell) -> float | None:
    """A finite number from a cell, else None (blank, 'nan', text)."""
    try:
        v = float(cell)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _text(blob: bytes, url: str) -> str:
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return z.read(z.namelist()[0]).decode("latin-1")
    return blob.decode("utf-8-sig", errors="replace")


# -- parsers --------------------------------------------------------------------------------

def parse_french(url: str, blob: bytes, section: str | None = None, unit: str = "%") -> Parsed:
    """A French data library CSV: the first table (or the one after a heading containing
    `section`): a header row then rows of YYYYMMDD / YYYYMM and values in percent."""
    lines = _text(blob, url).splitlines()
    out = Parsed()
    start = 0
    if section:
        start = next((i for i, l in enumerate(lines) if section.lower() in l.lower()), 0)
    header, freq = None, None
    for line in lines[start:]:
        cells = [c.strip() for c in line.split(",")]
        if header is None:
            if len(cells) > 1 and cells[0] == "" and any(cells[1:]):
                header = cells
            continue
        if not cells[0].isdigit():
            if out.values:
                break  # the end of the first table
            continue
        key = cells[0]
        if len(key) == 8:
            d, freq = date(int(key[:4]), int(key[4:6]), int(key[6:])), "day"
        elif len(key) == 6:
            d, freq = date(int(key[:4]), int(key[4:]), 1), "month"
        else:
            break  # the annual table that follows the monthly one
        for name, cell in zip(header[1:], cells[1:]):
            v = _num(cell)
            if v is None or v in (-99.99, -999):
                continue  # the library's missing-value markers
            out.values.append((name, _ms(d), v))
    for name in (header or [])[1:]:
        out.series[name] = (name, unit, freq or "")
    return out


def parse_shiller(url: str, blob: bytes) -> Parsed:
    """The mirror writes 0 where Shiller has no value yet (recent dividends, earnings, CPI; CAPE's
    first ten years): none of these series is ever really 0, so a 0 is left out as missing."""
    out = Parsed()
    rows = csv.DictReader(io.StringIO(_text(blob, url)))
    for r in rows:
        d = date.fromisoformat(r.pop("Date")[:10])
        for k, v in r.items():
            if (x := _num(v)) is not None and x != 0:
                out.values.append((k, _ms(d), x))
    units = {"SP500": "index", "Dividend": "USD", "Earnings": "USD", "Consumer Price Index": "index",
             "Long Interest Rate": "%", "Real Price": "USD", "Real Dividend": "USD", "Real Earnings": "USD",
             "PE10": "ratio"}
    for k in {s for s, _, _ in out.values}:
        out.series[k] = (k, units.get(k, ""), "month")
    return out


def parse_commodities(url: str, blob: bytes) -> Parsed:
    out = Parsed()
    for r in csv.DictReader(io.StringIO(_text(blob, url))):
        raw = r.pop("Date", None) or r.pop("date", None)
        if not raw:
            continue
        d = date.fromisoformat(raw[:10]) if "-" in raw else date(int(raw[:4]), int(raw[5:7] or 1), 1)
        for k, v in r.items():
            if (x := _num(v)) is not None:
                out.values.append((k, _ms(d), x))
    for k in {s for s, _, _ in out.values}:
        out.series[k] = (k, "index (2005 = 100)" if "Index" in k else "USD (nominal)", "month")
    return out


COT_FIELDS = {
    "Open Interest (All)": "open_interest",
    "Noncommercial Positions-Long (All)": "noncommercial_long",
    "Noncommercial Positions-Short (All)": "noncommercial_short",
    "Noncommercial Positions-Spreading (All)": "noncommercial_spreading",
    "Commercial Positions-Long (All)": "commercial_long",
    "Commercial Positions-Short (All)": "commercial_short",
    "Nonreportable Positions-Long (All)": "small_long",
    "Nonreportable Positions-Short (All)": "small_short",
}


def parse_cot(url: str, blob: bytes) -> Parsed:
    """CFTC legacy futures-only report: one series per market code and field ('067651:open_interest')."""
    out = Parsed()
    for r in csv.DictReader(io.StringIO(_text(blob, url))):
        code = (r.get("CFTC Contract Market Code") or "").strip()
        raw = (r.get("As of Date in Form YYYY-MM-DD") or "").strip()
        if not code or not raw:
            continue
        d = date.fromisoformat(raw)
        name = (r.get("Market and Exchange Names") or code).strip()
        for col, key in COT_FIELDS.items():
            v = _num((r.get(col) or "").strip())
            if v is not None:
                series = f"{code}:{key}"
                out.values.append((series, _ms(d), v))
                out.series.setdefault(series, (f"{name}: {key.replace('_', ' ')}", "contracts", "week"))
    return out


GICS_TO_MAP = {"Information Technology": "Technology", "Consumer Discretionary": "Consumer Cyclical",
               "Consumer Staples": "Consumer Defensive", "Financials": "Financial", "Health Care": "Healthcare",
               "Materials": "Basic Materials"}


def parse_constituents(url: str, blob: bytes) -> Parsed:
    out = Parsed()
    for r in csv.DictReader(io.StringIO(_text(blob, url))):
        t = (r.get("Symbol") or "").strip().upper()
        if not t:
            continue
        sector = (r.get("GICS Sector") or "").strip()
        out.constituents.append({"index": "S&P 500", "ticker": t, "name": (r.get("Security") or "").strip(),
                                 "sector": GICS_TO_MAP.get(sector, sector),
                                 "industry": (r.get("GICS Sub-Industry") or "").strip(),
                                 "added": (r.get("Date added") or "").strip() or None,
                                 "cik": (r.get("CIK") or "").strip() or None})
    return out


def _cot_files(today: date) -> list[tuple[str, bool]]:
    # 1986-2016 in one archive, then a file a year; a past year's file is final
    return [(CFTC + "deacot1986_2016.zip", True)] + [
        (f"{CFTC}deacot{y}.zip", y < today.year) for y in range(2017, today.year + 1)]


DATASETS: dict[str, Dataset] = {d.name: d for d in (
    Dataset("sp500-constituents", "S&P 500 members with GICS sector and sub-industry",
            "ODC PDDL (datasets/s-and-p-500-companies)",
            lambda t: [("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv", False)],
            parse_constituents, 7, "sp500", "mirror"),
    Dataset("french-3-factors-daily", "Fama/French 3 factors, daily (from 1926)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_Factors_daily_CSV.zip", False)], parse_french, 30,
            "french", "research"),
    Dataset("french-3-factors-monthly", "Fama/French 3 factors, monthly (from 1926)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_Factors_CSV.zip", False)], parse_french, 30,
            "french", "research"),
    Dataset("french-5-factors-daily", "Fama/French 5 factors (2x3), daily (from 1963)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", False)], parse_french, 30,
            "french", "research"),
    Dataset("french-49-industries-daily", "49 industry portfolios, value-weighted daily returns (from 1926)",
            "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "49_Industry_Portfolios_daily_CSV.zip", False)],
            lambda url, blob: parse_french(url, blob, section="Average Value Weighted Returns"), 30,
            "french", "research"),
    Dataset("shiller-us-market-monthly", "Shiller: S&P composite, dividends, earnings, CPI, rates, CAPE (from 1871)",
            "Robert J. Shiller (shillerdata.com) via datasets/s-and-p-500, ODC PDDL",
            lambda t: [("https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv", False)],
            parse_shiller, 30, "shiller", "mirror"),
    Dataset("imf-commodities-monthly", "IMF primary commodity prices and indexes, monthly 1980-2017 (frozen mirror)",
            "IMF terms (personal, non-commercial use) via datasets/commodity-prices",
            lambda t: [("https://raw.githubusercontent.com/datasets/commodity-prices/main/data/commodity-prices.csv", True)],
            parse_commodities, 30, "imf", "mirror"),
    Dataset("cftc-cot-legacy", "CFTC Commitments of Traders, legacy futures-only, weekly (from 1986)",
            "CFTC (public domain)", _cot_files, parse_cot, 7, "cftc", "official"),
)}


def due_files(store, ds: Dataset, today: date, force: bool = False) -> list[tuple[str, bool]]:
    """Files to fetch now: never-fetched ones, and open ones older than the refresh cadence. A
    final file once imported is closed and never fetched again."""
    state = store.dataset_files(ds.name)
    out = []
    for url, final in ds.files(today):
        s = state.get(url)
        if s is None:
            out.append((url, final))
        elif s["closed"]:
            continue
        elif force or (today - s["fetched_at"].date()).days >= ds.refresh_days:
            out.append((url, final))
    return out


def fetch(store, names: list[str] | None = None, today: date | None = None, force: bool = False,
          session: requests.Session | None = None, log=print, only_started: bool = False) -> dict:
    """Fetch (or refresh) datasets. `only_started`: refresh only datasets fetched before (the
    scheduled sync never starts one by itself). Returns {name: files fetched}."""
    today = today or datetime.now(ET).date()
    session = session or requests.Session()
    done = {}
    for name in names or list(DATASETS):
        ds = DATASETS[name]
        if only_started and not store.dataset_files(name):
            continue
        n = 0
        for url, final in due_files(store, ds, today, force):
            file = url.rsplit("/", 1)[-1]
            try:
                resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
                if resp.status_code == 404 and not final:
                    log(f"{name}: {file} not published yet")
                    continue
                resp.raise_for_status()
                parsed = ds.parse(url, resp.content)
            except (requests.RequestException, ValueError, KeyError, zipfile.BadZipFile) as e:
                log(f"{name}: {file} not fetched ({e}); tried again next time")  # the others go on
                continue
            if not parsed.values and not parsed.constituents:
                log(f"{name}: {file} had nothing to read (format changed?); not marked fetched")
                continue
            # a dataset in one file is replaced whole (revised history, values withdrawn); one in
            # several files (a file a year) adds each file's values
            rows = store.save_dataset(ds.provider, name, parsed, replace=len(ds.files(today)) == 1)
            store.set_dataset_file(name, url, len(resp.content), rows, closed=final)
            if store.quality(name, "dataset") is None:  # a prior until something measures it
                from .composite import PRIORS

                store.set_quality(name, "dataset", PRIORS[ds.kind],
                                  {"basis": "prior", "kind": ds.kind, "licence": ds.licence})
            log(f"{name}: {file} ({len(resp.content) / 1e6:.2f} MB): {rows:,} values"
                + ("; final, closed" if final else ""))
            n += 1
        done[name] = n
    return done


# -- reading, for the chart app's dataset viewer ---------------------------------------------

MAX_SERIES = 8  # series drawn at once (the categorical palette's eight slots)


def series_list(store, name: str) -> list[dict]:
    """A dataset's series with their span; for the COT report its markets instead (see cot_markets)."""
    ds = DATASETS[name]
    return [{"series": r["series"], "title": r["title"], "unit": r["unit"], "freq": r["freq"],
             "first": r["first_ts"], "last": r["last_ts"], "n": r["n"]}
            for r in store.dataset_series(ds.provider, name) if r["n"]]


def cot_markets(store) -> list[dict]:
    """Markets of the COT report: code, name, span and latest open interest; those still reported
    first, largest open interest first."""
    ds = DATASETS["cftc-cot-legacy"]
    last = store.dataset_last_values(ds.provider, ds.name)
    markets: dict[str, dict] = {}
    for r in store.dataset_series(ds.provider, ds.name):
        code, _, field = r["series"].partition(":")
        m = markets.setdefault(code, {"code": code, "name": code, "first": None, "last": None, "openInterest": None})
        if field == "open_interest":
            m["name"] = (r["title"] or code).rsplit(": ", 1)[0]
            m["first"], m["last"] = r["first_ts"], r["last_ts"]
            m["openInterest"] = last.get(r["series"])
    newest = max((m["last"] or 0 for m in markets.values()), default=0)
    current = newest - 21 * 86_400_000  # reported within three weeks of the newest report
    return sorted(markets.values(), key=lambda m: (-(m["last"] or 0 >= current), -(m["openInterest"] or 0), m["name"]))


def values(store, name: str, series: list[str]) -> dict[str, dict]:
    """{series: {title, unit, freq, t: [ms...], v: [values...]}} for up to MAX_SERIES series."""
    ds = DATASETS[name]
    meta = {r["series"]: r for r in store.dataset_series(ds.provider, name)}
    out = {}
    for s in series[:MAX_SERIES]:
        if s not in meta:
            raise KeyError(f"no series {s!r} in {name}")
        df = store.dataset_values(ds.provider, name, s)
        out[s] = {"title": meta[s]["title"], "unit": meta[s]["unit"], "freq": meta[s]["freq"],
                  "t": [int(t) for t in df["ts"]], "v": [float(v) for v in df["value"]]}
    return out


def status(store, today: date | None = None) -> list[dict]:
    """Every dataset: files fetched and closed, values, series, span, last fetch, files due, quality."""
    today = today or datetime.now(ET).date()
    out = []
    for ds in DATASETS.values():
        files = store.dataset_files(ds.name)
        series = store.dataset_series(ds.provider, ds.name) if files else []
        members = len(store.constituents(ds.provider, "S&P 500")) if files and ds.parse is parse_constituents else 0
        q = store.quality(ds.name, "dataset")
        out.append({
            "name": ds.name, "title": ds.title, "provider": ds.provider, "kind": ds.kind, "licence": ds.licence,
            "files": len(files), "closed": sum(f["closed"] for f in files.values()),
            "bytes": sum(f["bytes"] or 0 for f in files.values()),
            "values": sum(s["n"] or 0 for s in series), "series": len(series), "members": members,
            "first": min((s["first_ts"] for s in series if s["first_ts"] is not None), default=None),
            "last": max((s["last_ts"] for s in series if s["last_ts"] is not None), default=None),
            "fetched": max((f["fetched_at"] for f in files.values()), default=None),
            "due": len(due_files(store, ds, today)) if files else None,
            "quality": q["score"] if q else None,
        })
    return out
