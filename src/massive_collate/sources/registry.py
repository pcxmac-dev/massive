"""Data providers are plugins: one `Provider` each, holding everything the app needs to know about it.

A provider plugin is a module that defines `PLUGIN` (a Provider; `PLUGINS` for several). The ones
built in sit in `sources/plugins/`, one module per provider: a module dropped there is found on the
next start. A provider shipped as its own package registers under the entry point group
`massive_collate.providers` (pointing at the module, or at the Provider itself).

What a plugin gives:
- `build(settings, request_budget)`: its DataSource (sources/base.py), with its limiter and caps.
- its keys (`KeyVar`: user environment variables; never written to a file), and how to get one
  (`signup`): tools/set-key.ps1 offers each plugin's keys from `python -m massive_collate plugins --json`.
- what it serves: asset classes (`assets`; `keyless` the ones it serves without keys), the market
  each is (`markets`), whether it carries a symbol (`carries`), and whether it is a closed archive.
- its plan, rate window and caps (`view`, `rate`), and what its server publishes (`limits`).
  It refreshes its own plan info: its capability scan (`scan`) asks the provider what the key
  really gets and may answer with the plan that shows (`Scanned.plan`: values of its own
  `plan_vars`), which the app applies wherever the user set none (config.with_detected). A
  provider whose answers cannot tell its plans apart says so (`plan_note`); its plan is then
  only what the user chose (`plan_choices`, offered by the key script and the Plugins view).
- facts the rest of the app asks: its kind of data for the composite model, the exchange it is the
  reference for, the bars one request returns, a monthly symbol cap, the bulk symbols' resolution.
- its capabilities (`provides`, `plan_resolutions`, `per_request`, `streams`), from which it reports
  in one standard form (`report`, a `Report`): its subscription, its limits, one line per job it
  can do on that plan (asset class, resolution, market, how far back, how fresh, bars a request)
  and its live streams. The delivery scheme (sourcing.py) reads only these reports; `sourcing`
  shows them. A plugin whose plan says more than its declarations amends its report (`reporter`).

Callbacks left out take the defaults below, so a plain keyed REST feed needs little more than its
build. A plugin that fails to load is left out and reported (`errors()`, `plugins`), never fatal.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from ..ratelimit import MachineLedger, RateLimiter

if TYPE_CHECKING:
    from ..config import Settings, Tier
    from .base import DataSource

ENTRY_POINTS = "massive_collate.providers"
DEFAULT_PER_REQUEST = 1000  # bars a request returns, for a provider that does not say


@dataclass(frozen=True)
class KeyVar:
    """A key a provider reads, from the user's environment variables only."""
    name: str
    label: str = "API key"
    aliases: tuple[str, ...] = ()  # also read (Alpaca's own APCA_ names); the key script sets `name`

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def value(self, env: Mapping[str, str]) -> str | None:
        return next((env[n] for n in self.names if env.get(n)), None)


@dataclass(frozen=True)
class Scanned:
    """A capability scan's answer: each thing asked (what, available, detail), and the plan the
    answers show ({plan variable: value}; empty when they cannot tell the plans apart). `alike`:
    the other values the same answers would come from ({variable: (values)}: Tiingo's Power and
    Commercial reach the same data), the plan found being the one with the lowest limits."""
    results: tuple[tuple[str, bool, str], ...]
    plan: Mapping[str, str] = field(default_factory=dict)
    alike: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# -- the standard report every provider gives the delivery scheme (sourcing.py) ---------------------

@dataclass(frozen=True)
class Limits:
    """A subscription's limits: what a job's cost is counted against."""
    requests_a_day: float  # what it can send in a day: its daily cap, or less where its rate is tighter
    per_day: int | None = None
    per_run: int | None = None
    rate: tuple[int | None, float] | None = None  # (requests, seconds) its limiter keeps
    symbols_a_month: int | None = None
    published: str = ""  # what its server publishes


@dataclass(frozen=True)
class Capability:
    """One job a provider can do on its plan: an asset class at one resolution."""
    asset: str
    resolution: str  # day, minute, second
    market: str  # what its data is: feeds of one market are copies of each other
    since: object  # date: how far back its plan reaches
    latency: float  # seconds its newest data trails real time (86,400: end of day)
    per_request: int | None  # bars one request returns (None: a symbol's whole history at once)


