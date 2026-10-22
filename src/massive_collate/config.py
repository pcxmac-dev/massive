"""Settings and plan tiers.

Settings come from environment variables, optionally loaded from a `.env` file
(variables already set in the environment win):

    MASSIVE_API_KEY   required for any API call
    MASSIVE_TIER      free | starter | developer | advanced   (default: free)
    MASSIVE_RPM       override the tier's requests-per-minute limit (0 = unlimited)
    MASSIVE_DB        path of the SQLite database (default: ~/MassiveData/massive.db)
    MASSIVE_BASE_URL  API base URL (default: https://api.massive.com)
    MASSIVE_MAX_REQUESTS_PER_RUN   hard cap on API requests in one command (default: 250)
    MASSIVE_MAX_REQUESTS_PER_DAY   hard cap on API requests per day, all runs (default: 2000)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

DEFAULT_BASE_URL = "https://api.massive.com"
# Stock data lives on this machine, outside the repository, so it never ends up in git.
DEFAULT_DB_PATH = Path.home() / "MassiveData" / "massive.db"


@dataclass(frozen=True)
class Tier:
    name: str
    requests_per_minute: int | None  # None = unlimited
    history_years: int | None  # None = no plan limit


# Backfill depth used when a plan has no history limit.
UNLIMITED_BACKFILL_YEARS = 20

# Stocks plan limits. Check your plan on massive.com and override with
# MASSIVE_RPM if yours differs.
TIERS: dict[str, Tier] = {
    "free": Tier("free", requests_per_minute=5, history_years=2),
    "starter": Tier("starter", requests_per_minute=None, history_years=5),
    "developer": Tier("developer", requests_per_minute=None, history_years=10),
    "advanced": Tier("advanced", requests_per_minute=None, history_years=None),
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    tier: Tier
    db_path: Path
    base_url: str = DEFAULT_BASE_URL
    max_requests_per_run: int = 250
    max_requests_per_day: int = 2000

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "MASSIVE_API_KEY is not set. Set it in your environment or in a .env file "
                "(see .env.example)."
            )
        return self.api_key


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Blank lines and # comments are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
) -> Settings:
    merged: MutableMapping[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    merged.update(os.environ if env is None else env)

    tier_name = merged.get("MASSIVE_TIER", "free").strip().lower()
    if tier_name not in TIERS:
        raise ConfigError(f"Unknown MASSIVE_TIER {tier_name!r}; choose one of {', '.join(TIERS)}")
    tier = TIERS[tier_name]

    rpm = _int_setting(merged, "MASSIVE_RPM")
    if rpm is not None:
        tier = Tier(tier.name, rpm or None, tier.history_years)

    per_run = _int_setting(merged, "MASSIVE_MAX_REQUESTS_PER_RUN")
    per_day = _int_setting(merged, "MASSIVE_MAX_REQUESTS_PER_DAY")

    return Settings(
        api_key=merged.get("MASSIVE_API_KEY") or None,
        tier=tier,
        db_path=Path(merged["MASSIVE_DB"]).expanduser() if merged.get("MASSIVE_DB") else DEFAULT_DB_PATH,
        base_url=(merged.get("MASSIVE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        max_requests_per_run=250 if per_run is None else per_run,
        max_requests_per_day=2000 if per_day is None else per_day,
    )


def _int_setting(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e
    if value < 0:
        raise ConfigError(f"{name} must not be negative")
    return value
