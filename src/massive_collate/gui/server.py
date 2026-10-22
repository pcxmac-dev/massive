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

from ..storage import Store
from .bars import BarEngine, Style

STATIC = Path(__file__).parent / "static"
MAX_LIMIT = 20000


class ChartApp:
    """The API, independent of HTTP so it can be tested directly."""

    def __init__(self, store: Store, priority: list[str] | None = None):
        self.store = store
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
            limit=limit, style=style, adjust=_adjust(q.get("adjust")),
        )

    def range_stats(self, q: dict[str, str]) -> dict:
        return self.engine.range_stats(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
            q.get("session", "regular"), int(q["from"]), int(q["to"]), adjust=_adjust(q.get("adjust")),
        )

    def exchanges(self) -> list[dict]:
        with self._lock:
            df = self.store.exchanges()
        return json.loads(df.to_json(orient="records"))


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

        def do_GET(self) -> None:
            # Only answer requests addressed to this machine (blocks DNS-rebinding from web pages).
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/symbols":
                    return self._json(app.symbols())
                if url.path == "/api/meta":
                    return self._json(app.meta(q["source"], q["ticker"], q.get("adjust")))
                if url.path == "/api/bars":
                    return self._json(app.bars(q))
                if url.path == "/api/range":
                    return self._json(app.range_stats(q))
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


def serve(db_path: Path, port: int = 8765, open_browser: bool = True, priority: list[str] | None = None) -> None:
    store = Store(db_path, check_same_thread=False)
    app = ChartApp(store, priority)
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
