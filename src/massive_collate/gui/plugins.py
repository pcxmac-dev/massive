"""The Plugins view (rail icon: a plug; the user, 2026-10-27): every data provider plugin (a feed,
a source: sources/registry.py), its version and origin (built in, or an installed package), its
plan and where that came from, its limits and capabilities, its last capability scan with a
Rescan, and its part in the delivery scheme; the plugin API this app offers.

Each plugin refreshes its own plan info: its scan asks the provider what the key really gets and
may name the plan that shows (registry.Scanned), applied wherever the user set none
(config.with_detected). Scans run on each chart app start, weekly in the sync, and from this view.

A plugin with several feeds (Alpaca: alpaca, and its nights on Blue Ocean, alpaca (blue ocean);
Tiingo: tiingo and tiingo (forex): Provider.part_of) is one entry: its feeds listed by the plugin's
name (registry.label), their capabilities, scans, eras, requests and symbols together.
While a rescan runs, each feed it has still to ask says "scanning" (the user, 2026-10-29: "when i
hit rescan all, i want the status of each plug in to say scanning, not 'ready'"); one asked in this
run is back to its state, and a feed with no scan (a public one) or none it can ask keeps its own.

Read from the settings, the plugins' reports, the scans kept (kv sourcing.SCAN_KEY), the watchlist
and the request ledger: no requests. A key's value never leaves: only whether it is set.
"""
from __future__ import annotations

from datetime import date, datetime

from ..sources import providers
from ..sources.registry import PLUGIN_API, errors, label, plugin_feeds, plugin_of

GROUPS = ("Feeds with keys", "Public feeds")


def _fresh(latency: float) -> str:
    return "real time" if latency < 60 else f"{latency / 60:.0f} min delayed" if latency < 86_400 else "end of day"


def _state(keys_set, served, kill) -> str:
    """ready, partly (only what it serves without its keys), no key, killed."""
    if kill:
        return "killed"
    if keys_set is False:
        return "partly" if served else "no key"
    return "ready"


def _asked_since(at, since) -> bool:
    """Whether a scan kept at `at` was asked at or after `since` (ISO times; one without an offset
    is local time, as the scans keep it)."""
    try:
        a, b = (t if t.tzinfo else t.astimezone() for t in (datetime.fromisoformat(at), datetime.fromisoformat(since)))
    except (TypeError, ValueError):
        return False
    return a >= b


def _scanning(p, running: dict | None, scan: dict | None, state: str) -> bool:
    """Whether the rescan going now has this feed still to ask: it covers the feed (every plugin,
    the feed, or the plugin it is part of), the feed has a scan and is set up (ready, or partly:
    what it serves without its keys), and its kept scan is older than the rescan."""
    if not running or p.scan is None or state not in ("ready", "partly"):
        return False
    if running.get("name") not in (None, p.name, plugin_of(p.name)):
        return False
    return not ((scan or {}).get("at") and _asked_since(scan["at"], running.get("since")))


def _fold(entry: dict, feeds: list[dict]) -> dict:
    """A plugin's entry with the feeds part of it folded in (`feeds`: its own first): what each can
    do, its scan's answers and its eras named by the feed; requests, symbols and load summed (each
    feed's requests are its own in the ledger, its caps the plugin's: one account)."""
    every = [entry, *feeds]
    for e in every:
        for row in (*e["capabilities"], *e["scan"]["results"], *e["delivery"]):
            row["feed"] = e["label"]
    out = {**entry,
           "feeds": [{"name": e["name"], "title": e["title"], "label": e["label"], "about": e["about"], "state": e["state"],
                      "serves": e["serves"], "scanAt": e["scan"]["at"]} for e in every],
           "assets": sorted({a for e in every for a in e["assets"]}),
           "capabilities": [c for e in every for c in e["capabilities"]],
           "streams": [s for e in every for s in e["streams"]],
           "notes": [*entry["notes"], *(f"{e['label']}: {n}" for e in feeds for n in e["notes"])],
           "delivery": [d for e in every for d in e["delivery"]],
           "watched": sum(e["watched"] for e in every), "load": round(sum(e["load"] for e in every), 3),
           "usage": {**entry["usage"], "today": sum(e["usage"]["today"] for e in every)}}
    ats = [e["scan"]["at"] for e in every if e["scan"]["has"] and e["scan"]["at"]]
    out["scan"] = {"has": any(e["scan"]["has"] for e in every), "at": min(ats) if ats else None,
                   "results": [r for e in every for r in e["scan"]["results"]],
                   "running": any(e["scan"]["running"] for e in every)}
    if any(e["state"] == "scanning" for e in every) and entry["state"] in ("ready", "partly", "scanning"):
        out["state"] = "scanning"
    return out


