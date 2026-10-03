"""Configuration: TOML file, then env-var overrides.

Env overrides:
  SCANNER_CONFIG=path/to/config.toml
  SCANNER_CHAINS=solana,bsc,base,robinhood       (enabled chains)
  SCANNER__SECTION__KEY=value                    (any key, e.g. SCANNER__THRESHOLDS__VOLUME_MULTIPLIER=4,
                                                  SCANNER__CHAINS__ROBINHOOD__THRESHOLDS__MIN_LIQUIDITY_USD=30000)
Telegram credentials come from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID only.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

from .trigger import Thresholds

DEFAULTS: dict = {
    "scanner": {
        "poll_interval_seconds": 90,
        "db_path": "data/scanner.db",
        "log_path": "logs/scanner.log",
        "log_level": "INFO",
        "snapshot_retention_hours": 24,
        "alert_cooldown_hours": 6,
        "stale_hours": 24,
        "dead_market_cap": 10_000,
        "dead_after_hours": 2,
        "drop_above_band_multiple": 3,
        "max_tracked_tokens_per_chain": 1500,
        "use_price_change_inference": True,
        "error_alert_threshold": 5,
        "error_alert_repeat_minutes": 60,
        "dexscreener_rpm_feeds": 55,
        "dexscreener_rpm_pairs": 250,
        "geckoterminal_rpm": 15,
    },
    "thresholds": {
        "market_cap_min": 1_000_000,
        "market_cap_max": 10_000_000,
        "lookback_hours": 3,
        "volume_multiplier": 3,
        "min_liquidity_usd": 50_000,
        "min_pair_age_minutes": 15,
    },
    "discovery": {
        "min_market_cap": 30_000,
        "min_liquidity_usd": 5_000,
        "dexscreener_feeds": True,
        "dexscreener_search_queries": [],
        "geckoterminal": True,
        "geckoterminal_pages": 1,
        "recheck_rejected_minutes": 15,
    },
    "chains": {
        "solana": {"enabled": True, "source": "dexscreener", "dexscreener_id": "solana", "geckoterminal_id": "solana"},
        "bsc": {"enabled": True, "source": "dexscreener", "dexscreener_id": "bsc", "geckoterminal_id": "bsc"},
        "base": {"enabled": True, "source": "dexscreener", "dexscreener_id": "base", "geckoterminal_id": "base"},
        "robinhood": {"enabled": True, "source": "dexscreener", "dexscreener_id": "robinhood", "geckoterminal_id": "robinhood"},
    },
}

THRESHOLD_KEYS = {f.name for f in fields(Thresholds)}
THRESHOLD_TYPES = {f.name: str(f.type) for f in fields(Thresholds)}


@dataclass
class ChainConfig:
    name: str
    source: str
    dexscreener_id: str
    geckoterminal_id: Optional[str]
    thresholds: Thresholds
    discovery_min_market_cap: float
    discovery_min_liquidity_usd: float


@dataclass
class Config:
    raw: dict
    chains: dict[str, ChainConfig] = field(default_factory=dict)

    def __getattr__(self, item):
        # Convenience: cfg.poll_interval_seconds etc. from [scanner].
        scanner = self.__dict__.get("raw", {}).get("scanner", {})
        if item in scanner:
            return scanner[item]
        raise AttributeError(item)

    @property
    def discovery(self) -> dict:
        return self.raw["discovery"]

    def chain_by_ds_id(self, ds_id: str) -> Optional[ChainConfig]:
        for c in self.chains.values():
            if c.dexscreener_id == ds_id:
                return c
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse_env_value(value: str):
    try:
        return tomllib.loads(f"v = {value}")["v"]
    except tomllib.TOMLDecodeError:
        return value


def _apply_env(raw: dict, env: dict) -> dict:
    for key, value in env.items():
        if not key.startswith("SCANNER__"):
            continue
        path = [p.lower() for p in key[len("SCANNER__"):].split("__") if p]
        node = raw
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = _parse_env_value(value)
    if env.get("SCANNER_CHAINS"):
        wanted = {c.strip().lower() for c in env["SCANNER_CHAINS"].split(",") if c.strip()}
        for name, c in raw["chains"].items():
            c["enabled"] = name in wanted
        for name in wanted - set(raw["chains"]):
            raw["chains"][name] = {"enabled": True}
    return raw


def _coerce(key: str, value):
    if key not in THRESHOLD_KEYS:
        raise ValueError(f"unknown threshold key: {key}")
    if THRESHOLD_TYPES[key] == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return float(value)


def _thresholds(values: dict) -> Thresholds:
    return Thresholds(**{k: _coerce(k, v) for k, v in values.items()})


def load_config(path: Optional[str] = None, env: Optional[dict] = None, include_disabled: bool = False) -> Config:
    env = dict(os.environ) if env is None else env
    path = path or env.get("SCANNER_CONFIG")
    raw = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
    elif Path("config.toml").exists():
        with open("config.toml", "rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
    raw = _apply_env(raw, env)

    base_thresholds = _thresholds(raw["thresholds"])
    cfg = Config(raw=raw)
    disc = raw["discovery"]
    for name, c in raw["chains"].items():
        if not c.get("enabled", True) and not include_disabled:
            continue
        source = c.get("source", "dexscreener")
        if source not in ("dexscreener", "geckoterminal"):
            raise ValueError(f"chain {name}: source must be dexscreener or geckoterminal")
        th = replace(base_thresholds, **{k: _coerce(k, v) for k, v in c.get("thresholds", {}).items()})
        cdisc = c.get("discovery", {})
        cfg.chains[name] = ChainConfig(
            name=name,
            source=source,
            dexscreener_id=c.get("dexscreener_id", name),
            geckoterminal_id=c.get("geckoterminal_id", name),
            thresholds=th,
            discovery_min_market_cap=float(cdisc.get("min_market_cap", disc["min_market_cap"])),
            discovery_min_liquidity_usd=float(cdisc.get("min_liquidity_usd", disc["min_liquidity_usd"])),
        )
    if not cfg.chains:
        raise ValueError("no chains enabled")
    return cfg
