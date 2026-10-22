"""Command-line interface: `python -m massive_collate <command>`."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from . import analysis, charts, collector
from .config import ConfigError, Settings, load_settings
from .errors import SourceError
from .symbols import resolve
from .sources import BUILDERS, DataSource, build_source
from .storage import Store

TASK_NAME = "MassiveCollateSync"
LOCK_STALE_SECONDS = 6 * 3600


# -- helpers -------------------------------------------------------------------

class Logger:
    """Print and append to a log file next to the database."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        print(msg, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


@contextmanager
def run_lock(db_path: Path) -> Iterator[None]:
    """Only one downloading run at a time, so two runs can never double the request rate."""
    lock = db_path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"another run is in progress (lock file {lock}); exiting") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _budget(settings: Settings, store: Store, source: str) -> int:
    left_today = settings.max_requests_per_day - store.requests_today(source)
    return max(0, min(settings.max_requests_per_run, left_today))


@contextmanager
def api_run(args, settings: Settings, command: str) -> Iterator[tuple[DataSource, Store, Logger]]:
    """Open the store, take the lock, build a source with a hard request cap, and audit the run."""
    log = Logger(settings.db_path.parent / "logs" / "massive-collate.log")
    with Store(settings.db_path) as store, run_lock(settings.db_path):
        budget = _budget(settings, store, args.source)
        if budget == 0:
            log(f"daily request cap of {settings.max_requests_per_day} reached for {args.source}; nothing sent")
            yield None, store, log  # type: ignore[misc]
            return
        source = build_source(args.source, settings, request_budget=budget)
        run_id = store.start_run(args.source, command)
        log(f"{command}: source={args.source} tier={settings.tier.name} "
            f"rate={settings.tier.requests_per_minute or 'unlimited'}/min cap={budget} requests")
        status, message = "ok", ""
        try:
            yield source, store, log
        except collector.StopRun as e:
            status, message = "stopped", str(e)
            log(f"stopped: {e}")
        except SourceError as e:
            status, message = "error", str(e)
            log(f"error: {e}")
        except Exception as e:
            status, message = "error", repr(e)
            raise
        finally:
            store.finish_run(run_id, source.requests_made, getattr(args, "_rows", 0), status, message)
            log(f"{command}: {source.requests_made} requests used")


# -- commands ------------------------------------------------------------------

def _watch_line(w: dict) -> str:
    symbol = f"{w['ticker']}::{w['exchange']}" if w["exchange"] else w["ticker"]
    via = w["source_ticker"] + (" (inverted)" if w["invert"] else "") if w["source_ticker"] != w["ticker"] else ""
    return (f"{w['source']:8} {symbol:14} {w['asset']:6} {via:20} {w['resolution']:8} "
            f"listed: {str(w['listed_from'] or '?'):10}  backfilled to: {w['backfilled_to'] or '-'}")


