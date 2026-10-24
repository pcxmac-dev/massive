"""Composite and reference models of an asset that trades on several exchanges, and quality scores.

Exchanges differ by a little (fees, who trades there, a premium venue); one consolidated tape hides
that, and the extremes of a consolidation are set by whichever exchange printed furthest out. So
each such asset gets two models next to the feeds' own data:

- reference: one exchange's data, the gold standard (`REFERENCE`): Coinbase for crypto; for a US
  stock its primary listing exchange (its opening and closing auctions are the official prices),
  built from exchange-tagged consolidated ticks when they are stored.
- composite: per bar, every exchange's bar combined - open, high, low and close as the mean
  weighted by volume x quality, volume and trades summed, VWAP across exchanges - with the
  deviation across exchanges: the weighted standard deviation of the closes (bps), the spread of
  the closes, the envelope (highest high, lowest low) and which exchanges took part or stood out.

Quality (0-1) says how far a source can be trusted for display and reporting. For an exchange it is
measured: each exchange against the composite of the others (leave-one-out) - median distance of
its close, high and low, the share of outlier bars, how complete it is. An outlier exchange (a
premium venue) scores lower, so it weighs less in the composite and ranks lower for display. Other
sources start from a prior by provenance (`PRIORS`) until audits measure them.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

DAY_MS = 86_400_000
OUTLIER_BPS = 10.0  # a bar this far from the other exchanges' composite (and > 3 sd) stands out
QUALITY_SCALE_BPS = 10.0  # median distance at which quality falls to 1/e
QUALITY_DAYS = 30  # bars of the trailing window that set an exchange's quality

# Gold standard per asset class: one exchange.
REFERENCE = {"crypto": "coinbase"}
# Quality before anything is measured, by provenance.
PRIORS = {"exchange": 1.0, "official": 1.0, "consolidated": 0.9, "vendor": 0.9, "research": 0.85, "mirror": 0.8}
FEED_KIND = {"massive": "consolidated", "alpaca": "exchange", "coinbase": "exchange", "bitfinex": "exchange",
             "bitstamp": "exchange", "cboe": "official", "tiingo": "vendor", "composite": "consolidated"}
# Where a feed's kind depends on the asset class: Alpaca's stocks are the consolidated tape (SIP).
KIND_BY_ASSET = {("alpaca", "stock"): "consolidated"}


def exchange_feeds(feeds: dict, asset: str) -> dict[str, str]:
    """{feed: exchange} of the feeds whose data of `asset` is one exchange's (not a consolidation)."""
    out = {}
    for name, p in feeds.items():
        venue = p.venues.get(asset)
        if venue and venue not in ("sip", "consolidated", "otc", "index", "cme", "iex") and asset in p.assets:
            out[name] = venue
    return out


def reference_feed(feeds: dict, asset: str, watched: set[str]) -> str | None:
    """The feed holding the gold-standard exchange for an asset class, if it is watched here."""
    ref = REFERENCE.get(asset)
    return ref if ref in watched else None


def quality_of(store, subject: str, asset: str) -> float:
    """Measured quality, else the prior for the source's kind."""
    q = store.quality(subject, asset)
    if q is not None:
        return q["score"]
    kind = KIND_BY_ASSET.get((subject, asset)) or FEED_KIND.get(subject, "vendor")
    return PRIORS.get(kind, 0.9)


