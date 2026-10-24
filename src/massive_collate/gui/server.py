"""Local HTTP server for the chart app. Binds to 127.0.0.1 only and serves data from the local store."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
from datetime import datetime, timedelta, timezone
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import Settings, load_settings
from ..storage import LayoutLimit, Store
from . import activity, astro
from .bars import BarEngine, Style, resample
from ..calc.derivatives import Model
from .stats import details_subset, key_stats

STATIC = Path(__file__).parent / "static"
MAX_LIMIT = 20000
MAX_BODY = 5 * 1024 * 1024
WRITE_HEADER = "X-Massive-Charts"


class ChartApp:
    """The API, independent of HTTP so it can be tested directly."""

    def __init__(self, store: Store, priority: list[str] | None = None, settings: Settings | None = None):
        self.store = store
        self.settings = settings or load_settings(env={}, env_file=None)
        self.engine = BarEngine(store, priority=priority, quality=self._feed_quality)
        self._lock = self.engine.lock

    def _feed_quality(self, feed: str) -> float:
        """A feed's best quality over asset classes (display priority between feeds)."""
        from ..composite import quality_of

        return max((quality_of(self.store, feed, a) for a in ("stock", "crypto", "index", "fx", "future")), default=1.0)

    def models(self, ticker: str, asset: str, sources: list[str]) -> dict | None:
        """The models of an asset traded on several exchanges: the gold-standard reference (one
        exchange), the composite across exchanges, the consolidated tape, and each exchange's quality."""
        from ..composite import exchange_feeds, quality_of, reference_feed
        from ..sources import feed_profiles

        feeds = feed_profiles(self.settings)
        exch = {f: v for f, v in exchange_feeds(feeds, asset).items() if f in sources}
        if len(exch) < 2:
            return None
        consolidated = next((f for f in sources if f in feeds and feeds[f].venues.get(asset) in ("consolidated", "sip")), None)
        with self._lock:
            has_composite = self.store.bar_edges("composite", ticker, "minute") is not None
            exchanges = sorted(({"feed": f, "exchange": v, "quality": round(quality_of(self.store, f, asset), 3)}
                                for f, v in exch.items()), key=lambda x: -x["quality"])
        return {"reference": reference_feed(feeds, asset, set(sources)), "composite": has_composite,
                "consolidated": consolidated, "exchanges": exchanges}

    def symbols(self) -> list[dict]:
        """One entry per ticker, with every feed that has data for it."""
        with self._lock:
            watch = self.store.watchlist()
            stored = self.store.stored_series()
            exchanges = self.store.exchanges()
            snapshot = self.store.daily_snapshot()
            icons = self.store.icon_tickers()
            profiles = self.store.company_profiles()
            members = self.store.constituents("sp500", "S&P 500")
            now = datetime.now(timezone.utc).astimezone(activity.ET)
            states = {(r["ticker"]): r for src in {w["source"] for w in watch}
                      for r in activity.symbol_rows(self.store, self.settings, src, now)}
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        # Futures contracts are stored only as parts of a continuous series (NQ1!): not symbols of their own.
        with self._lock:
            parts = {c["ticker"] for w in watch if w["asset"] == "future"
                     for c in self.store.contracts(w["source"], w["source_ticker"])}
        watched = {w["ticker"] for w in watch}
        tickers = sorted(({t for _, t, _ in stored} - (parts - watched)) | watched)
        out = []
        for ticker in tickers:
            ws = [w for w in watch if w["ticker"] == ticker]
            w = next((x for x in ws if x.get("name")), ws[0] if ws else {})
            mic = next((x["exchange"] for x in ws if x.get("exchange")), None)
            feeds = [
                {"source": s, "data": sorted(k for s2, t2, k in stored if s2 == s and t2 == ticker)}
                for s in self.engine.sources_for(ticker)
            ]
            out.append({
                "ticker": ticker, "name": w.get("name"), "kind": w.get("kind") or w.get("asset") or "stock",
                "asset": w.get("asset") or "stock",
                "exchange": mic, "exchangeName": names.get(mic, mic) if mic else None,
                "listed": str(w["listed_from"]) if w.get("listed_from") else None,
                "feeds": feeds,
                "models": self.models(ticker, w.get("asset") or "stock", [f["source"] for f in feeds]),
                "icon": ticker in icons,
                "market": _market(snapshot, ticker, w),
                **_sector(ticker, profiles.get(ticker), w.get("asset") or "stock", (snapshot.get(ticker) or {}).get("close"),
                          members.get(ticker)),
                "changePct": _change(snapshot, ticker),
                "data": {k: states[ticker][k] for k in ("state", "progress", "last", "expectedThrough", "nextDue")}
                if ticker in states else None,
            })
        return out

    def meta(self, source: str, ticker: str, adjust: str = "none") -> dict:
        s = self.engine.series(source, ticker, _adjust(adjust))
        if s is None or s.frame.empty:
            raise LookupError(f"no data stored for {source}:{ticker}")
        first, last = s.frame.index[0], s.frame.index[-1]
        return {
            "source": source, "ticker": s.ticker, "base": s.kind, "hasDaily": s.daily is not None,
            "asset": s.asset, "aroundTheClock": s.around_the_clock, "rolls": s.rolls, "adjust": s.adjust,
            "feeds": self.engine.sources_for(ticker) if source == "*" else [source],
            "pricePrecision": s.price_precision, "volumePrecision": s.volume_precision,
            "periods": self.engine.periods(s), "first": first.isoformat(), "last": last.isoformat(),
            "rows": len(s.frame), "attribution": self.attribution(s.ticker, s.kind, s.asset),
        }

    def attribution(self, ticker: str, base: str, asset: str) -> dict:
        """Which feed each span of the series came from (one feed per span): {timespan: [[start
        ms, end ms, feed], ...]} for the base series and the official daily bars."""
        if asset == "future" or base == "trades":
            return {}
        with self._lock:
            out = {}
            for timespan in dict.fromkeys((base, "day")):
                spans = [[a, b, src] for src, ss in self.store.coverage(ticker, timespan).items() for a, b in ss]
                out[timespan] = sorted(spans)
            return out

    def bars(self, q: dict[str, str]) -> dict:
        limit = min(int(q.get("limit", 1000)), MAX_LIMIT)
        style = Style(
            type=q.get("type", "candles"),
            box=float(q["box"]) if q.get("box") else None,
            reversal=float(q["reversal"]) if q.get("reversal") else None,
            lines=int(q.get("lines", 3)),
        )
        if (style.box is not None and style.box <= 0) or (style.reversal is not None and style.reversal <= 0) \
                or not 1 <= style.lines <= 10:
            raise ValueError("box, reversal and lines must be positive (lines 1-10)")
        return self.engine.bars(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
            q.get("session", "regular"),
            to_ms=int(q["to"]) if q.get("to") else None,
            since_ms=int(q["since"]) if q.get("since") else None,
            limit=limit, style=style, adjust=_adjust(q.get("adjust")), lod=int(q.get("lod", 1)), model=_model(q),
        )

    def info(self, source: str, ticker: str, adjust: str = "none") -> dict:
        """Side panel: symbol, business details, key statistics and data coverage."""
        s = self.engine.series(source, ticker, _adjust(adjust))
        if s is None or s.frame.empty:
            raise LookupError(f"no data stored for {source}:{ticker}")
        feeds = self.engine.sources_for(s.ticker) if source == "*" else [source]
        with self._lock:
            watch = next((w for w in self.store.watchlist() if w["ticker"] == s.ticker and w["source"] in feeds), {})
            details = next((d for d in (self.store.details(f, s.ticker) for f in feeds) if d), None)
            exchanges = self.store.exchanges()
        daily = s.daily if s.daily is not None and not s.daily.empty else resample(s.frame, "day", 1, "extended")
        mic = watch.get("exchange") or (details or {}).get("primary_exchange")
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        out = {
            "symbol": {
                "ticker": s.ticker, "name": watch.get("name") or (details or {}).get("name"),
                "asset": s.asset, "kind": watch.get("kind"), "exchange": mic, "exchangeName": names.get(mic, mic),
                "sourceTicker": watch.get("source_ticker"), "inverted": bool(watch.get("invert")),
                "listed": str(watch["listed_from"]) if watch.get("listed_from") else None,
                "figi": watch.get("figi"),
            },
            "details": details_subset(details),
            "detailsUpdated": (details or {}).get("_updated_at"),
            "stats": key_stats(daily),
            "attribution": self.attribution(s.ticker, s.kind, s.asset),
            "audits": self.audit_summary(s.ticker),
            "data": {
                "feeds": feeds, "base": s.kind, "rows": int(len(s.frame)),
                "first": s.frame.index[0].isoformat(), "last": s.frame.index[-1].isoformat(),
                "officialDaily": s.daily is not None, "pricePrecision": s.price_precision,
            },
        }
        if s.asset == "future" and s.rolls:
            out["futures"] = {"contract": s.rolls[-1]["to"], "rolls": len(s.rolls), "lastRoll": s.rolls[-1],
                              "adjust": s.adjust}
        return out

    def markets(self) -> dict:
        from . import world
        from .markets import markets

        with self._lock:
            out = markets(self.engine.calendar(), self.store.exchanges(), self.store.watchlist())
        with self._lock:
            moves = world.market_moves(self.store)
        out["world"] = world.world_markets(moves=moves)
        out["sun"] = world.subsolar()
        out["sky"] = astro.sky()
        return out

    def deps(self) -> dict:
        from .. import deps

        with self._lock:
            return {"checks": deps.as_rows(deps.checks(self.store, self.settings))}

    def directory(self) -> dict:
        """Every listing in the directory (placeholders), with what is known locally."""
        with self._lock:
            rows = self.store.directory()
            watched = {w["ticker"] for w in self.store.watchlist()}
            profiles = self.store.company_profiles()
            members = self.store.constituents("sp500", "S&P 500")
            changes = self.store.snapshot_changes()
        out = []
        for r in rows:
            dv = (r["close"] or 0) * (r["volume"] or 0) if r["close"] and r["volume"] else None
            out.append({"ticker": r["ticker"], "kind": r["kind"], "name": r["name"], "exchange": r["exchange"],
                        "type": r["type"], "close": r["close"], "dollarVolume": dv, "icon": bool(r["icon"]),
                        "watched": r["ticker"] in watched, "changePct": changes.get(r["ticker"]),
                        **_sector(r["ticker"], profiles.get(r["ticker"]), "future" if r["kind"] == "future" else "stock",
                                  r["close"], members.get(r["ticker"]))})
        return {"items": out}

    def watch(self, ticker: str) -> dict:
        """Add a directory listing to the watchlist (the fetcher then collects it)."""
        from ..symbols import resolve

        with self._lock:
            row = next((r for r in self.store.directory() if r["ticker"] == ticker.upper()), None)
            if row is None:
                raise LookupError(f"{ticker} is not in the directory")
            spec = resolve(f"{row['ticker']}::{row['exchange']}" if row["kind"] == "stock" and row["exchange"] else row["ticker"])
            from ..sources import BUILDERS, supports

            for feed in BUILDERS:  # each span is still downloaded by one feed only
                if supports(feed, spec):
                    self.store.watch_spec(feed, spec)
        return {"watched": spec.ticker}

    def announcements(self) -> dict:
        from .announcements import announcements

        with self._lock:
            watch = self.store.watchlist()
            return announcements(self.store, watch)

    def calendar(self, q: dict[str, str]) -> dict:
        """Sessions of a symbol between two times (ms), for annotating the chart."""
        from .markets import symbol_sessions

        with self._lock:
            item = next((w for w in self.store.watchlist() if w["ticker"] == q["ticker"].upper()), None)
            s = self.engine.series(q.get("source", "*"), q["ticker"])
        asset = s.asset if s else (item or {}).get("asset", "stock")
        product = item["source_ticker"] if item and item["asset"] == "future" else None
        start = datetime.fromtimestamp(int(q["from"]) / 1000, timezone.utc)
        end = datetime.fromtimestamp(int(q["to"]) / 1000, timezone.utc)
        if end - start > timedelta(days=3700):
            raise ValueError("calendar range is limited to 10 years")
        return {"asset": asset, "sessions": symbol_sessions(self.engine.calendar(), asset, product, start, end)}

    def future(self, q: dict[str, str]) -> dict:
        style = Style(type=q.get("type", "candles"))
        return self.engine.future(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)), q.get("session", "regular"),
            style=style, adjust=_adjust(q.get("adjust")), lod=int(q.get("lod", 1)), model=_model(q),
            offset=max(0, int(q.get("offset", 0))), count=min(int(q.get("count", 500)), 5000),
        )

    def range_stats(self, q: dict[str, str]) -> dict:
        return self.engine.range_stats(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
            q.get("session", "regular"), int(q["from"]), int(q["to"]), adjust=_adjust(q.get("adjust")),
            model=_model(q),
        )

    # -- layouts (saved markups) ----------------------------------------------

    def layouts(self) -> dict:
        with self._lock:
            return {"layouts": self.store.list_layouts(), "max": self.store.MAX_LAYOUTS}

    def layout(self, layout_id: int) -> dict:
        with self._lock:
            found = self.store.get_layout(layout_id)
        if found is None:
            raise LookupError(f"no layout {layout_id}")
        return found

    def create_layout(self, body: dict) -> dict:
        data = body.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        with self._lock:
            return self.store.create_layout(str(body.get("name", "")), data)

    def update_layout(self, layout_id: int, body: dict) -> dict:
        data = body.get("data")
        if data is not None and not isinstance(data, dict):
            raise ValueError("data must be an object")
        with self._lock:
            found = self.store.update_layout(layout_id, body.get("name"), data)
        if found is None:
            raise LookupError(f"no layout {layout_id}")
        return found

    def delete_layout(self, layout_id: int) -> dict:
        with self._lock:
            if not self.store.delete_layout(layout_id):
                raise LookupError(f"no layout {layout_id}")
        return {"deleted": layout_id}

    def activity(self, q: dict[str, str]) -> dict:
        with self._lock:
            out = activity.activity(self.store, self.settings, self.store.path, q.get("source", "massive"),
                                    log_lines=min(int(q.get("lines", 200)), 1000))
        out["feeds"] = self.feeds()
        return out

    def datasets(self) -> dict:
        """The research datasets: what is held, its span, files fetched and closed, quality."""
        from .. import datasets

        with self._lock:
            rows = datasets.status(self.store)
        for r in rows:
            r["fetched"] = r["fetched"].isoformat() if r["fetched"] else None
        return {"datasets": rows}

    def dataset_series(self, name: str) -> dict:
        """A dataset's series (or, for the COT report, its markets) for the viewer's picker."""
        from .. import datasets

        if name not in datasets.DATASETS:
            raise LookupError(f"no dataset {name}")
        ds = datasets.DATASETS[name]
        with self._lock:
            if name == "cftc-cot-legacy":
                return {"name": name, "markets": datasets.cot_markets(self.store)}
            return {"name": name, "series": datasets.series_list(self.store, name),
                    "members": len(self.store.constituents(ds.provider, "S&P 500"))}

    def dataset_values(self, name: str, series: str) -> dict:
        from .. import datasets

        if name not in datasets.DATASETS:
            raise LookupError(f"no dataset {name}")
        wanted = [s for s in series.split(",") if s]
        if not wanted:
            raise ValueError("no series given")
        with self._lock:
            return {"name": name, "series": datasets.values(self.store, name, wanted)}

    def audit_summary(self, ticker: str | None = None, limit: int = 40) -> dict:
        with self._lock:
            rows = self.store.audits(limit, ticker)
            return {"audits": rows, "scores": self.store.audit_scores(), "queued": len(self.store.audit_queue()),
                    "quality": self.store.qualities()}

    def feeds(self) -> list[dict]:
        from ..sources import ARCHIVAL, BUILDERS, feed_profiles

        profiles = feed_profiles(self.settings)
        with self._lock:
            watch = self.store.watchlist()
        out = []
        for name in BUILDERS:
            p, s = profiles.get(name), self.settings.for_source(name)
            keys = (self.settings.alpaca.has_keys if name == "alpaca" and self.settings.alpaca
                    else bool(self.settings.tiingo_api_key) if name == "tiingo" else None)
            rows = [w for w in watch if w["source"] == name]
            closed = name in ARCHIVAL and all(w.get("closed_at") for w in rows)
            out.append({"name": name, "configured": p is not None, "assets": sorted(p.assets) if p else [],
                        "plan": s.tier.name, "keys": keys, "archive": name in ARCHIVAL, "closed": closed})
        return out

    def icon(self, ticker: str) -> tuple[str, bytes] | None:
        with self._lock:
            return self.store.icon(ticker)

    def start_scan(self, ticker: str | None = None) -> dict:
        """Run `scan --probe --fetch` (for one ticker, or every watched symbol) in its own
        process: it takes the run lock and the plan caps like a scheduled sync, and shows up in
        the activity view and the status bar while it runs."""
        if activity.lock_owner(self.store.path):
            raise RuntimeError("a run is already in progress")
        args = [sys.executable, "-m", "massive_collate", "scan", "--probe", "--fetch", "--all-sources"]
        if ticker:
            with self._lock:
                watched = {w["ticker"] for w in self.store.watchlist()}
            if ticker.upper() not in watched:
                raise LookupError(f"{ticker} is not on the watchlist")
            args += ["--only", ticker.upper()]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        self._scan = proc  # keep a reference so it is reaped
        return {"started": True, "pid": proc.pid}

    def exchanges(self) -> list[dict]:
        with self._lock:
            df = self.store.exchanges()
        return json.loads(df.to_json(orient="records"))