@dataclass(frozen=True)
class Report:
    """A provider's standard report: the same form for every plugin."""
    source: str
    title: str
    plan: str  # its subscription here
    keys_set: bool | None  # None: it needs none
    limits: Limits
    capabilities: tuple[Capability, ...]
    streams: tuple[tuple[str, str], ...] = ()  # (asset class, what its live stream is)
    kind: str = ""  # its kind of data (exchange, consolidated, official, vendor)
    notes: tuple[str, ...] = ()
    scanned: tuple[tuple[str, bool, str], ...] = ()  # its last capability scan: (what, available, detail)
    scanned_at: str | None = None

    def as_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "plan": self.plan, "keysSet": self.keys_set,
                "kind": self.kind, "notes": list(self.notes), "scannedAt": self.scanned_at,
                "scanned": [{"what": w, "available": ok, "detail": d} for w, ok, d in self.scanned],
                "limits": {**self.limits.__dict__, "rate": list(self.limits.rate) if self.limits.rate else None},
                "capabilities": [{**c.__dict__, "since": str(c.since)} for c in self.capabilities],
                "streams": [{"asset": a, "what": w} for a, w in self.streams]}

    def lines(self) -> list[str]:
        """The report for the terminal, the same layout for every provider."""
        lim = self.limits
        rate = f", {lim.rate[0]:,} per {lim.rate[1]:g} s" if lim.rate and lim.rate[0] else ""
        caps = (f"{lim.requests_a_day:,.0f} requests a day{rate}" + (f", {lim.per_run:,} a run" if lim.per_run else "")
                + (f", {lim.symbols_a_month:,} symbols a month" if lim.symbols_a_month else ""))
        keys = "" if self.keys_set is None else "; keys set" if self.keys_set else "; keys NOT set"
        out = [f"{self.source} ({self.title}): plan {self.plan}{keys}; {caps}"
               + (f"; server: {lim.published}" if lim.published else "")]
        for c in self.capabilities:
            fresh = "real time" if c.latency < 60 else f"{c.latency / 60:.0f} min delayed" if c.latency < 86_400 \
                else "end of day"
            bars = "whole history a request" if c.per_request is None else f"{c.per_request:,} bars a request"
            out.append(f"    {c.asset:6} {c.resolution:6} {c.market:12} from {c.since}  {fresh:16} {bars}")
        out += [f"    {a:6} live   {w}" for a, w in self.streams]
        out += [f"    note: {n}" for n in self.notes]
        if self.scanned_at:
            out.append(f"    scanned {self.scanned_at[:16].replace('T', ' ')}:")
            out += [f"      {'yes' if ok else 'no ':3} {w}" + (f": {d}" if d else "") for w, ok, d in self.scanned]
        return out


