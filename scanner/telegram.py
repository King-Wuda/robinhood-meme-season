"""Telegram notifications. Credentials come only from env vars."""
from __future__ import annotations

import html
import logging
import os
from typing import Optional

import httpx

from .models import PairData
from .trigger import Result

log = logging.getLogger(__name__)

CHAIN_NAMES = {"solana": "Solana", "bsc": "BNB Chain", "base": "Base", "robinhood": "Robinhood Chain"}


class Telegram:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        if not self.enabled:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: messages will only be logged")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[telegram disabled] %s", text)
            return False
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=20,
            )
            if resp.status_code != 200:
                log.error("telegram send failed: HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            log.error("telegram send failed: %s", exc)
            return False


def usd(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suffix}"
    return f"${v:.0f}"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m}m" if h else f"{m}m"


def format_alert(p: PairData, res: Result, now: float, band_min: float, chart: str) -> str:
    e = html.escape
    crossed = "n/a"
    if res.crossed_at is not None:
        lo, hi = now - res.crossed_at, now - res.crossed_after
        crossed = f"~{duration(hi)} ago" if hi - lo <= 180 else f"between {duration(lo)} and {duration(hi)} ago"
    age = duration(now - p.pair_created_at) if p.pair_created_at else "n/a"
    return "\n".join([
        f"🚀 <b>{e(p.name)} (${e(p.symbol)})</b> on {e(CHAIN_NAMES.get(p.chain, p.chain))}",
        f"Market cap: <b>{usd(p.market_cap)}</b> ({p.mcap_source})",
        f"Volume: <b>{res.volume_multiple:.1f}x</b> its own 6h hourly avg (1h vol {usd(p.vol_h1)})",
        f"Liquidity: {usd(p.liquidity_usd)}",
        f"Crossed {usd(band_min)}: {crossed}",
        f"Pair age: {age}",
        f"Chart: {e(chart)}",
        f"CA: <code>{e(p.token_address)}</code>",
        "<i>Pre-screen only. Do your own narrative research.</i>",
    ])