def _sector(ticker: str, profile: dict | None, asset: str, close: float | None = None,
            member: dict | None = None) -> dict:
    """Sector, industry and market cap for the market map (stocks with reference data). The cap is
    this share class's own (its shares outstanding x price) where known: a company with two listed
    classes (GOOG, GOOGL) would otherwise be drawn twice its size. An S&P 500 member takes its
    sector from the index list's GICS classification; its industry keeps the map's own name unless
    that belongs to another sector, when the GICS sub-industry is used."""
    from .sectors import SECTORS, classify

    profile = profile or {}
    if asset != "stock" or not (profile or member):
        return {"sector": None, "industry": None, "marketCap": None}
    sector, industry = classify(ticker, profile.get("sic_code"), profile.get("sic_description"))
    basis = "SIC"
    if member and member.get("sector") in SECTORS:
        if member["sector"] != sector:
            industry = member.get("industry") or industry
        sector, basis = member["sector"], "GICS (S&P 500 list)"
    shares = profile.get("class_shares")
    cap = shares * close if shares and close else profile.get("market_cap")
    return {"sector": sector, "industry": industry, "sectorBasis": basis, "marketCap": cap,
            "companyCap": profile.get("market_cap")}


def _change(snapshot: dict, ticker: str) -> float | None:
    """Last session's change in percent from local daily bars (any asset class)."""
    d = snapshot.get(ticker)
    return (d["close"] / d["prevClose"] - 1) * 100 if d and d.get("prevClose") else None


