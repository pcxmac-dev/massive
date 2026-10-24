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

Point in time (storage/datasets.py keeps every version of every value): a version's new and changed
values become available when the source published that version (a git commit's time, or the
file's Last-Modified date). A file's first import is history published on a schedule, so its
values are dated by the source's publication rule (`Dataset.published`), never later than the
file itself. Datasets hosted on GitHub are git submodules of this repository (data/github/...,
pinned commits) and their whole commit history is imported, each commit a published version; the
other files are downloaded and every distinct version is kept locally with its checksum
(<data>/sources/datasets/<provider>/).
"""
from __future__ import annotations

import csv
import email.utils
import hashlib
import io
import math
import os
import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
USER_AGENT = "massive-collate/1 (personal research; historical archive)"
FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
CFTC = "https://www.cftc.gov/files/dea/history/"
# The git submodules of GitHub-hosted datasets: <repository root>/data/github/<owner>/<repo>.
REPOS = Path(os.environ.get("MASSIVE_DATA_REPOS") or Path(__file__).resolve().parents[2] / "data" / "github")


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
    # When a value of the period starting at ts (ms) was first published, by the source's schedule:
    # dates the history in a file's first import. None: the file's own time (or members' 'date added').
    published: Callable[[int], int] | None = None
    git: tuple[str, str] | None = None  # (owner/repo, path of the file in it): a git submodule


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def _day_of(ts: int) -> date:
    return datetime.fromtimestamp(ts / 1000, ET).date()


def _at(d: date, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET).timestamp() * 1000)


def _months_after(months: int, day: int) -> Callable[[int], int]:
    """Published on `day` of the month `months` after the period's month (US/Eastern midnight)."""
    def rule(ts: int) -> int:
        d = _day_of(ts)
        m = d.month - 1 + months
        return _at(date(d.year + m // 12, m % 12 + 1, day))
    return rule


def _cot_published(ts: int) -> int:
    """The COT report as of a Tuesday is released that Friday at 15:30 ET (a holiday week a day or
    two later)."""
    d = _day_of(ts)
    return _at(d + timedelta(days=(4 - d.weekday()) % 7), 15, 30)


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


def _shiller_date(raw: str) -> date:
    """A Shiller date: ISO (2026-09-01), year.month as in Shiller's own sheet (1871.01, 1871.1 for
    October) or a decimal year at mid-month as in the mirror's first versions (1871.04166 = January)."""
    raw = raw.strip()
    if "-" in raw:
        return date.fromisoformat(raw[:10])
    year, _, frac = raw.partition(".")
    if len(frac) == 2:
        month = int(frac)
    elif len(frac) == 1:
        month = int(frac) * 10
    else:
        month = min(12, int(float("0." + (frac or "0")) * 12) + 1)
    return date(int(year), month, 1)


def parse_shiller(url: str, blob: bytes) -> Parsed:
    """The mirror writes 0 where Shiller has no value yet (recent dividends, earnings, CPI; CAPE's
    first ten years): none of these series is ever really 0, so a 0 is left out as missing."""
    out = Parsed()
    rows = csv.DictReader(io.StringIO(_text(blob, url)))
    for r in rows:
        d = _shiller_date(r.pop("Date"))
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
        sector = (r.get("GICS Sector") or r.get("Sector") or "").strip()  # older versions: Name, Sector
        out.constituents.append({"index": "S&P 500", "ticker": t, "name": (r.get("Security") or r.get("Name") or "").strip(),
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
            parse_constituents, 7, "sp500", "mirror",
            git=("datasets/s-and-p-500-companies", "data/constituents.csv")),
    Dataset("french-3-factors-daily", "Fama/French 3 factors, daily (from 1926)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_Factors_daily_CSV.zip", False)], parse_french, 30,
            "french", "research", published=_months_after(2, 15)),
    Dataset("french-3-factors-monthly", "Fama/French 3 factors, monthly (from 1926)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_Factors_CSV.zip", False)], parse_french, 30,
            "french", "research", published=_months_after(2, 15)),
    Dataset("french-5-factors-daily", "Fama/French 5 factors (2x3), daily (from 1963)", "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", False)], parse_french, 30,
            "french", "research", published=_months_after(2, 15)),
    Dataset("french-49-industries-daily", "49 industry portfolios, value-weighted daily returns (from 1926)",
            "Kenneth R. French Data Library",
            lambda t: [(FRENCH + "49_Industry_Portfolios_daily_CSV.zip", False)],
            lambda url, blob: parse_french(url, blob, section="Average Value Weighted Returns"), 30,
            "french", "research", published=_months_after(2, 15)),
    Dataset("shiller-us-market-monthly", "Shiller: S&P composite, dividends, earnings, CPI, rates, CAPE (from 1871)",
            "Robert J. Shiller (shillerdata.com) via datasets/s-and-p-500, ODC PDDL",
            lambda t: [("https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv", False)],
            parse_shiller, 30, "shiller", "mirror", published=_months_after(1, 2),
            git=("datasets/s-and-p-500", "data/data.csv")),
    Dataset("imf-commodities-monthly", "IMF primary commodity prices and indexes, monthly 1980-2017 (frozen mirror)",
            "IMF terms (personal, non-commercial use) via datasets/commodity-prices",
            lambda t: [("https://raw.githubusercontent.com/datasets/commodity-prices/main/data/commodity-prices.csv", True)],
            parse_commodities, 30, "imf", "mirror", published=_months_after(1, 5),
            git=("datasets/commodity-prices", "data/commodity-prices.csv")),
    Dataset("cftc-cot-legacy", "CFTC Commitments of Traders, legacy futures-only, weekly (from 1986)",
            "CFTC (public domain)", _cot_files, parse_cot, 7, "cftc", "official", published=_cot_published),
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
    """Fetch (or refresh) datasets: new file versions or commits are stored as versions. With
    `only_started`, only datasets fetched before (the scheduled sync never starts one by itself).
    Returns {name: versions stored}."""
    today = today or datetime.now(ET).date()
    session = session or requests.Session()
    started = {n for n in DATASETS if store.dataset_files(n)}
    upgrade_legacy(store, log)
    done = {}
    for name in names or list(DATASETS):
        ds = DATASETS[name]
        if only_started and name not in started:
            continue
        if ds.git and _repo(ds) is not None:
            done[name] = fetch_git(store, ds, today, force, log)
        else:
            done[name] = _fetch_files(store, ds, today, force, session, log)
    return done


