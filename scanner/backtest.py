"""Backtest: replay the live trigger over GeckoTerminal OHLCV history.

DEXScreener has no history, so history comes from GeckoTerminal 5-minute candles.
Historical market cap = candle close x (current market cap / current price), i.e. constant
supply. Historical liquidity is not available; the liquidity filter is skipped unless
--current-liquidity is passed (which uses today's liquidity and therefore looks ahead).
Every 5-minute candle close is treated as one poll.
"""
from __future__ import annotations

import bisect
import csv
import itertools
import json
import logging
import os
import statistics
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

from .config import THRESHOLD_KEYS, Config
from .http import ApiClient
from .models import norm_address, to_float
from .sources import geckoterminal as gt
from .trigger import HOUR, Observation, Thresholds, evaluate, in_cooldown

log = logging.getLogger(__name__)

CANDLE_SECONDS = 300
CHAIN_ALIASES = {"bnb": "bsc", "bnb chain": "bsc", "bnbchain": "bsc", "binance": "bsc", "bep20": "bsc",
                 "robinhood chain": "robinhood", "robinhood-chain": "robinhood", "sol": "solana"}
RUNNER_LABELS = {"runner", "pumped", "positive", "pos", "yes", "1", "true", "ran"}
CONTROL_LABELS = {"control", "non_runner", "non-runner", "negative", "neg", "no", "0", "false", "did_not_run"}

CAVEAT = (
    "CAVEAT: survivorship bias. Backtesting only on tokens that pumped tells you how often the "
    "trigger catches winners, not how often it fires on losers. Supply a control list of tokens "
    "that did NOT run (--controls, or a label column) to measure the false-positive rate. Also: "
    "historical market cap assumes constant supply, historical liquidity is unavailable, and "
    "5-minute candles approximate a 60-120s live poll."
)


@dataclass
class TokenInput:
    chain: str
    address: str
    label: str = "runner"
    pool: Optional[str] = None


@dataclass
class History:
    chain: str
    address: str
    pool: Optional[str] = None
    name: str = ""
    symbol: str = ""
    supply: Optional[float] = None
    mcap_source: str = ""
    created_at: Optional[float] = None
    complete: bool = False
    current_liquidity: Optional[float] = None
    candles: list = field(default_factory=list)  # [ts, o, h, l, c, v], ascending
    error: Optional[str] = None


@dataclass
class TokenResult:
    chain: str
    address: str
    symbol: str
    label: str
    history: str  # "ok" or "UNAVAILABLE: reason"
    mcap_source: str = ""
    fired: bool = False
    n_alerts: int = 0
    alert_time: str = ""
    alert_ts: Optional[float] = None
    mcap_at_alert: Optional[float] = None
    volume_multiple: Optional[float] = None
    minutes_since_cross: Optional[float] = None
    peak_mcap_after: Optional[float] = None
    peak_multiple: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    return_at_horizon_pct: Optional[float] = None
    horizon_hours_covered: Optional[float] = None
    ended_below_entry: Optional[bool] = None


# ---------------- input ----------------

def resolve_chain(cfg: Config, name: str) -> str:
    key = CHAIN_ALIASES.get(name.strip().lower(), name.strip().lower())
    for c in cfg.chains.values():
        if key in (c.name, c.dexscreener_id, c.geckoterminal_id):
            return c.name
    raise ValueError(f"unknown chain {name!r} (known: {', '.join(cfg.chains)})")


def _label(value, default: str) -> str:
    v = str(value).strip().lower() if value not in (None, "") else ""
    if v in RUNNER_LABELS:
        return "runner"
    if v in CONTROL_LABELS:
        return "control"
    return default


def load_inputs(path: str, cfg: Config, default_label: str = "runner") -> list[TokenInput]:
    if path.lower().endswith(".json"):
        with open(path) as fh:
            rows = json.load(fh)
    else:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        row = {str(k).strip().lower(): v for k, v in row.items()}
        addr = row.get("address") or row.get("token_address") or row.get("contract") or row.get("ca")
        if not addr or not row.get("chain"):
            raise ValueError(f"{path}: each row needs chain and address, got {row}")
        out.append(TokenInput(resolve_chain(cfg, row["chain"]), norm_address(addr),
                              _label(row.get("label"), default_label), row.get("pool") or None))
    return out


