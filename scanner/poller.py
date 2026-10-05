"""Live polling loop: discover -> refresh -> snapshot -> evaluate -> alert -> prune."""
from __future__ import annotations

import logging
import time
from typing import Callable

from .config import ChainConfig, Config
from .db import Store
from .http import ApiClient
from .models import Candidate, PairData
from .sources import dexscreener as ds
from .sources import geckoterminal as gt
from .telegram import Telegram, format_alert, usd
from .trigger import HOUR, Observation, evaluate, in_cooldown

log = logging.getLogger(__name__)


def make_api(cfg: Config) -> ApiClient:
    return ApiClient({
        "ds_feeds": cfg.dexscreener_rpm_feeds,
        "ds_pairs": cfg.dexscreener_rpm_pairs,
        "gt": cfg.geckoterminal_rpm,
    })


class Poller:
    def __init__(self, cfg: Config, store: Store, api: ApiClient, telegram: Telegram,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.api = api
        self.telegram = telegram
        self.clock = clock
        self.rejected: dict[tuple[str, str], float] = {}

    # ---------- discovery ----------
    def discover(self, errors: list[str]) -> dict[str, dict[str, Candidate]]:
        disc = self.cfg.discovery
        by_ds_id = {c.dexscreener_id: c.name for c in self.cfg.chains.values()}
        found: list[tuple[str, Candidate]] = []

        if disc["dexscreener_feeds"]:
            for cid, cand in ds.discover_feeds(self.api, set(by_ds_id)):
                found.append((by_ds_id[cid], cand))
        for q in disc["dexscreener_search_queries"]:
            try:
                for cid, cand in ds.discover_search(self.api, q, set(by_ds_id)):
                    found.append((by_ds_id[cid], cand))
            except Exception as exc:
                errors.append(f"dexscreener search {q!r}: {exc}")
        if disc["geckoterminal"]:
            for chain in self.cfg.chains.values():
                if chain.geckoterminal_id:
                    for cand in gt.discover(self.api, chain.geckoterminal_id, int(disc["geckoterminal_pages"])):
                        found.append((chain.name, cand))

        out: dict[str, dict[str, Candidate]] = {}
        for chain_name, cand in found:
            cand.chain = chain_name
            chain = self.cfg.chains[chain_name]
            if not self._passes_prefilter(chain, cand.market_cap, cand.liquidity_usd):
                continue
            out.setdefault(chain_name, {}).setdefault(cand.token_address, cand)
        return out

    def _passes_prefilter(self, chain: ChainConfig, mc, liq) -> bool:
        """Cheap filter on numbers a discovery payload already carries (None = unknown, keep)."""
        upper = chain.thresholds.market_cap_max * self.cfg.drop_above_band_multiple
        if mc is not None and (mc < chain.discovery_min_market_cap or mc > upper):
            return False
        if liq is not None and liq < chain.discovery_min_liquidity_usd:
            return False
        return True

    # ---------- per chain ----------
    def fetch(self, chain: ChainConfig, addresses: list[str]) -> dict[str, PairData]:
        if not addresses:
            return {}
        if chain.source == "geckoterminal":
            return gt.fetch_tokens(self.api, chain.name, chain.geckoterminal_id, addresses)
        return ds.fetch_tokens(self.api, chain.name, chain.dexscreener_id, addresses)

    def process_chain(self, chain: ChainConfig, candidates: dict[str, Candidate], now: float) -> int:
        tracked = {r["token_address"]: r for r in self.store.tracked(chain.name)}
        recheck = self.cfg.discovery["recheck_rejected_minutes"] * 60
        new = [a for a in candidates if a not in tracked
               and now - self.rejected.get((chain.name, a), -1e18) >= recheck]
        data = self.fetch(chain, list(tracked) + new)

        for addr in new:
            p = data.get(addr)
            if p and self._passes_prefilter(chain, p.market_cap, p.liquidity_usd) and p.market_cap is not None:
                self.store.track(chain.name, addr, candidates[addr].via, now)
                tracked[addr] = None
                log.info("tracking %s %s (%s) mc=%s via %s", chain.name, p.symbol, addr, usd(p.market_cap),
                         candidates[addr].via)
            else:
                self.rejected[(chain.name, addr)] = now

        alerts = 0
        for addr in list(tracked):
            p = data.get(addr)
            row = tracked[addr]
            if p is None:
                last_seen = (row["last_seen"] or row["first_seen"]) if row else now
                if now - last_seen > HOUR:
                    log.info("dropping %s %s: no data for >1h", chain.name, addr)
                    self.store.drop(chain.name, addr)
                continue
            alerts += self.evaluate_token(chain, p, now)
            self.store.add_snapshot(p, now)
            if self._should_drop(chain, p, row, now):
                self.store.drop(chain.name, addr)

        self._enforce_cap(chain, data)
        return alerts

    def _should_drop(self, chain: ChainConfig, p: PairData, row, now: float) -> bool:
        first_seen = row["first_seen"] if row else now
        last_active = (row["last_active"] if row else None) or first_seen
        if (p.vol_h1 or 0) > 0:
            last_active = now
        reason = None
        if now - last_active > self.cfg.stale_hours * HOUR or (p.vol_h24 == 0 and now - first_seen > HOUR):
            reason = "stale (no volume)"
        elif p.market_cap is not None and p.market_cap < self.cfg.dead_market_cap \
                and now - first_seen > self.cfg.dead_after_hours * HOUR:
            reason = f"dead (mc {usd(p.market_cap)})"
        elif p.market_cap is not None and p.market_cap > chain.thresholds.market_cap_max * self.cfg.drop_above_band_multiple:
            reason = f"far above band (mc {usd(p.market_cap)})"
        if reason:
            log.info("dropping %s %s (%s): %s", chain.name, p.symbol, p.token_address, reason)
        return reason is not None

    def _enforce_cap(self, chain: ChainConfig, data: dict[str, PairData]) -> None:
        rows = self.store.tracked(chain.name)
        excess = len(rows) - int(self.cfg.max_tracked_tokens_per_chain)
        if excess <= 0:
            return
        rows.sort(key=lambda r: (data[r["token_address"]].vol_h1 or 0) if r["token_address"] in data else -1)
        for r in rows[:excess]:
            self.store.drop(chain.name, r["token_address"])
        log.info("%s: evicted %d lowest-volume tokens (cap %s)", chain.name, excess,
                 self.cfg.max_tracked_tokens_per_chain)

    def evaluate_token(self, chain: ChainConfig, p: PairData, now: float) -> int:
        th = chain.thresholds
        history = self.store.mcap_history(chain.name, p.token_address, now - th.lookback_hours * HOUR - 600)
        history = [(ts, mc) for ts, mc in history if ts < now]
        if self.cfg.use_price_change_inference and p.market_cap and p.price_change_h1 is not None \
                and p.price_change_h1 > -99.9:
            # The API's 1h price change gives us the market cap one hour ago (constant supply),
            # so a crossover is detectable even for tokens we only just started tracking.
            # For a pair younger than 1h that change is measured from its first trade, so the
            # point belongs at launch time, not an hour ago.
            then = now - HOUR
            if p.pair_created_at and p.pair_created_at > then:
                then = p.pair_created_at
            history.append((then, p.market_cap / (1 + p.price_change_h1 / 100)))
        obs = Observation(now, p.market_cap, p.vol_h1, p.vol_h6, p.liquidity_usd, p.pair_created_at)
        res = evaluate(obs, history, th)
        if len(res.failed) == 1:
            log.debug("near miss %s %s: failed %s (vol %.1fx, mc %s)", chain.name, p.symbol, res.failed[0],
                      res.volume_multiple or 0, usd(p.market_cap))
        if not res.fired:
            return 0
        if in_cooldown(self.store.last_alert_ts(chain.name, p.token_address), now, self.cfg.alert_cooldown_hours):
            log.info("trigger %s %s suppressed by cooldown", chain.name, p.symbol)
            return 0
        chart = ds.chart_url(chain.dexscreener_id, p.pair_address) if chain.source == "dexscreener" else p.url
        msg = format_alert(p, res, now, th.market_cap_min, chart)
        log.info("ALERT %s %s %s mc=%s vol=%.1fx via %s", chain.name, p.symbol, p.token_address,
                 usd(p.market_cap), res.volume_multiple or 0, "+".join(res.reasons))
        if self.telegram.send(msg) or not self.telegram.enabled:
            self.store.record_alert(chain.name, p.token_address, now, p.market_cap, res.volume_multiple or 0, msg)
            return 1
        return 0  # send failed: not recorded, so it can retry next poll while still firing

    # ---------- loop ----------
    def run_once(self) -> list[str]:
        now = self.clock()
        errors: list[str] = []
        candidates = self.discover(errors)
        for chain in self.cfg.chains.values():
            try:
                n = self.process_chain(chain, candidates.get(chain.name, {}), now)
                log.info("%s: %d tracked, %d discovery candidates, %d alerts", chain.name,
                         len(self.store.tracked(chain.name)), len(candidates.get(chain.name, {})), n)
            except Exception as exc:
                log.exception("%s: poll failed", chain.name)
                errors.append(f"{chain.name}: {exc}")
            self.store.commit()
        self.store.prune_snapshots(now - self.cfg.snapshot_retention_hours * HOUR)
        self.rejected = {k: t for k, t in self.rejected.items() if now - t < 6 * HOUR}
        self.store.commit()
        return errors

    def run_forever(self) -> None:
        self.telegram.send(startup_message(self.cfg))
        consecutive = 0
        last_error_alert = 0.0
        while True:
            started = time.monotonic()
            try:
                errors = self.run_once()
            except Exception as exc:
                log.exception("poll crashed")
                errors = [f"poll crashed: {exc}"]
            if errors:
                consecutive += 1
                log.warning("poll had errors (%d consecutive): %s", consecutive, "; ".join(errors))
                if consecutive >= self.cfg.error_alert_threshold and \
                        time.time() - last_error_alert > self.cfg.error_alert_repeat_minutes * 60:
                    self.telegram.send(f"⚠️ Scanner: {consecutive} consecutive polls with errors.\n"
                                       + "\n".join(errors[:5])[:3000])
                    last_error_alert = time.time()
            else:
                if consecutive >= self.cfg.error_alert_threshold and last_error_alert:
                    self.telegram.send("✅ Scanner recovered, polls are succeeding again.")
                consecutive = 0
                last_error_alert = 0.0
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, self.cfg.poll_interval_seconds - elapsed))


def startup_message(cfg: Config) -> str:
    lines = ["🟢 Meme momentum scanner started"]
    for c in cfg.chains.values():
        t = c.thresholds
        lines.append(f"• {c.name} via {c.source}: band {usd(t.market_cap_min)}-{usd(t.market_cap_max)}, "
                     f"up ≥{t.min_mcap_rise_pct:g}% in {t.lookback_hours:g}h"
                     + (" (must cross band min)" if t.require_crossover else "")
                     + f", vol ≥{t.volume_multiplier:g}x, "
                     f"liq ≥{usd(t.min_liquidity_usd)}, age ≥{t.min_pair_age_minutes:g}m, "
                     + (f"early-launch window {t.launch_window_hours:g}h" if t.launch_window_hours > 0
                        else "early-launch off"))
    lines.append(f"Poll every {cfg.poll_interval_seconds}s, cooldown {cfg.alert_cooldown_hours}h")
    return "\n".join(lines)
