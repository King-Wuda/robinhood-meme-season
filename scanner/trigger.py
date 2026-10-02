"""The momentum trigger. Pure functions, shared by the live poller and the backtest."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

HOUR = 3600.0


@dataclass(frozen=True)
class Thresholds:
    market_cap_min: float = 1_000_000
    market_cap_max: float = 10_000_000
    lookback_hours: float = 3
    volume_multiplier: float = 3
    min_liquidity_usd: float = 50_000
    min_pair_age_minutes: float = 15


@dataclass
class Observation:
    ts: float
    market_cap: Optional[float]
    vol_h1: Optional[float]
    vol_h6: Optional[float]
    liquidity_usd: Optional[float]
    pair_created_at: Optional[float]


@dataclass
class Result:
    fired: bool
    failed: list[str] = field(default_factory=list)
    volume_multiple: Optional[float] = None
    crossed_after: Optional[float] = None  # last point seen below market_cap_min
    crossed_at: Optional[float] = None  # first point seen at/above it afterwards (crossing is between the two)


def volume_multiple(vol_h1: Optional[float], vol_h6: Optional[float], pair_age_hours: Optional[float]) -> Optional[float]:
    """Trailing 1h volume divided by the trailing 6h hourly average.

    For pairs younger than 6h the 6h window only holds `age` hours of trading, so the
    hourly average is vol_h6 / age (clamped to [1, 6]). A pair under 1h old therefore
    always scores 1.0x: it has no history of its own to spike against.
    """
    if vol_h1 is None or vol_h6 is None:
        return None
    hours = 6.0 if pair_age_hours is None else min(6.0, max(1.0, pair_age_hours))
    avg = vol_h6 / hours
    if avg <= 0:
        return None
    return vol_h1 / avg


def find_crossover(now: float, market_cap_min: float, lookback_hours: float,
                   history: Sequence[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """If market cap was below `market_cap_min` within the lookback window, return
    (last point below, first point at/above after it, or `now`): the crossing lies between them.

    `history` is (ts, market_cap) points strictly before `now`, any order.
    """
    window_start = now - lookback_hours * HOUR
    points = sorted((ts, mc) for ts, mc in history if mc is not None and ts < now)
    last_below = None
    for ts, mc in points:
        if ts >= window_start and mc < market_cap_min:
            last_below = ts
    if last_below is None:
        return None
    after = [ts for ts, mc in points if ts > last_below and mc >= market_cap_min]
    return last_below, (after[0] if after else now)


def evaluate(obs: Observation, history: Sequence[tuple[float, float]], th: Thresholds,
             require_liquidity: bool = True) -> Result:
    """All of: market cap in band, crossed up from below the band within the lookback,
    1h volume >= multiplier x own 6h hourly average, liquidity and pair-age floors."""
    res = Result(fired=False)
    mc = obs.market_cap

    if mc is None or not (th.market_cap_min <= mc <= th.market_cap_max):
        res.failed.append("market_cap_band")

    crossing = find_crossover(obs.ts, th.market_cap_min, th.lookback_hours, history)
    if crossing is None:
        res.failed.append("crossover")
    else:
        res.crossed_after, res.crossed_at = crossing

    age_h = None if obs.pair_created_at is None else (obs.ts - obs.pair_created_at) / HOUR
    res.volume_multiple = volume_multiple(obs.vol_h1, obs.vol_h6, age_h)
    if res.volume_multiple is None or res.volume_multiple < th.volume_multiplier:
        res.failed.append("volume_spike")

    if require_liquidity and (obs.liquidity_usd is None or obs.liquidity_usd < th.min_liquidity_usd):
        res.failed.append("liquidity")

    if age_h is None or age_h * 60 < th.min_pair_age_minutes:
        res.failed.append("pair_age")

    res.fired = not res.failed
    return res


def in_cooldown(last_alert_ts: Optional[float], now: float, cooldown_hours: float) -> bool:
    return last_alert_ts is not None and now - last_alert_ts < cooldown_hours * HOUR