def _changed(counts: dict) -> int:
    return sum(counts[k] for k in ("new", "revised", "withdrawn", "joined", "changed", "left"))


def _describe(counts: dict) -> str:
    parts = [f"{counts[k]:,} {k}" for k in ("new", "revised", "withdrawn", "joined", "changed", "left") if counts[k]]
    return ", ".join(parts) or "no changes"


def _quality_prior(store, ds: Dataset) -> None:
    if store.quality(ds.name, "dataset") is None:  # a prior until something measures it
        from .composite import PRIORS

        store.set_quality(ds.name, "dataset", PRIORS[ds.kind], {"basis": "prior", "kind": ds.kind, "licence": ds.licence})


def _ms_dt(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _published_at(resp) -> datetime | None:
    """The file's Last-Modified time: when the source published this version."""
    raw = (getattr(resp, "headers", None) or {}).get("Last-Modified")
    try:
        return email.utils.parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _keep(store, ds: Dataset, file: str, blob: bytes, sha: str) -> Path | None:
    """Keep this version of a downloaded file: <data>/sources/datasets/<provider>/<name>.<sha12><ext>."""
    if store.datasets_dir is None:
        return None
    base = store.datasets_dir.parent / "sources" / "datasets" / ds.provider
    f = Path(file)
    dest = base / f"{f.stem}.{sha[:12]}{f.suffix}"
    if not dest.exists():
        base.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    return dest


def _fetch_files(store, ds: Dataset, today: date, force: bool, session, log) -> int:
    name, n = ds.name, 0
    single = len(ds.files(today)) == 1  # a file holding the whole dataset: what it drops is withdrawn
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
        blob = resp.content
        sha = hashlib.sha256(blob).hexdigest()
        prev = store.dataset_files(name).get(url)
        if prev and prev["sha256"] == sha:
            store.set_dataset_file(name, url, len(blob), prev["values"] or 0, closed=final, sha256=sha,
                                   version_at=prev["version_at"], origin=sha, local_path=prev["local_path"])
            log(f"{name}: {file} unchanged")
            continue
        version = _published_at(resp) or datetime.now(timezone.utc)
        local = _keep(store, ds, file, blob, sha)
        counts = store.save_dataset_version(ds.provider, name, parsed, _ms_dt(version), sha,
                                            first_rule=ds.published if prev is None else None,
                                            withdraw=single and prev is not None)
        store.set_dataset_file(name, url, len(blob), _changed(counts), closed=final, sha256=sha, version_at=version,
                               origin=sha, local_path=str(local) if local else None)
        _quality_prior(store, ds)
        log(f"{name}: {file} ({len(blob) / 1e6:.2f} MB, published {version:%Y-%m-%d}): {_describe(counts)}"
            + ("; final, closed" if final else ""))
        n += 1
    return n


class GitError(RuntimeError):
    pass


def _repo(ds: Dataset) -> Path | None:
    """The dataset's git submodule checkout, if it is there."""
    if not ds.git:
        return None
    path = REPOS / ds.git[0]
    return path if (path / ".git").exists() else None


def _git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=600)
    if r.returncode != 0:
        raise GitError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout if binary else r.stdout.decode()


def _git_ref(repo: Path) -> str:
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", ref)
            return ref
        except GitError:
            continue
    return "HEAD"


