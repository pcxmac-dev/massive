"""World stock exchanges for the Markets view: where they are, whether they are open right now
(lunch breaks included), when that changes, and their upcoming holidays.

Sessions and holidays come from `exchange_calendars` (each exchange's own calendar, lunar
holidays and special closures included). Shenzhen shares Shanghai's calendar and the NSE shares
the BSE's. Calendars are built once per process and cover a year ahead.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd


@dataclass(frozen=True)
class Exchange:
    code: str  # exchange_calendars name (ISO MIC)
    name: str
    city: str
    country: str
    region: str
    lat: float
    lon: float
    index: str  # flagship index
    calendar: str | None = None  # calendar to use when it differs from `code`


EXCHANGES: tuple[Exchange, ...] = (
    # Americas
    Exchange("XNYS", "New York Stock Exchange / Nasdaq", "New York", "United States", "Americas", 40.71, -74.01, "S&P 500 · Nasdaq · Dow"),
    Exchange("XTSE", "Toronto Stock Exchange", "Toronto", "Canada", "Americas", 43.65, -79.38, "S&P/TSX Composite"),
    Exchange("XMEX", "Bolsa Mexicana de Valores", "Mexico City", "Mexico", "Americas", 19.43, -99.13, "S&P/BMV IPC"),
    Exchange("BVMF", "B3", "São Paulo", "Brazil", "Americas", -23.55, -46.63, "Ibovespa"),
    Exchange("XBUE", "Bolsas y Mercados Argentinos", "Buenos Aires", "Argentina", "Americas", -34.60, -58.38, "S&P Merval"),
    Exchange("XSGO", "Santiago Stock Exchange", "Santiago", "Chile", "Americas", -33.45, -70.67, "S&P IPSA"),
    Exchange("XLIM", "Bolsa de Valores de Lima", "Lima", "Peru", "Americas", -12.05, -77.04, "S&P/BVL Peru General"),
    Exchange("XBOG", "Bolsa de Valores de Colombia", "Bogotá", "Colombia", "Americas", 4.71, -74.07, "MSCI COLCAP"),
    # Europe
    Exchange("XLON", "London Stock Exchange", "London", "United Kingdom", "Europe", 51.51, -0.09, "FTSE 100"),
    Exchange("XETR", "Xetra (Deutsche Börse)", "Frankfurt", "Germany", "Europe", 50.11, 8.68, "DAX"),
    Exchange("XPAR", "Euronext Paris", "Paris", "France", "Europe", 48.87, 2.34, "CAC 40"),
    Exchange("XAMS", "Euronext Amsterdam", "Amsterdam", "Netherlands", "Europe", 52.37, 4.90, "AEX"),
    Exchange("XBRU", "Euronext Brussels", "Brussels", "Belgium", "Europe", 50.85, 4.35, "BEL 20"),
    Exchange("XLIS", "Euronext Lisbon", "Lisbon", "Portugal", "Europe", 38.72, -9.14, "PSI"),
    Exchange("XDUB", "Euronext Dublin", "Dublin", "Ireland", "Europe", 53.35, -6.26, "ISEQ 20"),
    Exchange("XSWX", "SIX Swiss Exchange", "Zurich", "Switzerland", "Europe", 47.37, 8.54, "SMI"),
    Exchange("XMIL", "Borsa Italiana", "Milan", "Italy", "Europe", 45.46, 9.19, "FTSE MIB"),
    Exchange("XMAD", "Bolsa de Madrid", "Madrid", "Spain", "Europe", 40.42, -3.70, "IBEX 35"),
    Exchange("XSTO", "Nasdaq Stockholm", "Stockholm", "Sweden", "Europe", 59.33, 18.07, "OMX Stockholm 30"),
    Exchange("XOSL", "Oslo Børs", "Oslo", "Norway", "Europe", 59.91, 10.75, "OBX"),
    Exchange("XCSE", "Nasdaq Copenhagen", "Copenhagen", "Denmark", "Europe", 55.68, 12.57, "OMX Copenhagen 25"),
    Exchange("XHEL", "Nasdaq Helsinki", "Helsinki", "Finland", "Europe", 60.17, 24.94, "OMX Helsinki 25"),
    Exchange("XICE", "Nasdaq Iceland", "Reykjavík", "Iceland", "Europe", 64.15, -21.94, "OMX Iceland 15"),
    Exchange("XWBO", "Wiener Börse", "Vienna", "Austria", "Europe", 48.21, 16.37, "ATX"),
    Exchange("XWAR", "Warsaw Stock Exchange", "Warsaw", "Poland", "Europe", 52.23, 21.01, "WIG20"),
    Exchange("XPRA", "Prague Stock Exchange", "Prague", "Czechia", "Europe", 50.08, 14.43, "PX"),
    Exchange("XBUD", "Budapest Stock Exchange", "Budapest", "Hungary", "Europe", 47.50, 19.04, "BUX"),
    Exchange("ASEX", "Athens Stock Exchange", "Athens", "Greece", "Europe", 37.98, 23.73, "ATHEX Composite"),
    Exchange("XIST", "Borsa İstanbul", "Istanbul", "Türkiye", "Europe", 41.01, 28.98, "BIST 100"),
    # Middle East & Africa
    Exchange("XSAU", "Saudi Exchange (Tadawul)", "Riyadh", "Saudi Arabia", "Middle East & Africa", 24.71, 46.68, "TASI"),
    Exchange("XTAE", "Tel Aviv Stock Exchange", "Tel Aviv", "Israel", "Middle East & Africa", 32.08, 34.78, "TA-35"),
    Exchange("XJSE", "Johannesburg Stock Exchange", "Johannesburg", "South Africa", "Middle East & Africa", -26.11, 28.06, "FTSE/JSE Top 40"),
    # Asia-Pacific
    Exchange("XTKS", "Tokyo Stock Exchange", "Tokyo", "Japan", "Asia-Pacific", 35.68, 139.77, "Nikkei 225 · TOPIX"),
    Exchange("XKRX", "Korea Exchange", "Seoul", "South Korea", "Asia-Pacific", 37.52, 126.92, "KOSPI"),
    Exchange("XSHG", "Shanghai Stock Exchange", "Shanghai", "China", "Asia-Pacific", 31.23, 121.47, "SSE Composite"),
    Exchange("XSHE", "Shenzhen Stock Exchange", "Shenzhen", "China", "Asia-Pacific", 22.54, 114.06, "SZSE Component", "XSHG"),
    Exchange("XHKG", "Hong Kong Exchanges", "Hong Kong", "Hong Kong", "Asia-Pacific", 22.28, 114.16, "Hang Seng"),
    Exchange("XTAI", "Taiwan Stock Exchange", "Taipei", "Taiwan", "Asia-Pacific", 25.03, 121.56, "TAIEX"),
    Exchange("XSES", "Singapore Exchange", "Singapore", "Singapore", "Asia-Pacific", 1.28, 103.85, "Straits Times Index"),
    Exchange("XKLS", "Bursa Malaysia", "Kuala Lumpur", "Malaysia", "Asia-Pacific", 3.15, 101.70, "FTSE Bursa Malaysia KLCI"),
    Exchange("XBKK", "Stock Exchange of Thailand", "Bangkok", "Thailand", "Asia-Pacific", 13.73, 100.52, "SET"),
    Exchange("XIDX", "Indonesia Stock Exchange", "Jakarta", "Indonesia", "Asia-Pacific", -6.20, 106.82, "IDX Composite"),
    Exchange("XPHS", "Philippine Stock Exchange", "Manila", "Philippines", "Asia-Pacific", 14.55, 121.02, "PSEi"),
    Exchange("XBOM", "BSE", "Mumbai", "India", "Asia-Pacific", 18.93, 72.83, "S&P BSE Sensex"),
    Exchange("XNSE", "National Stock Exchange of India", "Mumbai", "India", "Asia-Pacific", 19.06, 72.86, "Nifty 50", "XBOM"),
    Exchange("XKAR", "Pakistan Stock Exchange", "Karachi", "Pakistan", "Asia-Pacific", 24.86, 67.01, "KSE-100"),
    Exchange("XASX", "Australian Securities Exchange", "Sydney", "Australia", "Asia-Pacific", -33.87, 151.21, "S&P/ASX 200"),
    Exchange("XNZE", "NZX", "Wellington", "New Zealand", "Asia-Pacific", -41.29, 174.78, "S&P/NZX 50"),
)

_calendars: dict[str, object] = {}
_lock = threading.Lock()


def _calendar(name: str):
    import exchange_calendars as xc

    with _lock:
        cal = _calendars.get(name)
        if cal is None:
            today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
            start, end = today - pd.Timedelta(days=30), today + pd.Timedelta(days=400)
            try:
                cal = xc.get_calendar(name, start=start, end=end)
            except Exception:
                # Some exchanges publish next year's holidays late in the year (China, Singapore,
                # India): use what is recorded and say so rather than guess.
                cal = xc.get_calendar(name, start=start)
            _calendars[name] = cal
        return cal


def warm() -> None:
    """Build every calendar (a few seconds); run in a background thread at start-up."""
    for e in EXCHANGES:
        try:
            _calendar(e.calendar or e.code)
        except Exception:  # an unknown calendar must not stop the others
            pass


def _iso(t) -> str | None:
    return None if t is None or pd.isna(t) else pd.Timestamp(t).tz_convert("UTC").isoformat()


def status(e: Exchange, now: datetime) -> dict:
    cal = _calendar(e.calendar or e.code)
    minute = pd.Timestamp(now).tz_convert("UTC").floor("min")
    tz = str(cal.tz)
    local_now = minute.tz_convert(tz)
    sessions = cal.sessions
    today = local_now.tz_localize(None).normalize()
    out = {"code": e.code, "name": e.name, "city": e.city, "country": e.country, "region": e.region,
           "lat": e.lat, "lon": e.lon, "index": e.index, "timezone": tz,
           "localTime": local_now.strftime("%a %H:%M")}
    if cal.is_open_on_minute(minute, ignore_breaks=True):
        session = cal.minute_to_session(minute, direction="previous")
        close = cal.session_close(session)
        b0, b1 = cal.session_break_start(session), cal.session_break_end(session)
        if not pd.isna(b0) and b0 <= minute < b1:
            out.update(state="break", until=_iso(b1), event="reopens")
        else:
            nxt = b0 if not pd.isna(b0) and minute < b0 else close
            out.update(state="open", until=_iso(nxt), event="lunch break" if nxt is b0 else "closes")
    else:
        nxt = cal.next_open(minute)
        out.update(state="closed", until=_iso(nxt), event="opens")
    if today in sessions:
        o, c = cal.session_open(today), cal.session_close(today)
        out["today"] = {"open": o.tz_convert(tz).strftime("%H:%M"), "close": c.tz_convert(tz).strftime("%H:%M")}
        b0, b1 = cal.session_break_start(today), cal.session_break_end(today)
        if not pd.isna(b0):
            out["today"]["break"] = f"{b0.tz_convert(tz):%H:%M}-{b1.tz_convert(tz):%H:%M}"
    else:
        out["today"] = None
    # Upcoming closures on the exchange's normal trading days (its own weekend excluded).
    days = pd.date_range(today, today + pd.Timedelta(days=60), freq="D")
    trading_weekdays = days[[cal.weekmask[d.dayofweek] == "1" for d in days]]
    last = sessions[-1]
    out["holidays"] = [d.strftime("%Y-%m-%d") for d in trading_weekdays if d not in sessions and d <= last][:4]
    out["calendarEnds"] = last.strftime("%Y-%m-%d")
    out["weekend"] = [i for i, c in enumerate(cal.weekmask) if c == "0"]
    return out


def world_markets(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for e in EXCHANGES:
        try:
            out.append(status(e, now))
        except Exception as err:  # keep the rest if one calendar has a problem
            out.append({"code": e.code, "name": e.name, "city": e.city, "country": e.country, "region": e.region,
                        "lat": e.lat, "lon": e.lon, "index": e.index, "state": "unknown", "error": str(err)})
    return out


def subsolar(now: datetime | None = None) -> dict:
    """Where the sun is overhead now: the longitude where it is solar noon, and the latitude
    (declination). Within about half a degree (NOAA's simple formulas): plenty to place noon on a globe."""
    import math

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.timetuple().tm_yday
    hours = now.hour + now.minute / 60 + now.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))  # minutes
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lon = -15 * (hours - 12 + eqtime / 60)
    lon = (lon + 180) % 360 - 180
    return {"lat": math.degrees(decl), "lon": lon, "at": now.isoformat()}