def combine(bars: dict[str, pd.DataFrame], weights: dict[str, float]) -> pd.DataFrame:
    """Composite bars from each exchange's bars (index ts ms; columns o h l c v vw n). Weight per
    bar: volume x the exchange's quality (equal volumes if none)."""
    frames = []
    for venue, df in bars.items():
        if df.empty:
            continue
        frames.append(df.assign(venue=venue, q=weights.get(venue, 1.0)))
    if not frames:
        return pd.DataFrame()
    all_ = pd.concat(frames)
    v = all_["v"].fillna(0.0).clip(lower=0.0)
    all_["w"] = (v.where(v > 0, 1e-9)) * all_["q"]
    all_["px"] = all_["vw"].where(all_["vw"].notna() & (all_["vw"] > 0), all_["c"])
    g = all_.groupby(level=0)
    W = g["w"].sum()
    out = pd.DataFrame({k: (all_[k] * all_["w"]).groupby(level=0).sum() / W for k in ("o", "h", "l", "c")})
    out["v"] = g["v"].sum()
    out["vw"] = (all_["px"] * v).groupby(level=0).sum() / out["v"].where(out["v"] > 0)
    out["n"] = g["n"].sum(min_count=1)
    dev = (all_["c"] - out["c"].reindex(all_.index).to_numpy()) ** 2 * all_["w"]
    out["sd_bps"] = np.sqrt(dev.groupby(level=0).sum() / W) / out["c"] * 1e4
    out["range_bps"] = (g["c"].max() - g["c"].min()) / out["c"] * 1e4
    out["env_h"] = g["h"].max()
    out["env_l"] = g["l"].min()
    out["venues"] = g["venue"].count()
    out["members"] = g["venue"].agg(lambda s: ",".join(sorted(s)))
    # Leave-one-out: each exchange against the others' composite and the others' dispersion (an
    # outlier must not widen its own yardstick). Prices are centred on the bar's composite first, so
    # the sums of squares keep their precision.
    centre = out["c"].reindex(all_.index).to_numpy()
    x = all_["c"].to_numpy() - centre
    w = all_["w"].to_numpy()
    sx = pd.Series(w * x, index=all_.index).groupby(level=0).sum().reindex(all_.index).to_numpy()
    sxx = pd.Series(w * x * x, index=all_.index).groupby(level=0).sum().reindex(all_.index).to_numpy()
    rest_w = W.reindex(all_.index).to_numpy() - w
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_o = np.where(rest_w > 0, (sx - w * x) / rest_w, np.nan)
        var_o = np.where(rest_w > 0, (sxx - w * x * x) / rest_w - mean_o ** 2, np.nan)
        others = centre + mean_o
        dist = (all_["c"].to_numpy() - others) / others * 1e4
        sd_o = np.sqrt(np.clip(var_o, 0, None)) / others * 1e4
    # an outlier needs a majority to stand out from: at least two other exchanges in the bar
    rest_n = g["venue"].count().reindex(all_.index).to_numpy() - 1
    flag = (np.abs(dist) > np.maximum(OUTLIER_BPS, 3 * np.nan_to_num(sd_o))) & (rest_n >= 2)
    all_["outlier"] = flag & np.isfinite(dist)
    out["outliers"] = all_[all_["outlier"]].groupby(level=0)["venue"].agg(lambda s: ",".join(sorted(s)))
    out["outliers"] = out["outliers"].fillna("")
    return out


def leave_one_out(bars: dict[str, pd.DataFrame], weights: dict[str, float] | None = None) -> dict[str, dict]:
    """Per exchange: median |close|, |high|, |low| distance (bps) from the median of the other
    exchanges (a median, so one outlier exchange does not drag the others' yardstick), the share of
    bars beyond OUTLIER_BPS and 3 x the others' spread, and completeness (bars present / bars any
    exchange has). Needs at least two other exchanges for a bar to count."""
    venues = [v for v, df in bars.items() if not df.empty]
    if len(venues) < 2:
        return {}
    field = {k: pd.DataFrame({v: bars[v][k] for v in venues}) for k in ("c", "h", "l")}
    stamps = field["c"].index
    out = {}
    for v in venues:
        others = [u for u in venues if u != v]
        ref = {k: field[k][others].median(axis=1, skipna=True) for k in field}
        spread = field["c"][others].std(axis=1, ddof=0) / ref["c"] * 1e4
        enough = field["c"][others].notna().sum(axis=1) >= min(2, len(others))
        mine = field["c"][v].notna() & enough
        if not mine.any():
            continue
        d = {k: ((field[k][v] - ref[k]) / ref[k] * 1e4).abs()[mine] for k in field}
        outl = (d["c"] > np.maximum(OUTLIER_BPS, 3 * spread[mine].fillna(0))).mean()
        out[v] = {"closeBps": float(d["c"].median()), "highBps": float(d["h"].median()), "lowBps": float(d["l"].median()),
                  "outlierShare": float(outl), "completeness": float(bars[v].index.size / max(1, stamps.size)),
                  "bars": int(mine.sum())}
    return out


