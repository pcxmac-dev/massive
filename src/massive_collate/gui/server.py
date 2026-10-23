"""Local HTTP server for the chart app. Binds to 127.0.0.1 only and serves data from the local store."""
from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import Settings, load_settings
from ..storage import LayoutLimit, Store
from . import activity
from .bars import BarEngine, Style, resample
from .models import Model
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
        self.engine = BarEngine(store, priority=priority)
        self._lock = self.engine.lock

    def symbols(self) -> list[dict]:
        """One entry per ticker, with every feed that has data for it."""
        with self._lock:
            watch = self.store.watchlist()
            stored = self.store.stored_series()
            exchanges = self.store.exchanges()
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        tickers = sorted({t for _, t, _ in stored} | {w["ticker"] for w in watch})
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
            "rows": len(s.frame),
        }

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
            return activity.activity(self.store, self.settings, self.store.path, q.get("source", "massive"),
                                     log_lines=min(int(q.get("lines", 200)), 1000))

    def exchanges(self) -> list[dict]:
        with self._lock:
            df = self.store.exchanges()
        return json.loads(df.to_json(orient="records"))


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
                if url.path == "/api/range":
                    return self._json(app.range_stats(q))
                if url.path == "/api/activity":
                    return self._json(app.activity(q))
                if url.path == "/api/exchanges":
                    return self._json(app.exchanges())
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