# ---------------- history ----------------

def fetch_history(api: ApiClient, cfg: Config, tok: TokenInput, cache_dir: Optional[str],
                  max_days: float, refresh: bool = False) -> History:
    cache_path = os.path.join(cache_dir, f"{tok.chain}_{tok.address}.json") if cache_dir else None
    if cache_path and not refresh and os.path.exists(cache_path):
        with open(cache_path) as fh:
            return History(**json.load(fh))
    h = _fetch_history(api, cfg, tok, max_days)
    if cache_path and h.error is None:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump(asdict(h), fh)
    return h


def _fetch_history(api: ApiClient, cfg: Config, tok: TokenInput, max_days: float) -> History:
    chain = cfg.chains[tok.chain]
    net = chain.geckoterminal_id
    h = History(chain=tok.chain, address=tok.address)
    if not net:
        h.error = f"no GeckoTerminal network configured for {tok.chain}"
        return h
    try:
        info = api.get_json(f"{gt.BASE}/networks/{net}/tokens/{tok.address}", "gt")
        if not info or not info.get("data"):
            h.error = "token not found on GeckoTerminal"
            return h
        a = info["data"]["attributes"]
        h.name, h.symbol = a.get("name") or "", a.get("symbol") or ""
        price = to_float(a.get("price_usd"))
        mcap, fdv = to_float(a.get("market_cap_usd")), to_float(a.get("fdv_usd"))
        if not price:
            h.error = "no current price (cannot derive supply)"
            return h
        h.supply, h.mcap_source = ((mcap / price), "marketCap") if mcap else ((fdv / price) if fdv else None, "FDV")
        if not h.supply:
            h.error = "no market cap / FDV (cannot derive supply)"
            return h

        pools = gt.token_pools(api, net, tok.address)
        if tok.pool:
            pool = next((p for p in pools if norm_address(p["attributes"]["address"]) == norm_address(tok.pool)), None)
            h.pool = tok.pool
        else:
            pool = max(pools, key=lambda p: to_float(p["attributes"].get("reserve_in_usd")) or 0, default=None)
            h.pool = pool["attributes"]["address"] if pool else None
        if not h.pool:
            h.error = "no pools found"
            return h
        if pool:
            h.created_at = gt.parse_ts(pool["attributes"].get("pool_created_at"))
            h.current_liquidity = to_float(pool["attributes"].get("reserve_in_usd"))

        candles: dict[int, list] = {}
        before = None
        oldest_allowed = time.time() - max_days * 86400
        while True:
            batch = gt.ohlcv(api, net, h.pool, tok.address, before)
            if not batch:
                h.complete = True
                break
            for c in batch:
                candles[int(c[0])] = [float(x) for x in c]
            oldest = min(int(c[0]) for c in batch)
            if (h.created_at and oldest <= h.created_at + CANDLE_SECONDS) or len(batch) < 1000:
                h.complete = True
                break
            if oldest < oldest_allowed:
                break
            before = oldest
        h.candles = [candles[k] for k in sorted(candles)]
        if not h.candles:
            h.error = "no OHLCV candles returned"
        elif h.created_at is None and h.complete:
            h.created_at = h.candles[0][0]
    except Exception as exc:
        h.error = f"API error: {exc}"
    return h


# ---------------- replay ----------------