def cmd_watch(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        for text in args.tickers:
            spec = resolve(text)
            if args.action == "add":
                store.watch_spec(args.source, spec, args.resolution)
            elif args.action == "remove":
                store.unwatch(args.source, [spec.ticker])
        for w in store.watchlist(args.source):
            print(_watch_line(w))
    return 0


def cmd_sync(args, settings: Settings) -> int:
    """Sync every configured feed (or just --source), each within its own limits."""
    names = list(BUILDERS) if args.all_sources else [args.source]
    ran, errors = 0, []
    for name in names:
        args.source = name
        try:
            with api_run(args, settings, "sync") as (source, store, log):
                if source is None:
                    continue
                result = collector.sync(source, store, settings.tier, log=log)
                args._rows = result.rows
                log(f"sync {name}: {result.rows} rows saved"
                    + (f" (stopped: {result.stopped})" if result.stopped else ""))
            ran += 1
        except ConfigError as e:
            errors.append(f"{name}: {e}")  # feed not configured on this machine
    if not ran and errors:
        raise ConfigError("; ".join(errors))
    for e in errors:
        print(f"skipped {e}", file=sys.stderr)
    return 0


def cmd_gui(args, settings: Settings) -> int:
    from .gui.server import serve

    serve(settings.db_path, port=args.port, open_browser=not args.no_browser,
          priority=list(settings.source_priority))
    return 0


def cmd_probe(args, settings: Settings) -> int:
    with api_run(args, settings, f"probe {args.ticker}") as (source, store, log):
        if source is None:
            return 0
        spec = resolve(args.ticker)
        if args.watch:
            store.watch_spec(args.source, spec)
        if spec.asset == "future":
            log(f"{spec.ticker}: continuous futures are resolved by `sync` (it needs the current contract)")
            return 0
        collector.resolve_resolution(source, store, spec.ticker, log, asset=spec.asset, fetch=spec.source_ticker)
    return 0


def cmd_download(args, settings: Settings) -> int:
    with api_run(args, settings, f"download {' '.join(args.tickers)}") as (source, store, log):
        if source is None:
            return 0
        rows = 0
        for t in args.tickers:
            rows += collector.download_bars(
                source, store, settings.tier, t.upper(), args.start, args.end, args.timespan, args.multiplier, log
            )
        args._rows = rows
    return 0


def cmd_market_daily(args, settings: Settings) -> int:
    with api_run(args, settings, "market-daily") as (source, store, log):
        if source is None:
            return 0
        result = collector.sync_market_daily(source, store, settings.tier, log=log)
        args._rows = result.rows
    return 0


def cmd_exchanges(args, settings: Settings) -> int:
    if args.refresh:
        with api_run(args, settings, "exchanges") as (source, store, log):
            if source is not None:
                args._rows = store.upsert_exchanges(source.exchanges())
    with Store(settings.db_path) as store:
        df = store.exchanges(args.source)
        print(df.to_string(index=False) if not df.empty else "no exchanges stored yet; run with --refresh")
    return 0


def cmd_info(args, settings: Settings) -> int:
    with api_run(args, settings, f"info {args.ticker}") as (source, store, log):
        if source is not None:
            inst = source.instrument(args.ticker)
            print(inst.describe())
            print(f"listed: {inst.list_date or '?'}  active: {inst.active}  FIGI: {inst.figi or '?'}")
            if inst.country:
                print(f"country: {inst.country.name} ({inst.country.code}), timezone {inst.country.timezone}")
    return 0


def cmd_status(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        s = store.summary()
        print(f"database: {s['db_path']} ({s['size_bytes'] / 1e6:.1f} MB)")
        print(f"plan: {settings.tier.name}, {settings.tier.requests_per_minute or 'unlimited'} requests/min, "
              f"history {settings.tier.history_years or 'unlimited'} years")
        print(f"caps: {settings.max_requests_per_run}/run, {settings.max_requests_per_day}/day")
        for src in BUILDERS:
            print(f"requests today ({src}): {store.requests_today(src)}")
        print("\nwatchlist:")
        for w in store.watchlist():
            print("  " + _watch_line(w))
        print("\nticks:")
        for src, t, n, lo, hi in s["trades"]:
            print(f"  {src:8} {t:8} {n:>12,} trades  {_ns(lo)} -> {_ns(hi)}")
        print("\nbars:")
        for src, ts, mult, tickers, n, lo, hi in s["bars"]:
            print(f"  {src:8} {mult}/{ts:7} {tickers:>6} tickers {n:>12,} bars  {_ns(lo * 10**6)} -> {_ns(hi * 10**6)}")
        print("\nrecent runs:")
        for r in store.recent_runs(args.runs):
            print(f"  #{r[0]} {r[3]} {r[2]:24} {r[7]:7} {r[5]} requests, {r[6]} rows {r[8] or ''}")
    return 0


def _ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, collector.ET).strftime("%Y-%m-%d %H:%M:%S")


def cmd_admin(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        if args.action == "check":
            print(f"integrity: {store.integrity_check()}")
        elif args.action == "vacuum":
            with run_lock(settings.db_path):
                store.vacuum()
            print("vacuumed")
        elif args.action == "backup":
            dest = args.dest or settings.db_path.parent / "backups" / f"massive-{datetime.now():%Y%m%d-%H%M%S}.db"
            print(f"backup written to {store.backup(Path(dest))}")
        elif args.action == "reset-denied":
            print(f"cleared {store.clear_denied(args.source)} denied days")
    return 0


def _load_prices(store: Store, args) -> tuple:
    """(bars DataFrame indexed by time, trades DataFrame or None) from the finest stored data."""
    trades = store.load_trades(args.ticker, args.source, args.start, args.end)
    if not trades.empty:
        return analysis.trades_to_bars(trades, args.rule), trades
    for timespan in ("second", "minute", "hour", "day"):
        bars = store.load_bars([args.ticker], timespan, 1, args.start, args.end, args.source)
        if not bars.empty:
            return bars.set_index("time"), None
    raise SystemExit(f"no data stored for {args.ticker}")


def cmd_analyze(args, settings: Settings) -> int:
    import pandas as pd

    with Store(settings.db_path) as store:
        bars, trades = _load_prices(store, args)
    daily = bars["close"].groupby(bars.index.date).last()
    daily.index = pd.to_datetime(daily.index)
    print(f"{args.ticker.upper()} — daily statistics from the finest stored data\n")
    print(analysis.summary(daily).to_string())
    if trades is not None:
        print("\nper exchange:")
        print(analysis.exchange_breakdown(trades).to_string())
        rv = analysis.realized_volatility(trades.set_index("time")["price"])
        print(f"\nmean daily realised volatility (from ticks): {rv.mean():.4%}")
    return 0


def cmd_chart(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        bars, trades = _load_prices(store, args)
    out_dir = settings.db_path.parent / "charts"
    stem = f"{args.ticker.upper()}-{datetime.now():%Y%m%d-%H%M%S}"
    paths = [charts.save(
        charts.candlestick(bars, f"{args.ticker.upper()} ({args.rule} bars)", args.sma, show_vwap=True),
        Path(args.out) if args.out else out_dir / f"{stem}-candles.html",
    )]
    if trades is not None:
        paths.append(charts.save(
            charts.exchange_share(analysis.exchange_breakdown(trades), f"{args.ticker.upper()} volume by exchange"),
            out_dir / f"{stem}-exchanges.html",
        ))
        if args.ticks:
            paths.append(charts.save(charts.tick_scatter(trades, f"{args.ticker.upper()} ticks"),
                                     out_dir / f"{stem}-ticks.html"))
    for p in paths:
        print(f"chart written to {p}")
    return 0


def cmd_export(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        if args.kind == "trades":
            df = store.load_trades(args.ticker, args.source, args.start, args.end)
        else:
            df = store.load_bars([args.ticker], args.kind, 1, args.start, args.end, args.source)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"{len(df)} rows written to {out}")
    return 0


def cmd_schedule(args, settings: Settings) -> int:
    """Windows Task Scheduler entry that runs `sync` every hour."""
    if sys.platform != "win32":
        print("On Linux/macOS add this line to `crontab -e`:")
        print(f"0 * * * * cd {Path.cwd()} && {sys.executable} -m massive_collate sync")
        return 0
    if args.action == "show":
        return subprocess.call(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
    if args.action == "remove":
        return subprocess.call(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # no console window popping up every hour
    runner = pythonw if pythonw.exists() else exe
    command = f'"{runner}" -m massive_collate sync'
    return subprocess.call([
        "schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
        "/SC", "HOURLY", "/MO", str(args.every_hours), "/F",
    ])


# -- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="massive_collate", description=__doc__)
    p.add_argument("--source", default="massive", choices=sorted(BUILDERS), help="data source (default: massive)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("watch", help="manage the watchlist")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("tickers", nargs="*")
    s.add_argument("--resolution", default="auto", choices=["auto", "trades", "second", "minute"])
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("sync", help="scheduled run: update and backfill the watchlist on every feed")
    s.add_argument("--only", dest="all_sources", action="store_false",
                   help="sync only the feed given by --source instead of all configured feeds")
    s.set_defaults(func=cmd_sync, all_sources=True)

    s = sub.add_parser("gui", help="open the chart app in your browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("probe", help="find the finest resolution your plan allows for a ticker")
    s.add_argument("ticker")
    s.add_argument("--watch", action="store_true", help="also add it to the watchlist")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("download", help="one-off bar download")
    s.add_argument("tickers", nargs="+")
    s.add_argument("--from", dest="start", type=_date)
    s.add_argument("--to", dest="end", type=_date)
    s.add_argument("--timespan", default="minute")
    s.add_argument("--multiplier", type=int, default=1)
    s.set_defaults(func=cmd_download)

    s = sub.add_parser("market-daily", help="daily bars for the whole US market")
    s.set_defaults(func=cmd_market_daily)

    s = sub.add_parser("exchanges", help="list exchanges")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_exchanges)

    s = sub.add_parser("info", help="reference details for a ticker")
    s.add_argument("ticker")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("status", help="what is stored, limits, and recent runs")
    s.add_argument("--runs", type=int, default=10)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("admin", help="database maintenance")
    s.add_argument("action", choices=["check", "vacuum", "backup", "reset-denied"])
    s.add_argument("--dest")
    s.set_defaults(func=cmd_admin)

    for name, func, helptext in (
        ("analyze", cmd_analyze, "statistics from stored data"),
        ("chart", cmd_chart, "write interactive HTML charts"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("ticker")
        s.add_argument("--from", dest="start", type=_date)
        s.add_argument("--to", dest="end", type=_date)
        s.add_argument("--rule", default="1min", help="bar size built from ticks, e.g. 1s, 1min, 1h, 1D")
        s.set_defaults(func=func)
        if name == "chart":
            s.add_argument("--sma", type=int, nargs="*", default=[])
            s.add_argument("--ticks", action="store_true", help="also plot every trade")
            s.add_argument("--out")

    s = sub.add_parser("export", help="export stored data to CSV")
    s.add_argument("ticker")
    s.add_argument("--kind", default="trades", choices=["trades", "second", "minute", "hour", "day"])
    s.add_argument("--from", dest="start", type=_date)
    s.add_argument("--to", dest="end", type=_date)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("schedule", help="install/remove the scheduled sync (Windows Task Scheduler)")
    s.add_argument("action", choices=["install", "remove", "show"])
    s.add_argument("--every-hours", type=int, default=1)
    s.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(getattr(args, "ticker", None), str) and args.command not in ("probe",):
        args.ticker = resolve(args.ticker).ticker  # data commands accept SPCX::NASDAQ, USDEUR, NQ1! ...
    try:
        settings = load_settings()
        return args.func(args, settings)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