@dataclass(frozen=True, eq=False)
class Provider:
    name: str  # the feed's name everywhere: its database file, the watchlist, the ledgers
    title: str
    build: Callable[["Settings", int | None], "DataSource"]
    assets: frozenset  # asset classes it can collect at all (with keys / on some plan)
    order: int = 100  # its place in lists (the built-in feeds: 0-60)
    about: str = ""  # one line for menus and lists
    keys: tuple[KeyVar, ...] = ()
    keyless: frozenset = frozenset()  # asset classes served without its keys (Alpaca: crypto)
    signup: str = ""  # where a key is made
    plan_vars: tuple[str, ...] = ()  # its plan's variables: read from the saved user variables like keys
    plan_choices: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # plan variable -> its values (the default first)
    plan_note: str = ""  # what its scan cannot tell about its plan (the user's choice is then the plan)
    options: tuple[str, ...] = ()  # its other variables (Settings.env)
    archival: bool = False  # a closed feed: each symbol filled once, then never asked again
    tier: "Tier | None" = None  # its plan, when it has one fixed plan
    per_run: int | None = None  # its request caps with that plan
    per_day: int | None = None
    limits: str = ""  # what its server publishes, beside the window kept (Fetcher, Limits)
    kind: str = "vendor"  # its data for the composite model: exchange, consolidated, official, vendor
    kind_by_asset: Mapping[str, str] = field(default_factory=dict)
    reference_for: frozenset = frozenset()  # asset classes whose gold standard exchange it is
    venue_notes: Mapping[str, str] = field(default_factory=dict)  # audits: what its data of an asset is
    symbols_per_month: int | None = None  # different symbols its plan allows a month
    bulk_resolution: str = "auto"  # the initializer's bulk symbols start at this resolution
    missing_keys_note: str = ""  # the Fetcher's hover text when its keys are not set
    missing_keys_chip: str = "no key"
    # Capabilities, for the delivery scheme (sourcing.py): the resolutions it provides per asset class
    # (day, minute, second; default: day and minute for each of `assets`), the bars one request
    # returns per resolution (None: a symbol's whole history in one request), its live streams.
    provides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    per_request: Mapping[str, int | None] = field(default_factory=dict)
    streams: Mapping[str, str] = field(default_factory=dict)  # asset class -> what its stream is
    # Callbacks (None: the default described at each method).
    view: Callable[["Settings"], "Settings"] | None = None
    rate: Callable[["Settings"], tuple[int | None, float]] | None = None
    has_keys: Callable[["Settings"], bool] | None = None
    serves: Callable[["Settings"], frozenset | None] | None = None
    markets: Callable[["Settings"], dict] | None = None
    carries: Callable[[object], bool] | None = None
    plan_resolutions: Callable[["Tier", str], tuple[str, ...]] | None = None  # what its plan gives per asset
    reporter: Callable[["Report", "Settings | None"], "Report"] | None = None  # amends its standard report
    # Its capability scan (`capabilities`; on each chart app start, weekly in the sync, Rescan in the
    # Plugins view): a few requests asking what its plan really gives, each answer (what, available,
    # detail), or a `Scanned` with the plan they show too; kept, and shown in its report.
    scan: Callable[["DataSource"], "list[tuple[str, bool, str]] | Scanned"] | None = None
    origin: str = "built in"  # set when loaded: built in, or the package's entry point

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(n for k in self.keys for n in k.names)

    @property
    def key_hint(self) -> str:
        return " and ".join(k.name for k in self.keys)

    def keys_set(self, settings: "Settings") -> bool | None:
        """Whether its keys are set (None: it needs none). Default: every key's variable has a value."""
        if not self.keys:
            return None
        if self.has_keys:
            return bool(self.has_keys(settings))
        return all(k.value(settings.env) for k in self.keys)

    def served(self, settings: "Settings") -> frozenset | None:
        """The asset classes it collects on this machine; None when it is not set up here at all.
        Default: all of `assets` with its keys (or needing none), only `keyless` without."""
        if self.serves:
            return self.serves(settings)
        return self.assets if self.keys_set(settings) in (None, True) else frozenset(self.keyless)

    def settings_view(self, base: "Settings") -> "Settings":
        """The settings as this feed sees them (Settings.for_source): its plan and caps. Default:
        its fixed `tier` and caps when it has them, otherwise the settings as they are."""
        if self.view:
            return self.view(base)
        if self.tier is None:
            return base
        return replace(base, tier=self.tier, max_requests_per_run=self.per_run or base.max_requests_per_run,
                       max_requests_per_day=self.per_day or base.max_requests_per_day, origin=base)

    def rate_window(self, settings: "Settings") -> tuple[int | None, float]:
        """The window its limiter keeps: (requests, seconds). Default: its plan's rate a minute."""
        if self.rate:
            return self.rate(settings)
        rpm = settings.for_source(self.name).tier.requests_per_minute
        return rpm, minute_window(rpm)

    def market_of(self, settings: "Settings") -> dict[str, str]:
        """Which market each of its asset classes is (feeds of one market are copies of each other:
        one download per span between them). Default: each class its own market, named after the feed."""
        return self.markets(settings) if self.markets else {a: self.name for a in self.assets}

    def can_carry(self, spec) -> bool:
        """Whether it can hold a symbol at all: its asset class, then its own list (`carries`)."""
        return spec.asset in self.assets and (self.carries is None or bool(self.carries(spec)))

    def kind_for(self, asset: str) -> str:
        return self.kind_by_asset.get(asset) or self.kind

    def resolutions_for(self, tier: "Tier", asset: str) -> tuple[str, ...]:
        """The resolutions its plan (`tier`) provides for an asset class. Default: `provides`, or day
        and minute for each of its asset classes."""
        if asset not in self.assets:
            return ()
        if self.plan_resolutions:
            return tuple(self.plan_resolutions(tier, asset))
        return tuple(self.provides.get(asset, ("day", "minute")))

    def run_scan(self, source: "DataSource") -> Scanned:
        """Its capability scan, its plan held to its own plan variables (a plugin sets no other's)."""
        out = self.scan(source) if self.scan else ()
        rows, plan, alike = (out.results, out.plan, out.alike) if isinstance(out, Scanned) else (out, {}, {})
        plan = {k: str(v) for k, v in (plan or {}).items() if k in self.plan_vars and v}
        return Scanned(tuple((str(w), bool(ok), str(d)) for w, ok, d in rows), plan,
                       {k: tuple(str(x) for x in v) for k, v in (alike or {}).items() if k in plan})

    def plan_state(self, settings: "Settings") -> list[dict]:
        """Each of its plan variables: its value here and where it comes from (the user's own
        setting, its scan, or the default when neither set it), its choices."""
        mine = settings.variables or {}
        found = settings.detected or {}
        out = []
        for v in self.plan_vars:
            choices = list(self.plan_choices.get(v, ()))
            out.append({"name": v, "value": mine.get(v) or found.get(v) or (choices[0] if choices else None),
                        "origin": "you" if mine.get(v) else "scan" if found.get(v) else "default", "choices": choices})
        return out

    def request_bars(self, resolution: str) -> int | None:
        """Bars one request returns (None: a symbol's whole history at once). Default: 1,000."""
        return self.per_request.get(resolution, DEFAULT_PER_REQUEST)

    def report(self, profile, today, settings: "Settings | None" = None, scanned: dict | None = None) -> Report:
        """Its standard report on this machine: `profile` is its coverage.FeedProfile (plan, asset
        classes served, markets, capacity); `settings` add its caps and keys (the scheme needs neither);
        `scanned`, its last capability scan ({at, results})."""
        from ..collector import history_floor
        from ..coverage import latency

        caps = []
        for asset in sorted(profile.assets):
            for res in self.resolutions_for(profile.tier, asset):
                since = history_floor(profile.tier, today, asset)
                if res == "second":
                    since = max(since, today - timedelta(days=profile.second_days))
                caps.append(Capability(asset, res, profile.venues.get(asset) or self.name, since,
                                       latency(profile.tier, asset), self.request_bars(res)))
        view = settings.for_source(self.name) if settings is not None else None
        limits = Limits(profile.capacity if profile.capacity is not None else float(view.max_requests_per_day if view else 0),
                        per_day=view.max_requests_per_day if view else None,
                        per_run=view.max_requests_per_run if view else None,
                        rate=self.rate_window(settings) if settings is not None else None,
                        symbols_a_month=self.symbols_per_month, published=self.limits)
        out = Report(self.name, self.title, profile.tier.name, self.keys_set(settings) if settings is not None else None,
                     limits, tuple(caps), tuple((a, w) for a, w in self.streams.items() if a in profile.assets),
                     self.kind, ("a closed archive: each symbol filled once, then not asked again",) if self.archival else (),
                     tuple((str(w), bool(ok), str(d)) for w, ok, d in (scanned or {}).get("results", [])),
                     (scanned or {}).get("at"))
        return self.reporter(out, settings) if self.reporter else out