def replay(h: History, th: Thresholds, label: str, cooldown_hours: float, horizon_hours: float,
           current_liquidity: bool = False) -> TokenResult:
    r = TokenResult(h.chain, h.address, h.symbol, label, "ok", h.mcap_source)
    if h.error:
        r.history = f"UNAVAILABLE: {h.error}"
        return r
    c = h.candles
    opens = [x[0] for x in c]
    closes_t = [x[0] + CANDLE_SECONDS for x in c]
    mcaps = [x[4] * h.supply for x in c]
    cum = [0.0]
    for x in c:
        cum.append(cum[-1] + x[5])

    def vol(t: float, seconds: float) -> float:
        return cum[bisect.bisect_left(opens, t)] - cum[bisect.bisect_left(opens, t - seconds)]

    start_t = closes_t[0] if h.complete else closes_t[0] + 6 * HOUR  # need full 6h window
    liq = h.current_liquidity if current_liquidity else None
    alerts = []
    last_alert = None
    for i, t in enumerate(closes_t):
        if t < start_t:
            continue
        lo = bisect.bisect_left(closes_t, t - th.lookback_hours * HOUR - 1)
        history = list(zip(closes_t[lo:i], mcaps[lo:i]))
        obs = Observation(t, mcaps[i], vol(t, HOUR), vol(t, 6 * HOUR), liq, h.created_at)
        res = evaluate(obs, history, th, require_liquidity=current_liquidity)
        if res.fired and not in_cooldown(last_alert, t, cooldown_hours):
            alerts.append((i, t, res))
            last_alert = t

    r.n_alerts = len(alerts)
    if not alerts:
        return r
    i, t, res = alerts[0]
    r.fired = True
    r.alert_ts = t
    r.alert_time = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    r.mcap_at_alert = mcaps[i]
    r.volume_multiple = round(res.volume_multiple, 2)
    r.minutes_since_cross = round((t - res.crossed_at) / 60, 1) if res.crossed_at else None
    after = [x for x in c[i + 1:] if x[0] < t + horizon_hours * HOUR]
    if after:
        peak = max(x[2] for x in after) * h.supply
        trough = min(x[3] for x in after) * h.supply
        end = after[-1][4] * h.supply
        r.peak_mcap_after = peak
        r.peak_multiple = round(peak / r.mcap_at_alert, 2)
        r.max_drawdown_pct = round(min(0.0, trough / r.mcap_at_alert - 1) * 100, 1)  # worst point vs entry
        r.return_at_horizon_pct = round((end / r.mcap_at_alert - 1) * 100, 1)
        r.horizon_hours_covered = round((after[-1][0] + CANDLE_SECONDS - t) / HOUR, 1)
        r.ended_below_entry = end < r.mcap_at_alert
    return r


# ---------------- summary ----------------

def summarize(results: list[TokenResult]) -> dict:
    ok = [r for r in results if r.history == "ok"]
    runners = [r for r in ok if r.label == "runner"]
    controls = [r for r in ok if r.label == "control"]
    fired = [r for r in ok if r.fired]
    peaks = [r.peak_multiple for r in fired if r.peak_multiple is not None]
    runner_peaks = [r.peak_multiple for r in runners if r.fired and r.peak_multiple is not None]
    return {
        "tokens": len(results),
        "history_unavailable": len(results) - len(ok),
        "runners": len(runners),
        "runners_fired": sum(r.fired for r in runners),
        "hit_rate": _ratio(sum(r.fired for r in runners), len(runners)),
        "controls": len(controls),
        "controls_fired": sum(r.fired for r in controls),
        "false_positive_rate": _ratio(sum(r.fired for r in controls), len(controls)),
        "alerts": len(fired),
        "median_peak_multiple": round(statistics.median(peaks), 2) if peaks else None,
        "median_peak_multiple_runners": round(statistics.median(runner_peaks), 2) if runner_peaks else None,
        "alerts_ended_below_entry": sum(1 for r in fired if r.ended_below_entry),
        "alerts_drawdown_over_50pct": sum(1 for r in fired if (r.max_drawdown_pct or 0) <= -50),
    }


def _ratio(a: int, b: int) -> Optional[float]:
    return round(a / b, 3) if b else None


def format_summary(s: dict, horizon_hours: float) -> str:
    pct = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"
    return "\n".join([
        f"Tokens: {s['tokens']} ({s['history_unavailable']} with history UNAVAILABLE, listed above)",
        f"Runners: {s['runners']}, fired on {s['runners_fired']} -> hit rate {pct(s['hit_rate'])}",
        f"Controls: {s['controls']}, fired on {s['controls_fired']} -> false-positive rate {pct(s['false_positive_rate'])}"
        + ("" if s["controls"] else "  (no control list supplied)"),
        f"Alerts: {s['alerts']}; median alert-to-peak multiple {s['median_peak_multiple']} "
        f"(runners only: {s['median_peak_multiple_runners']}) within {horizon_hours:g}h",
        f"Alerts that ended below entry after {horizon_hours:g}h: {s['alerts_ended_below_entry']}; "
        f"drew down 50%+ at some point: {s['alerts_drawdown_over_50pct']}",
    ])


