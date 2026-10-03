"""The momentum trigger. Pure functions, shared by the live poller and the backtest."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

HOUR = 3600.0


@dataclass(frozen=True)
class Thresholds:
    market_cap_min: float = 1_000_000
    market_cap_max: float = 10_000_000
    lookback_hours: float = 3  # window for the move (and for the optional crossover)
    # The move: market cap must be up at least this % from its lowest point in the lookback
    # window, so only tokens that were recently *pushed* into the band fire.
    min_mcap_rise_pct: float = 50
    # Optional stricter rule: the window low must also be under market_cap_min (i.e. it crossed
    # up into the band). Off by default: a push from $2M to $6M counts too.
    require_crossover: bool = False
    volume_multiplier: float = 3
    min_liquidity_usd: float = 50_000
    min_pair_age_minutes: float = 15
    # Early-launch path: if the move started within the token's first `launch_window_hours`,
    # it fires without the relative volume spike (a day-old token has no baseline of its own
    # to spike against). 0 disables it.
    launch_window_hours: float = 24
    # Optional floor for the early-launch path: 1h volume >= this fraction of market cap.
    launch_min_volume_to_mcap: float = 0


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
    rise_pct: Optional[float] = None  # % up from the window low
    low_mcap: Optional[float] = None  # lowest market cap in the lookback window
    low_ts: Optional[float] = None  # when that low was
    crossed_after: Optional[float] = None  # last point below market_cap_min in the window, if any
    crossed_at: Optional[float] = None  # first point at/above it afterwards (crossing is between the two)
    reasons: list[str] = field(default_factory=list)  # which path fired: "volume_spike", "early_launch"


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


def window_low(now: float, lookback_hours: float,
               history: Sequence[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """(ts, market_cap) of the lowest point within the lookback window, or None if no points."""
    start = now - lookback_hours * HOUR
    points = [(mc, ts) for ts, mc in history if mc is not None and start <= ts < now]
    if not points:
        return None
    mc, ts = min(points)
    return ts, mc


def is_early_launch(obs: Observation, move_start: Optional[float], th: Thresholds) -> bool:
    """Did the move start (window low) within the token's first launch_window_hours?"""
    if th.launch_window_hours <= 0 or move_start is None or obs.pair_created_at is None:
        return False
    if move_start - obs.pair_created_at > th.launch_window_hours * HOUR:
        return False
    if th.launch_min_volume_to_mcap > 0:
        if not obs.market_cap or (obs.vol_h1 or 0) < th.launch_min_volume_to_mcap * obs.market_cap:
            return False
    return True


def evaluate(obs: Observation, history: Sequence[tuple[float, float]], th: Thresholds,
             require_liquidity: bool = True) -> Result:
    """All of:
      - market cap in [market_cap_min, market_cap_max];
      - the move: market cap up >= min_mcap_rise_pct from its low in the lookback window
        (and, if require_crossover, that low was under market_cap_min);
      - EITHER 1h volume >= volume_multiplier x own 6h hourly average (volume spike)
        OR the move started within the token's first launch_window_hours (early launch);
      - liquidity and pair-age floors.
    """
    res = Result(fired=False)
    mc = obs.market_cap

    if mc is None or not (th.market_cap_min <= mc <= th.market_cap_max):
        res.failed.append("market_cap_band")

    low = window_low(obs.ts, th.lookback_hours, history)
    if low is not None and mc:
        res.low_ts, res.low_mcap = low
        res.rise_pct = (mc / res.low_mcap - 1) * 100 if res.low_mcap > 0 else None
    if res.rise_pct is None or res.rise_pct < th.min_mcap_rise_pct:
        res.failed.append("mcap_rise")

    crossing = find_crossover(obs.ts, th.market_cap_min, th.lookback_hours, history)
    if crossing is not None:
        res.crossed_after, res.crossed_at = crossing
    elif th.require_crossover:
        res.failed.append("crossover")

    age_h = None if obs.pair_created_at is None else (obs.ts - obs.pair_created_at) / HOUR
    res.volume_multiple = volume_multiple(obs.vol_h1, obs.vol_h6, age_h)
    if res.volume_multiple is not None and res.volume_multiple >= th.volume_multiplier:
        res.reasons.append("volume_spike")
    if is_early_launch(obs, res.low_ts, th):
        res.reasons.append("early_launch")
    if not res.reasons:
        res.failed.append("volume_spike")

    if require_liquidity and (obs.liquidity_usd is None or obs.liquidity_usd < th.min_liquidity_usd):
        res.failed.append("liquidity")

    if age_h is None or age_h * 60 < th.min_pair_age_minutes:
        res.failed.append("pair_age")

    res.fired = not res.failed
    return res


def in_cooldown(last_alert_ts: Optional[float], now: float, cooldown_hours: float) -> bool:
    return last_alert_ts is not None and now - last_alert_ts < cooldown_hours * HOUR
