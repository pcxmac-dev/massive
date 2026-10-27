"""The Plugins view (rail icon: a plug; the user, 2026-10-27): every data provider plugin (a feed,
a source: sources/registry.py), its plan and where that came from, its limits and capabilities,
its last capability scan with a Rescan, and its part in the delivery scheme.

Each plugin refreshes its own plan info: its scan asks the provider what the key really gets and
may name the plan that shows (registry.Scanned), applied wherever the user set none
(config.with_detected). Scans run on each chart app start, weekly in the sync, and from this view.

Read from the settings, the plugins' reports, the scans kept (kv sourcing.SCAN_KEY), the watchlist
and the request ledger: no requests. A key's value never leaves: only whether it is set.
"""
from __future__ import annotations

from datetime import date

from ..sources import providers
from ..sources.registry import errors

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


def _plan(p, settings, scan: dict | None) -> dict:
    """Its plan here: each plan variable with its origin (you, its scan, the default), what its last
    scan found, where that and your own setting disagree, and what its scan cannot tell."""
    found = (scan or {}).get("plan") or {}
    mine = settings.variables or {}
    vars_ = p.plan_state(settings)
    differs = [{"name": v, "yours": mine[v], "found": found[v]} for v in found
               if mine.get(v) and str(mine[v]).strip().lower() != str(found[v]).strip().lower()]
    return {"tier": settings.for_source(p.name).tier.name, "vars": vars_, "found": found,
            "differs": differs, "note": p.plan_note}


def view(store, settings, feeds: dict, scheme, scans: dict, running: dict | None = None,
         today: date | None = None) -> dict:
    """Every plugin for the view (see the module's docstring); `running`: the rescan going now
    ({"name": one provider or None for all, "since": iso}) or None."""
    from .. import killswitch

    today = today or scheme.today
    counts = store.watch_counts()
    out = []
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
                     "unique": e.unique, "others": [s for s in e.sources if s != name], "symbols": e.symbols}
                    for e in scheme.eras if name in e.sources]
        out.append({
            "name": name, "title": p.title, "about": p.about, "origin": p.origin, "signup": p.signup,
            "group": GROUPS[0] if p.keys else GROUPS[1], "archive": p.archival, "kind": p.kind,
            "assets": sorted(p.assets), "keyless": sorted(p.keyless), "serves": served,
            "configured": prof is not None, "keysSet": keys_set,
            "keys": [{"name": k.name, "label": k.label, "set": bool(k.value(settings.env))} for k in p.keys],
            "state": _state(keys_set, served, kill), "kill": kill,
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
                     "running": bool(running) and running.get("name") in (None, name)},
            "usage": {"today": store.requests_today(name), "perDay": view_.max_requests_per_day,
                      "perRun": view_.max_requests_per_run},
            "watched": sum(n for (feed, _), n in counts.items() if feed == name),
            "delivery": delivery, "load": round(scheme.load.get(name, 0.0), 3),
        })
    return {"plugins": out, "groups": list(GROUPS), "errors": [{"where": w, "why": why} for w, why in errors()],
            "running": running, "offline": bool(settings.offline),
            "keyScript": r"powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1"}