# -- helpers for building a provider's client ----------------------------------------------------

def minute_window(rpm: int | None) -> float:
    """The seconds a limiter of `rpm` requests a minute keeps them in. A few a minute (Massive's
    free classes: 5): a couple of seconds' margin, so jitter between sending and arriving never
    puts six in the server's minute."""
    return 62.0 if rpm and rpm <= 10 else 60.0


def limiter(name: str, settings: "Settings", key: str | None = None) -> RateLimiter:
    """A feed's limiter, its window kept for the whole machine (every process and database on it)
    under the feed's name and its key's fingerprint (the public feeds' limits are per address)."""
    out = RateLimiter(*rate_window(name, settings))
    out.share(MachineLedger(name, key))
    return out


def machine_caps(client, name: str, settings: "Settings", key: str | None = None):
    """The feed's daily request cap held for the machine too, not only for this database."""
    machine = MachineLedger(name, key)
    cap = settings.for_source(name).max_requests_per_day
    client.take_day = lambda: machine.take_day(cap)
    return client


def public_client(name: str, base_url: str, settings: "Settings", request_budget: int | None):
    """A keyless public feed's client (its limits are per address: the machine's)."""
    from .public import PublicClient

    client = PublicClient(base_url, rate_limiter=limiter(name, settings), request_budget=request_budget)
    return machine_caps(client, name, settings)


