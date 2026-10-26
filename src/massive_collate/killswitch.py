"""The kill policy: a feed that misbehaves is cut off, not retried.

Being banned by a feed costs far more than missing some of its data, so every connection to a
feed (each live stream, and each feed's fetching) is killed on the first sign it is unwelcome
and left off for a while, longer each time:

- **Refused: killed at once.** The server says no: a rate limit (429, "rate limit exceeded"), a
  connection or subscription limit ("subscription limit reached"), keys or plan refused (401,
  403, Alpaca's 402/406/409), or a policy close of a stream (WebSocket 1008, 1013, 4000-4999).
- **Failing: killed after a few in a row.** A live stream that drops within STABLE_SECONDS of
  connecting FLAP_LIMIT times within FLAP_WINDOW_S (the server accepting and dropping it; a
  connection that could not be made at all, the network down, is not one: it only backs off); a
  fetch run whose server fails (5xx, no answer) on FETCH_FAILURES symbols in a row, or whose run
  ends in a server error.
- **For how long:** KILL_MINUTES, one step further for each kill within STRIKE_MEMORY_H of the
  last (30 min, 1 h, 2 h, 4 h, 8 h, then 24 h each time); a day without one starts over.
- **Everywhere:** kills are kept in the database (kv `kill:<feed>:<live|fetch>`), so every
  process honours them (the recorder, the hourly sync, a run the chart starts) and a restart
  does not clear them. Only the user does: `python -m massive_collate kills release FEED`.
  `kills kill FEED` cuts one off by hand; `kills` lists them with each feed's outcomes. The Fetcher view shows a killed
  feed as "killed", the Live view its stream.
- **One recorder per machine, strictly:** the recorder holding the machine lock ends any other
  recorder process it finds (live.py, reap_strays): duplicates double every connection to the
  exchanges, which is how the "subscription limit reached" storms began.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KINDS = ("live", "fetch")
KILL_MINUTES = (30, 60, 120, 240, 480, 1440)
STRIKE_MEMORY_H = 24  # kills within this of the last one climb the ladder; after it, start over
FLAP_LIMIT = 4  # short-lived connections ...
FLAP_WINDOW_S = 900  # ... within this many seconds: a stream the server keeps dropping
FETCH_FAILURES = 3  # symbols in a row whose server failed (5xx, no answer): the feed is down

# What a refusal looks like, in the words the feeds use (lower case).
REFUSAL_WORDS = ("rate limit", "too many", "subscription limit", "limit reached", "limit exceeded",
                 "unauthorized", "forbidden", "authentication", "not authenticated", "auth failed",
                 "insufficient subscription", "connection limit", "banned", "blocked")
REFUSAL_STATUS = (401, 403, 418, 429, 451)
REFUSAL_CLOSE = (1008, 1013)  # policy violation, try again later (and 4000-4999: the server's own)


def key(feed: str, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    return f"kill:{feed}:{kind}"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def is_refusal(error: BaseException | str, status: int | None = None) -> bool:
    """Whether an error is the server saying no (as opposed to a fault on the way): by HTTP
    status, a WebSocket close code, a handshake answer, or the feed's own words."""
    if status in REFUSAL_STATUS:
        return True
    text = str(error).lower()
    if any(w in text for w in REFUSAL_WORDS):
        return True
    for code in REFUSAL_STATUS:
        if f"upgrade refused: http/1.1 {code}" in text or f"http {code}" in text:
            return True
    if "closed by the server (" in text:
        try:
            code = int(text.split("closed by the server (", 1)[1].split(")", 1)[0])
        except ValueError:
            return False
        return code in REFUSAL_CLOSE or 4000 <= code <= 4999
    return False


def killed(store, feed: str, kind: str, now: datetime | None = None) -> dict | None:
    """The kill in force on a feed's live stream or fetching: {until, why, at, strikes}, or None."""
    k = store.kv_json(key(feed, kind), {})
    try:
        return k if datetime.fromisoformat(k["until"]) > _now(now) else None
    except (KeyError, TypeError, ValueError):
        return None


def strike(store, feed: str, kind: str, why: str, now: datetime | None = None, minutes: float | None = None,
           log=None) -> dict:
    """Kill a feed's live stream or fetching: for the next step of KILL_MINUTES (or `minutes`, a
    kill by hand, which replaces any kill in force). Returns the kill. A strike while already
    killed is the same kill: it does not climb the ladder, and is neither logged nor recorded again."""
    now = _now(now)
    fresh = [True]

    def climb(k: dict) -> dict:
        try:
            if minutes is None and datetime.fromisoformat(k["until"]) > now:
                fresh[0] = False
                return {**k, "why": why[:300]}  # already off: the same kill, the latest reason
            recent = now - datetime.fromisoformat(k["at"]) < timedelta(hours=STRIKE_MEMORY_H)
        except (KeyError, TypeError, ValueError):
            recent = False
        strikes = (k.get("strikes", 0) if recent else 0) + 1
        length = minutes if minutes is not None else KILL_MINUTES[min(strikes, len(KILL_MINUTES)) - 1]
        return {"at": now.isoformat(timespec="seconds"), "until": (now + timedelta(minutes=length)).isoformat(timespec="seconds"),
                "why": why[:300], "strikes": strikes, "by": "user" if minutes is not None else "policy"}

    k = store.kv_update(key(feed, kind), climb, {})
    if fresh[0]:
        store.feed_event(feed, kind, "killed", detail=f"until {k['until']} (strike {k['strikes']}): {why}", now=now)
        if log:
            log(f"KILLED {feed} {kind} until {k['until'][:16].replace('T', ' ')} UTC (strike {k['strikes']}): {why}")
    return k


def release(store, feed: str, kinds=KINDS) -> list[str]:
    """Lift the kills on a feed (the user's call: it also forgets its strikes). Returns those lifted."""
    lifted = []
    for kind in kinds:
        if store.kv_json(key(feed, kind)):
            store.kv_set(key(feed, kind), "")
            store.feed_event(feed, kind, "released", detail="by the user")
            lifted.append(kind)
    return lifted


def kills(store, feeds, now: datetime | None = None) -> list[dict]:
    """The kills in force: [{feed, kind, until, why, strikes, by}]."""
    out = []
    for feed in feeds:
        for kind in KINDS:
            k = killed(store, feed, kind, now)
            if k:
                out.append({"feed": feed, "kind": kind, **k})
    return out


class Flapping:
    """A live stream's short-lived connections: `drop` says whether the latest makes FLAP_LIMIT
    within FLAP_WINDOW_S (the stream is to be killed)."""

    def __init__(self):
        self.drops: list[float] = []

    def drop(self, at: float) -> bool:
        self.drops = [t for t in self.drops if at - t < FLAP_WINDOW_S] + [at]
        return len(self.drops) >= FLAP_LIMIT

    def clear(self) -> None:
        self.drops = []
