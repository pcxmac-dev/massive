"""Which markets are open right now, their exchanges and hours (the Markets panel), and the
sessions of a symbol over a time range (for annotating sessions on the chart)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..sessions import ET, ENERGY_FUTURES, EQUITY_FUTURES, MarketCalendar, nyse_holidays

US_VENUE_TYPES = ("exchange",)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(ET).isoformat() if t else None


def _next_holiday(cal: MarketCalendar, today: date) -> dict | None:
    for i in range(0, 400):
        d = today + timedelta(days=i)
        if d.weekday() < 5 and not cal.is_trading_day(d):
            name = nyse_holidays(d.year).get(d) or "Market closed"
            return {"date": d.isoformat(), "name": name}
        if cal.regular_hours(d) and cal.regular_hours(d)[1].hour < 16:
            return {"date": d.isoformat(), "name": "Early close (13:00 ET)"}
    return None


def us_equities(cal: MarketCalendar, now: datetime) -> dict:
    """Pre-market / regular / after-hours / closed, with the next change."""
    today = now.astimezone(ET).date()
    phases = []
    for d in (today - timedelta(days=1), today, *(today + timedelta(days=i) for i in range(1, 10))):
        ext = cal.stock_session(d)
        if not ext:
            continue
        reg = cal.regular_hours(d)
        phases += [("pre-market", ext.open, reg[0]), ("open", reg[0], reg[1]), ("after-hours", reg[1], ext.close)]
    current = next((p for p in phases if p[1] <= now < p[2]), None)
    upcoming = next((p for p in phases if p[1] > now), None)
    today_reg = cal.regular_hours(today)
    return {
        "state": current[0] if current else "closed",
        "since": _iso(current[1]) if current else None,
        "until": _iso(current[2]) if current else None,
        "next": {"state": upcoming[0], "at": _iso(upcoming[1])} if upcoming else None,
        "today": {"open": _iso(today_reg[0]), "close": _iso(today_reg[1])} if today_reg else None,
        "nextHoliday": _next_holiday(cal, today),
    }


def _around_the_clock(cal: MarketCalendar, asset: str, product: str | None, now: datetime) -> dict:
    today = now.astimezone(ET).date()
    sessions = cal.sessions(asset, today - timedelta(days=4), today + timedelta(days=10), product)
    current = next((s for s in sessions if s.open <= now < s.close), None)
    upcoming = next((s for s in sessions if s.open > now), None)
    return {
        "state": "open" if current else "closed",
        "since": _iso(current.open) if current else None,
        "until": _iso(current.close) if current else None,
        "next": {"state": "open", "at": _iso(upcoming.open)} if upcoming else None,
        "tradeDate": current.day.isoformat() if current else None,
    }


def markets(cal: MarketCalendar, exchanges, watch: list[dict], now: datetime | None = None) -> dict:
    """Every market group the watchlist touches, plus US equities always."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    us = exchanges[(exchanges["country"] == "US") & (exchanges["type"].isin(US_VENUE_TYPES))] \
        if len(exchanges) and "country" in exchanges else exchanges.iloc[0:0] if len(exchanges) else []
    venues = sorted({(r.name, r.mic) for r in us.itertuples() if r.mic}) if len(us) else []
    out = [{
        "id": "us-equities", "name": "US equities", "timezone": "America/New_York",
        "hours": "Pre-market 04:00, regular 09:30-16:00, after-hours to 20:00 ET",
        "exchanges": [{"name": n, "mic": m} for n, m in venues] or [{"name": "Nasdaq", "mic": "XNAS"},
                                                                       {"name": "New York Stock Exchange", "mic": "XNYS"}],
        "symbols": sorted(w["ticker"] for w in watch if w["asset"] == "stock"),
        **us_equities(cal, now),
    }]
    index = us_equities(cal, now)
    out.append({
        "id": "us-indices", "name": "US indices", "timezone": "America/New_York",
        "hours": "Calculated 09:30-16:00 ET", "exchanges": [{"name": "Index calculation (Nasdaq, S&P, Dow Jones)", "mic": None}],
        "symbols": sorted(w["ticker"] for w in watch if w["asset"] == "index"),
        **{**index, "state": "open" if index["state"] == "open" else "closed"},
    })
    products = sorted({w["source_ticker"] for w in watch if w["asset"] == "future"})
    groups = {"equity": [p for p in products if p in EQUITY_FUTURES or p not in ENERGY_FUTURES],
              "energy": [p for p in products if p in ENERGY_FUTURES]}
    for group, prods in groups.items():
        if not prods:
            continue
        out.append({
            "id": f"cme-{group}", "name": f"CME Globex {'equity index' if group == 'equity' else 'energy'} futures",
            "timezone": "America/Chicago",
            "hours": "Sunday 18:00 to Friday 17:00 ET, daily break 17:00-18:00 ET",
            "exchanges": [{"name": "CME Globex", "mic": "XCME" if group == "equity" else "XNYM"}],
            "symbols": sorted(w["ticker"] for w in watch if w["asset"] == "future" and w["source_ticker"] in prods),
            **_around_the_clock(cal, "future", prods[0], now),
        })
    if any(w["asset"] == "fx" for w in watch):
        out.append({
            "id": "fx", "name": "Foreign exchange", "timezone": "America/New_York",
            "hours": "Sunday 17:00 to Friday 17:00 ET", "exchanges": [{"name": "Interbank (OTC)", "mic": None}],
            "symbols": sorted(w["ticker"] for w in watch if w["asset"] == "fx"),
            **_around_the_clock(cal, "fx", None, now),
        })
    return {"now": now.isoformat(), "markets": out}


def symbol_sessions(cal: MarketCalendar, asset: str, product: str | None, start: datetime, end: datetime) -> list[dict]:
    """Sessions overlapping [start, end] for chart annotations: open/close and, for US stocks,
    the regular hours inside them."""
    out = []
    for s in cal.sessions(asset, (start - timedelta(days=2)).astimezone(ET).date(),
                          (end + timedelta(days=2)).astimezone(ET).date(), product):
        if s.close < start or s.open > end:
            continue
        row = {"day": s.day.isoformat(), "open": int(s.open.timestamp() * 1000), "close": int(s.close.timestamp() * 1000)}
        if asset == "stock":
            reg = cal.regular_hours(s.day)
            if reg:
                row["regularOpen"], row["regularClose"] = int(reg[0].timestamp() * 1000), int(reg[1].timestamp() * 1000)
        out.append(row)
    return out