# ---------------- driver ----------------

def parse_sweep(specs: list[str]) -> list[dict]:
    axes = {}
    for spec in specs or []:
        key, _, values = spec.partition("=")
        key = key.strip()
        if key not in THRESHOLD_KEYS:
            raise ValueError(f"--sweep key {key!r} must be one of {sorted(THRESHOLD_KEYS)}")
        axes[key] = [float(v) for v in values.split(",") if v.strip()]
    if not axes:
        return [{}]
    return [dict(zip(axes, combo)) for combo in itertools.product(*axes.values())]


def run(cfg: Config, api: ApiClient, inputs: list[TokenInput], out_dir: str, sweep: list[str],
        horizon_hours: float = 24, max_days: float = 14, cache_dir: Optional[str] = "data/history_cache",
        refresh: bool = False, current_liquidity: bool = False) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    histories = []
    for n, tok in enumerate(inputs, 1):
        log.info("[%d/%d] history %s %s", n, len(inputs), tok.chain, tok.address)
        histories.append((tok, fetch_history(api, cfg, tok, cache_dir, max_days, refresh)))

    def results_for(overrides: dict) -> list[TokenResult]:
        return [replay(h, replace(cfg.chains[tok.chain].thresholds, **overrides), tok.label,
                       cfg.alert_cooldown_hours, horizon_hours, current_liquidity) for tok, h in histories]

    combos = parse_sweep(sweep)
    base = results_for({})
    _write_csv(os.path.join(out_dir, "per_token.csv"), [asdict(r) for r in base])
    summary = summarize(base)

    report = [CAVEAT, "", "Per-token results (config thresholds):"]
    for r in base:
        if r.history != "ok":
            report.append(f"  {r.chain:<10} {r.address}  {r.label:<7} history {r.history}")
        elif not r.fired:
            report.append(f"  {r.chain:<10} {r.symbol or r.address:<14} {r.label:<7} fired: no")
        else:
            outcome = (f"peak {r.peak_multiple}x, max DD {r.max_drawdown_pct}%, {horizon_hours:g}h ret "
                       f"{r.return_at_horizon_pct}% ({r.horizon_hours_covered}h of data)"
                       if r.peak_multiple is not None else "no candles after alert yet")
            report.append(
                f"  {r.chain:<10} {r.symbol or r.address:<14} {r.label:<7} fired: YES at {r.alert_time} "
                f"mc {r.mcap_at_alert:,.0f} ({r.mcap_source}), vol {r.volume_multiple}x, {outcome}, "
                f"alerts {r.n_alerts}")
    report += ["", "Summary:", format_summary(summary, horizon_hours)]
    if not current_liquidity:
        report.append("Liquidity filter NOT applied (no historical liquidity). Use --current-liquidity to "
                      "apply today's liquidity (lookahead).")

    if combos != [{}]:
        rows = []
        for combo in combos:
            s = summarize(results_for(combo))
            rows.append({**combo, **s})
        rows.sort(key=lambda r: ((r["hit_rate"] or 0) - (r["false_positive_rate"] or 0),
                                 r["median_peak_multiple"] or 0), reverse=True)
        _write_csv(os.path.join(out_dir, "sweep.csv"), rows)
        keys = list(combos[0])
        report += ["", f"Parameter sweep ({len(rows)} combos, best first; full table in sweep.csv):"]
        for r in rows[:15]:
            params = ", ".join(f"{k}={_num(r[k])}" for k in keys)
            report.append(f"  {params}: hit {_p(r['hit_rate'])}, FP {_p(r['false_positive_rate'])}, "
                          f"alerts {r['alerts']}, median peak {_x(r['median_peak_multiple'])}, "
                          f"ended below entry {r['alerts_ended_below_entry']}")

    report += ["", CAVEAT]
    text = "\n".join(report)
    with open(os.path.join(out_dir, "report.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    return summary


def _x(v) -> str:
    return "n/a" if v is None else f"{v}x"


def _num(v: float) -> str:
    return f"{v:.0f}" if float(v).is_integer() else f"{v:g}"


def _p(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