def rate_window(name: str, settings: "Settings") -> tuple[int | None, float]:
    """The window each feed's limiter keeps: (requests, seconds). A little inside what each
    server publishes (Provider.limits) so a limiter never meets a refusal."""
    p = get(name)
    if p is None:
        rpm = settings.for_source(name).tier.requests_per_minute
        return rpm, minute_window(rpm)
    return p.rate_window(settings)


# -- the registry ----------------------------------------------------------------------------------

_lock = threading.RLock()
_providers: dict[str, Provider] | None = None
_errors: list[tuple[str, str]] = []


def _plugins_in(module, origin: str) -> list[Provider]:
    if isinstance(module, Provider):
        return [replace(module, origin=origin)]
    found = list(getattr(module, "PLUGINS", ()))
    if isinstance(getattr(module, "PLUGIN", None), Provider):
        found.append(module.PLUGIN)
    if not found or not all(isinstance(p, Provider) for p in found):
        raise TypeError("defines no PLUGIN (a sources.registry.Provider)")
    return [replace(p, origin=origin) for p in found]


def _entry_points() -> Iterable:
    from importlib.metadata import entry_points

    try:
        return entry_points(group=ENTRY_POINTS)
    except Exception:  # a broken installed distribution must not stop the app
        return ()


def discover() -> tuple[dict[str, Provider], list[tuple[str, str]]]:
    """Every provider plugin: the built-in modules, then the installed packages' entry points.
    Returns (providers by name in their order, [(where, why)] for the ones left out)."""
    from . import plugins

    found: list[Provider] = []
    errors: list[tuple[str, str]] = []
    for info in sorted(pkgutil.iter_modules(plugins.__path__), key=lambda m: m.name):
        where = f"{plugins.__name__}.{info.name}"
        try:
            found += _plugins_in(importlib.import_module(where), "built in")
        except Exception as e:
            errors.append((where, f"{type(e).__name__}: {e}"))
    for ep in _entry_points():
        where = f"{ep.name} ({ep.value})"
        try:
            found += _plugins_in(ep.load(), f"package: {ep.value}")
        except Exception as e:
            errors.append((where, f"{type(e).__name__}: {e}"))
    out: dict[str, Provider] = {}
    for p in sorted(found, key=lambda p: (p.order, p.name)):
        if p.name in out:
            errors.append((p.origin, f"a second provider named {p.name!r}: the first kept ({out[p.name].origin})"))
            continue
        out[p.name] = p
    return out, errors


def providers() -> dict[str, Provider]:
    """The providers by name, in their order (found once a process; `reload()` looks again)."""
    global _providers
    with _lock:
        if _providers is None:
            _providers, found_errors = discover()
            _errors[:] = found_errors
        return _providers


def reload() -> dict[str, Provider]:
    global _providers
    with _lock:
        _providers = None
        return providers()


def get(name: str) -> Provider | None:
    return providers().get(name)


def errors() -> list[tuple[str, str]]:
    """The plugins left out at loading: (module or entry point, why)."""
    providers()
    return list(_errors)


def variables() -> tuple[str, ...]:
    """Every variable a provider reads (keys, plans, options), for Settings.env."""
    return tuple(dict.fromkeys(n for p in providers().values() for n in (*p.key_names, *p.plan_vars, *p.options)))


def menu(settings: "Settings | None" = None) -> list[dict]:
    """The providers for the key script and the `plugins` command: no key's value, only whether it
    is set (when settings are given)."""
    out = []
    for p in providers().values():
        row = {"name": p.name, "title": p.title, "about": p.about, "signup": p.signup, "origin": p.origin,
               "archive": p.archival, "assets": sorted(p.assets), "keyless": sorted(p.keyless),
               "keys": [{"name": k.name, "label": k.label, "aliases": list(k.aliases)} for k in p.keys],
               "planChoices": [{"name": v, "choices": list(c)} for v, c in p.plan_choices.items()],
               "planNote": p.plan_note, "scan": p.scan is not None}
        if settings is not None:
            served = p.served(settings)
            row.update(keysSet=p.keys_set(settings), serves=sorted(served) if served else [],
                       plan=settings.for_source(p.name).tier.name, planVars=p.plan_state(settings))
        out.append(row)
    return out