def _market(snapshot: dict, ticker: str, watch: dict) -> dict | None:
    """Last close, change and 30-session average dollar volume from local daily bars."""
    d = snapshot.get(ticker)
    if not d or watch.get("asset", "stock") != "stock":
        return None  # dollar volume only means something for shares
    change = (d["close"] / d["prevClose"] - 1) * 100 if d["prevClose"] else None
    return {"close": d["close"], "changePct": change, "dollarVolume": d["dollarVolume"]}


def _model(q: dict[str, str]) -> Model:
    """Model from query parameters: model, basis, per, window, order, gaps."""
    return Model(
        kind=q.get("model", "price"), basis=q.get("basis", "price"), per=q.get("per", "hour"),
        window=int(q.get("window", 9)), order=int(q.get("order", 3)), reset_gaps=q.get("gaps", "1") != "0",
    ).validate()


def _adjust(value: str | None) -> str:
    value = value or "none"
    if value not in ("none", "difference", "ratio"):
        raise ValueError("adjust must be none, difference or ratio")
    return value


def make_handler(app: ChartApp, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "massive-chart"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, allow_nan=False).encode(), "application/json")

        def _local(self) -> bool:
            # Only answer requests addressed to this machine (blocks DNS-rebinding from web pages).
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return False
            return True

        def _write(self, method: str) -> None:
            """POST/PUT/DELETE for layouts. The custom header can't be sent cross-site without a
            CORS preflight, which this server never grants, so other web pages can't write."""
            if not self._local():
                return
            if self.headers.get(WRITE_HEADER) != "1":
                return self._send(HTTPStatus.FORBIDDEN, b"missing request header", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if method != "DELETE" else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                parts = urlparse(self.path).path.rstrip("/").split("/")
                if method == "POST" and parts == ["", "api", "watch"]:
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1]
                    if not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    return self._json(app.watch(ticker), HTTPStatus.CREATED)
                if method == "POST" and parts in (["", "api", "scan"], ["", "api", "fetch"]):
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1] if parts[2] == "fetch" else None
                    if parts[2] == "fetch" and not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    try:
                        return self._json(app.start_scan(ticker), HTTPStatus.ACCEPTED)
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
                if parts[:3] != ["", "api", "layouts"]:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                if method == "POST" and len(parts) == 3:
                    return self._json(app.create_layout(body), HTTPStatus.CREATED)
                if len(parts) == 4:
                    layout_id = int(parts[3])
                    if method == "PUT":
                        return self._json(app.update_layout(layout_id, body))
                    if method == "DELETE":
                        return self._json(app.delete_layout(layout_id))
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except LayoutLimit as e:
                return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            except LookupError as e:
                return self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            self._write("POST")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

        def do_GET(self) -> None:
            if not self._local():
                return
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/layouts":
                    return self._json(app.layouts())
                if url.path.startswith("/api/layouts/"):
                    return self._json(app.layout(int(url.path.rsplit("/", 1)[1])))
                if url.path == "/api/symbols":
                    return self._json(app.symbols())
                if url.path == "/api/meta":
                    return self._json(app.meta(q["source"], q["ticker"], q.get("adjust")))
                if url.path == "/api/bars":
                    return self._json(app.bars(q))
                if url.path == "/api/info":
                    return self._json(app.info(q["source"], q["ticker"], q.get("adjust")))
                if url.path == "/api/deps":
                    return self._json(app.deps())
                if url.path == "/api/directory":
                    return self._json(app.directory())
                if url.path == "/api/announcements":
                    return self._json(app.announcements())
                if url.path == "/api/markets":
                    return self._json(app.markets())
                if url.path == "/api/calendar":
                    return self._json(app.calendar(q))
                if url.path == "/api/future":
                    return self._json(app.future(q))
                if url.path == "/api/range":
                    return self._json(app.range_stats(q))
                if url.path == "/api/icon":
                    icon = app.icon(q["ticker"])
                    if icon is None:
                        return self._send(HTTPStatus.NOT_FOUND, b"no icon", "text/plain")
                    mime, data = icon
                    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                        return self._send(HTTPStatus.NOT_FOUND, b"no icon", "text/plain")  # no SVG: it can carry script
                    return self._send(HTTPStatus.OK, data, mime)
                if url.path == "/api/activity":
                    return self._json(app.activity(q))
                if url.path == "/api/exchanges":
                    return self._json(app.exchanges())
                if url.path == "/api/audits":
                    return self._json(app.audit_summary(q.get("ticker")))
                if url.path == "/api/datasets":
                    return self._json(app.datasets())
                if url.path == "/api/dataset/series":
                    return self._json(app.dataset_series(q["name"]))
                if url.path == "/api/dataset/values":
                    return self._json(app.dataset_values(q["name"], q["series"]))
            except (KeyError, ValueError) as e:
                return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            except LookupError as e:
                return self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            return self._static(url.path)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    return Handler


def serve(db_path: Path, port: int = 8765, open_browser: bool = True, priority: list[str] | None = None,
          settings: Settings | None = None) -> None:
    store = Store(db_path, check_same_thread=False)
    app = ChartApp(store, priority, settings)
    from . import world
    threading.Thread(target=world.warm, daemon=True).start()  # world calendars take a few seconds
    port_ref = [port]
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, port_ref))  # port taken: pick a free one
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"chart app running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