def score(stats: dict) -> float:
    m = 0.5 * stats["closeBps"] + 0.25 * stats["highBps"] + 0.25 * stats["lowBps"]
    return round(math.exp(-m / QUALITY_SCALE_BPS) * (1 - stats["outlierShare"]) * math.sqrt(stats["completeness"]), 4)


def _venue_bars(store, feed_of: dict[str, str], ticker: str, timespan: str, lo: int, hi: int) -> dict[str, pd.DataFrame]:
    out = {}
    for feed, venue in feed_of.items():
        df = store.bars_window([feed], ticker, timespan, lo, hi)
        if df.empty:
            out[venue] = pd.DataFrame(columns=["o", "h", "l", "c", "v", "vw", "n"])
            continue
        ts = df["ts"].astype("int64").to_numpy()
        out[venue] = pd.DataFrame({"o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy(),
                                   "c": df["close"].to_numpy(), "v": df["volume"].to_numpy(),
                                   "vw": df["vwap"].to_numpy(), "n": df["transactions"].to_numpy()}, index=ts)
    return out


def refresh(store, feeds: dict, now: datetime, log=print, rebuild_days: int = 2) -> dict:
    """Bring every multi-exchange asset's composite up to date (and the exchanges' quality, daily).
    Only the last `rebuild_days` are rebuilt once a composite exists (late bars, quality changes)."""
    watched: dict[str, set] = {}
    for w in store.watchlist():
        watched.setdefault((w["ticker"], w["asset"]), set()).add(w["source"])
    done = {}
    for (ticker, asset), sources in sorted(watched.items()):
        feed_of = {f: v for f, v in exchange_feeds(feeds, asset).items() if f in sources}
        if len(feed_of) < 2:
            continue
        weights = {v: quality_of(store, f, asset) for f, v in feed_of.items()}
        built = 0
        if not store.kv_get("composite:single-dropped:v1"):  # one-off: bars built from one exchange only
            store.drop_single_exchange_composites()
            store.kv_set("composite:single-dropped:v1", now.isoformat())
        for timespan in ("minute", "day"):
            edges = {f: e for f in feed_of if (e := store.bar_edges(f, ticker, timespan))}
            if len(edges) < 2:
                continue
            first, last = min(e[0] for e in edges.values()), max(e[1] for e in edges.values())
            # Rebuild the last few days, and from wherever an exchange's data now reaches further
            # back than at the last build (a new exchange, a backfill); everything the first time.
            key = f"composite:edges:{ticker}:{timespan}"
            before = json.loads(store.kv_get(key) or "{}")
            have = store.bar_edges("composite", ticker, timespan)
            lo = have[1] - rebuild_days * DAY_MS if have else first
            for f, (a, _) in edges.items():
                if f not in before or a < before[f][0]:
                    lo = min(lo, a)
            lo = max(first, lo) // DAY_MS * DAY_MS
            while lo <= last:
                hi = lo + (30 if timespan == "day" else 1) * DAY_MS
                comp = combine(_venue_bars(store, feed_of, ticker, timespan, lo, hi), weights)
                if not comp.empty:
                    comp = comp[comp["venues"] >= 2]  # one exchange alone is that exchange, not a composite
                if not comp.empty:
                    built += store.save_composite(ticker, timespan, comp)
                lo = hi
            store.kv_set(key, json.dumps({f: list(e) for f, e in edges.items()}))
        key = f"quality:{ticker}"
        last_q = store.kv_get(key)
        if built or not last_q or (now - datetime.fromisoformat(last_q)).total_seconds() > 86_400:
            hi = int(now.timestamp() * 1000)
            stats = leave_one_out(_venue_bars(store, feed_of, ticker, "minute", hi - QUALITY_DAYS * DAY_MS, hi), weights)
            for f, v in feed_of.items():
                if v in stats:
                    store.set_quality(f, asset, score(stats[v]), {**stats[v], "ticker": ticker, "basis": "leave-one-out"})
            store.kv_set(key, now.isoformat())
            if stats:
                log(f"{ticker}: exchange quality " + ", ".join(
                    f"{v} {score(s):.2f} (close {s['closeBps']:.1f} bps, outliers {s['outlierShare']:.0%})"
                    for v, s in sorted(stats.items())))
        if built:
            log(f"{ticker}: composite of {len(feed_of)} exchanges, {built:,} bars")
        done[ticker] = built
    return done