def _plan(p, settings, scan: dict | None) -> dict:
    """Its plan here: each plan variable with its origin (you, its scan, the default), what its last
    scan found, where that and your own setting disagree, and what its scan cannot tell."""
    found = (scan or {}).get("plan") or {}
    alike = {k: list(v) for k, v in ((scan or {}).get("alike") or {}).items()}
    mine = settings.variables or {}
    vars_ = p.plan_state(settings)
    differs = [{"name": v, "yours": mine[v], "found": found[v]} for v in found
               if mine.get(v) and str(mine[v]).strip().lower() not in
               {str(x).strip().lower() for x in (found[v], *alike.get(v, ()))}]
    return {"tier": settings.for_source(p.name).tier.name, "vars": vars_, "found": found, "alike": alike,
            "differs": differs, "note": p.plan_note}


def view(store, settings, feeds: dict, scheme, scans: dict, running: dict | None = None,
         today: date | None = None) -> dict:
    """Every plugin for the view (see the module's docstring); `running`: the rescan going now
    ({"name": one provider or None for all, "since": iso}) or None."""
    from .. import killswitch

    today = today or scheme.today
    counts = store.watch_counts()
    entries = {}
    for name, p in providers().items():
        prof = feeds.get(name)  # None: not set up here at all (Massive without its key)
        scan = scans.get(name)
        report = p.report(prof, today, settings, scan) if prof is not None else None
        keys_set = p.keys_set(settings)
        served = sorted(prof.assets) if prof is not None else []
        kill = killswitch.killed(store, name, "fetch")
        view_ = settings.for_source(name)
        delivery = [{"job": e.resolution, "asset": e.asset, "market": e.market, "start": e.start.isoformat(),
                     "end": e.end.isoformat() if e.end else None, "first": e.sources[0] == name,
                     "unique": e.unique, "others": [label(s) for s in e.sources if s != name], "symbols": e.symbols}
                    for e in scheme.eras if name in e.sources]
        state = _state(keys_set, served, kill)
        scanning = _scanning(p, running, scan, state)
        entries[name] = {
            "name": name, "title": p.title, "label": label(name), "about": p.about, "origin": p.origin,
            "version": p.version, "api": p.api,
            "signup": p.signup,
            "group": GROUPS[0] if p.keys else GROUPS[1], "archive": p.archival, "kind": p.kind,
            "assets": sorted(p.assets), "keyless": sorted(p.keyless), "serves": served,
            "configured": prof is not None, "keysSet": keys_set,
            "keys": [{"name": k.name, "label": k.label, "set": bool(k.value(settings.env))} for k in p.keys],
            "state": "scanning" if scanning else state, "kill": kill,
            "plan": _plan(p, settings, scan),
            "limits": ({**report.limits.__dict__, "rate": list(report.limits.rate) if report.limits.rate else None}
                       if report else {"published": p.limits}),
            "capabilities": [{"asset": c.asset, "resolution": c.resolution, "market": c.market, "since": str(c.since),
                              "fresh": _fresh(c.latency), "perRequest": c.per_request}
                             for c in (report.capabilities if report else ())],
            "streams": [{"asset": a, "what": w} for a, w in (report.streams if report else ())],
            "notes": list(report.notes) if report else [],
            "scan": {"has": p.scan is not None, "at": (scan or {}).get("at"),
                     "results": [{"what": w, "available": bool(ok), "detail": d} for w, ok, d in (scan or {}).get("results", [])],
                     "running": scanning},
            "usage": {"today": store.requests_today(name), "perDay": view_.max_requests_per_day,
                      "perRun": view_.max_requests_per_run},
            "watched": sum(n for (feed, _), n in counts.items() if feed == name),
            "delivery": delivery, "load": round(scheme.load.get(name, 0.0), 3),
        }
    out = [_fold(e, [entries[n] for n in plugin_feeds(name)[1:]]) for name, e in entries.items() if plugin_of(name) == name]
    return {"plugins": out, "groups": list(GROUPS), "errors": [{"where": w, "why": why} for w, why in errors()],
            "api": PLUGIN_API,
            "running": running, "offline": bool(settings.offline),
            "keyScript": r"powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1"}