def fetch_git(store, ds: Dataset, today: date, force: bool = False, log=print, update: bool = True) -> int:
    """Import every commit of a GitHub-hosted dataset's file not imported yet, oldest first, each a
    published version at its commit time; then check the submodule out at the repository's newest
    commit."""
    repo, path = _repo(ds), ds.git[1]
    files = store.dataset_files(ds.name)
    git_files = {u: f for u, f in files.items() if u.startswith("git:")}
    if git_files and not force and (today - max(f["fetched_at"] for f in git_files.values()).date()).days < ds.refresh_days:
        return 0
    if update:
        try:
            _git(repo, "fetch", "--quiet", "origin")
        except GitError as e:
            log(f"{ds.name}: could not update {ds.git[0]} ({e}); using the commits already here")
    ref = _git_ref(repo)
    commits = [line.split(" ", 1) for line in _git(repo, "log", "--reverse", "--format=%H %cI", ref, "--", path).splitlines()
               if line.strip()]
    if not git_files and files:
        store.clear_dataset_files(ds.name)  # the downloads' ledger gives way to the commit history
    seen = {f["origin"] for f in git_files.values()}
    n, totals = 0, dict(new=0, revised=0, withdrawn=0, unchanged=0, joined=0, changed=0, left=0)
    for sha, when in commits:
        if sha in seen:
            continue
        t = datetime.fromisoformat(when)
        blob = _git(repo, "show", f"{sha}:{path}", binary=True)
        try:
            parsed = ds.parse(path, blob)
        except (ValueError, KeyError, csv.Error) as e:
            log(f"{ds.name}: commit {sha[:8]} not readable ({e}); skipped")
            parsed = Parsed()
        url = f"git:{ds.git[0]}@{sha}"
        if not parsed.values and not parsed.constituents:
            store.set_dataset_file(ds.name, url, len(blob), 0, closed=True, version_at=t, origin=sha,
                                   local_path=str(repo / path))
            continue
        counts = store.save_dataset_version(ds.provider, ds.name, parsed, _ms_dt(t), sha,
                                            first_rule=ds.published if not seen and n == 0 else None, withdraw=True)
        store.set_dataset_file(ds.name, url, len(blob), _changed(counts), closed=True, version_at=t, origin=sha,
                               local_path=str(repo / path))
        totals = {k: totals[k] + counts[k] for k in totals}
        n += 1
    if commits:
        _git(repo, "checkout", "--quiet", "--detach", ref)  # the repository's newest commit: the file's latest version
        _quality_prior(store, ds)
    log(f"{ds.name}: {n} new commits of {ds.git[0]} ({len(commits)} in all)" + (f": {_describe(totals)}" if n else ""))
    return n


def upgrade_legacy(store, log=print) -> None:
    """Carry values stored before versioning into versioned storage: datasets from GitHub are
    imported again from their commit history (exact publication times); the others become one
    version dated by their publication rule."""
    for provider in store.dataset_providers():
        obs, members = store.dataset_legacy(provider)
        if not obs and not members:
            continue
        for ds in (d for d in DATASETS.values() if d.provider == provider):
            if _repo(ds) is not None:
                store.clear_dataset_files(ds.name)  # the next fetch imports the commits
                continue
            files = store.dataset_files(ds.name)
            when = max((f["fetched_at"] for f in files.values()), default=datetime.now(timezone.utc))
            parsed = Parsed(values=[(s_, ts, v) for d, s_, ts, v in obs if d == ds.name],
                            constituents=[{"index": i, "ticker": t, "name": n, "sector": se, "industry": ind,
                                           "added": a, "cik": c} for i, t, n, se, ind, a, c in members]
                            if ds.parse is parse_constituents else [])
            if parsed.values or parsed.constituents:
                store.save_dataset_version(provider, ds.name, parsed, _ms_dt(when), "before-versioning",
                                           first_rule=ds.published)
        store.drop_dataset_legacy(provider)
        log(f"datasets: {provider} carried over to versioned storage")


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


def values(store, name: str, series: list[str], as_of: int | None = None) -> dict[str, dict]:
    """{series: {title, unit, freq, t: [ms...], v: [values...]}} for up to MAX_SERIES series: the
    latest version, or what was public at `as_of` (ms)."""
    ds = DATASETS[name]
    meta = {r["series"]: r for r in store.dataset_series(ds.provider, name)}
    out = {}
    for s in series[:MAX_SERIES]:
        if s not in meta:
            raise KeyError(f"no series {s!r} in {name}")
        df = store.dataset_values(ds.provider, name, s, as_of=as_of)
        out[s] = {"title": meta[s]["title"], "unit": meta[s]["unit"], "freq": meta[s]["freq"],
                  "t": [int(t) for t in df["ts"]], "v": [float(v) for v in df["value"]]}
    return out


def status(store, today: date | None = None) -> list[dict]:
    """Every dataset: files fetched and closed, values, series, span, last fetch, files due, quality."""
    today = today or datetime.now(ET).date()
    out = []
    for ds in DATASETS.values():
        files = store.dataset_files(ds.name)
        series = [x for x in store.dataset_series(ds.provider, ds.name) if x["n"]] if files else []  # withdrawn left out
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
            "due": (int((today - max(f["fetched_at"] for f in files.values()).date()).days >= ds.refresh_days)
                    if ds.git and _repo(ds) is not None else len(due_files(store, ds, today))) if files else None,
            "source": f"git {ds.git[0]}" if ds.git and _repo(ds) is not None else "download",
            "quality": q["score"] if q else None,
        })
    return out
