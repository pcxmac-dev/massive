"""Audits: how closely a second feed agrees with what is stored.

An audit takes a span one feed holds (the owner) and asks another feed (the auditor) for the
same span. The second copy is compared in memory and only the comparison is kept: audits are the
one time a span is downloaded twice, and they never store bars.

What is compared, bar by bar on identical timestamps:
- timing, to the second: bars present in only one feed, bar stamps that are not whole minutes,
  and whether shifting one feed by a bar lines them up better (start- vs end-stamped bars);
- open, high, low, close and the high-low range, in basis points of price;
- volume and trade count in percent, VWAP in basis points.
Each comparison keeps its box stats (min, quartiles, median, 95th percentile, max, mean) and the
share of bars within TOLERANCE. Feeds filter trade conditions slightly differently, so a small
margin of error is expected; the verdict says whether it stays small.

Audits run after new data arrives (queued for another feed's run, within that feed's caps), at
random over stored history, or on demand (`audit` command). Their pass rates feed the ranking
that decides which feed downloads a span (coverage.py).
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import freshness
from .coverage import STEP_MS, FeedProfile, day_start_ms, frontier_ms, has_open_time, ms
from .errors import NotEntitled, SourceError
from .sessions import ET

# Margin of error per bar: a price within max(PRICE_BPS of price, one cent) of the other feed's,
# volume and trade count within their percentages.
TOLERANCE = {"price_bps": 5.0, "price_abs": 0.01, "volume_pct": 5.0, "count_pct": 10.0, "vwap_bps": 5.0}
# Verdict thresholds: share of matched bars within tolerance, share of bars only one feed has.
PASS = {"price": 0.98, "volume": 0.90, "missing": 0.02}
WARN = {"price": 0.90, "volume": 0.75, "missing": 0.10}
PRICE_FIELDS = ("o", "h", "l", "c")
NAMES = {"o": "open", "h": "high", "l": "low", "c": "close", "range": "high-low range", "v": "volume",
         "vw": "VWAP", "n": "trade count"}
QUEUE_MAX_AGE = timedelta(days=7)
# What each feed's crypto bars are (the same pair can be different markets).
VENUES = {("massive", "crypto"): "consolidated across exchanges", ("alpaca", "crypto"): "one exchange's book (Kraken)"}


def box(values) -> dict | None:
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if not len(v):
        return None
    q = np.percentile(v, [0, 25, 50, 75, 95, 100])
    return {"n": int(len(v)), "min": q[0], "q1": q[1], "median": q[2], "q3": q[3], "p95": q[4], "max": q[5],
            "mean": float(v.mean())}


def _bps(a, b):
    return (b - a) / a * 1e4 if a else None


def _pct(a, b):
    return (b - a) / a * 100 if a else (0.0 if not b else None)


def compare(owner: list[dict], auditor: list[dict], step_ms: int = 60_000, judge_volume: bool = True) -> dict:
    """Compare two copies of a span (bars: t ms, o, h, l, c, v, vw, n). Diffs are auditor minus
    owner. Returns counts, per-field box stats and within-tolerance shares, timing, worst bars and
    a verdict. `judge_volume=False` reports volume without judging it (feeds covering different
    venues, e.g. consolidated crypto against one exchange's book)."""
    a = {int(x["t"]): x for x in owner}
    b = {int(x["t"]): x for x in auditor}
    if not a or not b:
        return {"verdict": "no-data", "matched": 0, "onlyOwner": len(a), "onlyAuditor": len(b)}
    both = sorted(a.keys() & b.keys())
    union = len(a.keys() | b.keys())
    diffs: dict[str, list] = {k: [] for k in (*PRICE_FIELDS, "range", "v", "n", "vw")}
    within: dict[str, list[bool]] = {k: [] for k in diffs}
    worst = []
    for t in both:
        x, y = a[t], b[t]
        price = x.get("c") or x.get("o")
        tol_bps = max(TOLERANCE["price_bps"], TOLERANCE["price_abs"] / price * 1e4) if price else TOLERANCE["price_bps"]
        biggest = (0.0, None)
        for f in PRICE_FIELDS:
            d = _bps(x.get(f), y.get(f))
            diffs[f].append(d)
            if d is not None:
                within[f].append(abs(d) <= tol_bps)
                if abs(d) > abs(biggest[0]):
                    biggest = (d, f)
        if x.get("h") is not None and x.get("l") is not None and y.get("h") is not None and price:
            d = ((y["h"] - y["l"]) - (x["h"] - x["l"])) / price * 1e4
            diffs["range"].append(d)
            within["range"].append(abs(d) <= 2 * tol_bps)  # two prices, so twice the margin
        for f, tol in (("v", TOLERANCE["volume_pct"]), ("n", TOLERANCE["count_pct"])):
            d = _pct(x.get(f), y.get(f)) if x.get(f) is not None and y.get(f) is not None else None
            diffs[f].append(d)
            if d is not None:
                within[f].append(abs(d) <= tol)
        if x.get("vw") and y.get("vw"):
            d = _bps(x["vw"], y["vw"])
            diffs["vw"].append(d)
            within["vw"].append(abs(d) <= TOLERANCE["vwap_bps"])
        if biggest[1]:
            worst.append({"t": t, "field": NAMES[biggest[1]], "owner": x[biggest[1]], "auditor": y[biggest[1]],
                          "bps": round(biggest[0], 2)})
    worst = sorted(worst, key=lambda w: -abs(w["bps"]))[:5]

    # Timing: does a shift of one bar line the feeds up better than none? (start- vs end-stamping)
    def agree(shift: int) -> int:
        return sum(1 for t, x in a.items() if (y := b.get(t + shift)) and x.get("c") and y.get("c")
                   and abs(_bps(x["c"], y["c"])) <= TOLERANCE["price_bps"])
    offsets = {s // 1000: agree(s) for s in (-step_ms, 0, step_ms)}
    best = max(offsets, key=lambda s: (offsets[s], s == 0))
    off_grid = sum(1 for t in (*a, *b) if t % 1000 or (step_ms >= 60_000 and t % 60_000))

    share = {k: (sum(v) / len(v) if v else None) for k, v in within.items()}
    missing = (len(a) - len(both) + len(b) - len(both)) / union
    price_share = min(share[f] for f in PRICE_FIELDS if share[f] is not None) if both else 0.0
    vol_share = share["v"] if share["v"] is not None and judge_volume else 1.0
    if best != 0:
        verdict = "fail"
    elif price_share >= PASS["price"] and vol_share >= PASS["volume"] and missing <= PASS["missing"]:
        verdict = "pass"
    elif price_share >= WARN["price"] and vol_share >= WARN["volume"] and missing <= WARN["missing"]:
        verdict = "warn"
    else:
        verdict = "fail"
    return {
        "verdict": verdict, "matched": len(both), "onlyOwner": len(a) - len(both), "onlyAuditor": len(b) - len(both),
        "missingShare": missing, "timing": {"bestShiftSeconds": best, "agreeingBarsByShift": offsets,
                                            "stampsOffTheGrid": off_grid},
        "withinTolerance": share, "box": {k: box(v) for k, v in diffs.items()},
        "units": {"o": "bps", "h": "bps", "l": "bps", "c": "bps", "range": "bps of price", "v": "%", "n": "%", "vw": "bps"},
        "tolerance": TOLERANCE, "thresholds": {"pass": PASS, "warn": WARN}, "worst": worst,
        "volumeJudged": judge_volume,
    }


def _stored(store, source: str, ticker: str, timespan: str, a: int, b: int) -> list[dict]:
    rows = store.conn.execute(
        "SELECT ts, open, high, low, close, volume, vwap, transactions FROM bars WHERE source=? AND ticker=? "
        "AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ? ORDER BY ts", (source, ticker.upper(), timespan, a, b))
    return [dict(zip(("t", "o", "h", "l", "c", "v", "vw", "n"), r)) for r in rows]


def audit_span(auditor, store, ticker: str, timespan: str, a: int, b: int, owner: str, reason: str,
               log=print) -> dict | None:
    """Ask `auditor` (a source) for [a, b) of a series another feed holds and record how they agree.
    Costs the auditor's requests (usually one); stores no bars."""
    from .collector import _guard, invert_bars

    row = next((w for w in store.watchlist(auditor.name) if w["ticker"] == ticker.upper()), None)
    if row is None:
        return None
    lo, hi = datetime.fromtimestamp(a / 1000, ET), datetime.fromtimestamp(b / 1000, ET)
    theirs = []
    try:
        for page in _guard(auditor.bar_pages(row["source_ticker"], lo, hi, timespan, asset=row["asset"])):
            theirs += [x for x in page if a <= int(x["t"]) < b]
    except (NotEntitled, SourceError) as e:
        log(f"audit {ticker}: {auditor.name} could not provide {lo:%Y-%m-%d %H:%M} to {hi:%H:%M} ({e})")
        return None
    if row["invert"]:
        theirs = invert_bars(theirs)
    # Crypto feeds may cover different venues (consolidated vs one exchange): same prices, other volumes.
    report = compare(_stored(store, owner, ticker, timespan, a, b), theirs, STEP_MS.get(timespan, 60_000),
                     judge_volume=row["asset"] != "crypto")
    venues = {f: VENUES.get((f, row["asset"])) for f in (owner, auditor.name)}
    if row["asset"] == "crypto" and venues[owner] != venues[auditor.name]:
        # Not the same market: a consolidated price takes each minute's extremes across exchanges,
        # one exchange's book does not. The verdict stands; this says why highs/lows may differ.
        report["note"] = (f"cross-venue: {owner} is {venues[owner] or 'its own consolidation'}, "
                          f"{auditor.name} is {venues[auditor.name] or 'its own consolidation'}; highs, lows "
                          "and volume differ by design, opens, closes and VWAP should agree")
    origins = {o for src, o, x, y in store.coverage_detail(ticker, timespan) if src == owner and x < b and y > a}
    if "ticks" in origins:  # the stored bars were built here from the owner's ticks
        report["ownerOrigin"] = f"built here from {owner}'s ticks (condition rules applied)"
    result = {"ticker": ticker, "asset": row["asset"], "timespan": timespan, "start_ms": a, "end_ms": b,
              "owner": owner, "auditor": auditor.name, "reason": reason, "verdict": report["verdict"],
              "matched": report["matched"], "only_owner": report["onlyOwner"],
              "only_auditor": report["onlyAuditor"], "report": report}
    result["id"] = store.save_audit(result)
    close = (report.get("box") or {}).get("c") or {}
    log(f"audit {ticker} {lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M} ET: {owner} vs {auditor.name}: {report['verdict']}, "
        f"{report['matched']} bars matched, {report['onlyOwner']}+{report['onlyAuditor']} in one feed only"
        + (f", close median {close['median']:+.2f} bps (p95 {close['p95']:+.2f})" if close else ""))
    return result


def _window(me: FeedProfile, asset: str, timespan: str, now: datetime) -> tuple[int, int]:
    from .collector import history_floor

    return day_start_ms(history_floor(me.tier, now.astimezone(ET).date())), frontier_ms(asset, me.tier, now, timespan)


def run_audits(source, store, me: FeedProfile, feeds: dict[str, FeedProfile], now: datetime,
               queued: int = 2, random_count: int = 1, log=print, rng: random.Random | None = None) -> int:
    """Audit other feeds' data with this feed: first spans queued after their downloads, then
    random sessions of their stored history. Only spans this feed can serve on its plan."""
    rng = rng or random.Random()
    mine = {w["ticker"]: w for w in store.watchlist(source.name)}
    done = 0
    for q in store.audit_queue(exclude_owner=me.name):
        if done >= queued:
            break
        w = mine.get(q["ticker"])
        if datetime.fromisoformat(q["queued_at"]).replace(tzinfo=timezone.utc) < now - QUEUE_MAX_AGE:
            store.dequeue_audit(q["ticker"], q["timespan"], q["start_ms"], q["owner"])
            continue
        if w is None or not source.serves(w["asset"]):
            continue
        lo, hi = _window(me, w["asset"], q["timespan"], now)
        if not (lo <= q["start_ms"] and q["end_ms"] <= hi):
            continue  # not on this feed's plan yet (e.g. end of day): a later run
        audit_span(source, store, q["ticker"], q["timespan"], q["start_ms"], q["end_ms"], q["owner"], "after-download", log)
        store.dequeue_audit(q["ticker"], q["timespan"], q["start_ms"], q["owner"])
        done += 1
    for _ in range(random_count):
        pick = random_span(store, source, me, now, rng, mine)
        if pick:
            audit_span(source, store, *pick, "random", log)
            done += 1
    return done


def random_span(store, source, me: FeedProfile, now: datetime, rng: random.Random, mine: dict | None = None):
    """A random session of another feed's stored minute bars that this feed could serve:
    (ticker, timespan, start, end, owner), or None."""
    mine = mine or {w["ticker"]: w for w in store.watchlist(source.name)}
    candidates = []
    for ticker, w in mine.items():
        if w["asset"] == "future" or not source.serves(w["asset"]):
            continue
        lo, hi = _window(me, w["asset"], "minute", now)
        for owner, spans in store.coverage(ticker, "minute").items():
            if owner == me.name:
                continue
            for a, b in spans:
                a, b = max(a, lo), min(b, hi)
                if b - a >= 86_400_000:
                    candidates.append((ticker, w["asset"], owner, a, b))
    rng.shuffle(candidates)
    for ticker, asset, owner, a, b in candidates[:20]:
        for _ in range(10):
            t = a + rng.random() * (b - a)
            d = datetime.fromtimestamp(t / 1000, ET).date()
            for s in freshness.CALENDAR.sessions(asset, d, d):
                s0, s1 = max(ms(s.open), a), min(ms(s.close), b)
                if s1 - s0 >= 3_600_000 and has_open_time(asset, s0, s1):
                    return ticker, "minute", s0, s1, owner
    return None


def manual_day(store, ticker: str, day: date, asset: str) -> tuple[int, int] | None:
    """The session span of one day for `audit --day`."""
    s = freshness.CALENDAR.sessions(asset, day, day)
    return (ms(s[0].open), ms(s[-1].close)) if s else None
