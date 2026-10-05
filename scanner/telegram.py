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


def find_chats(token: str) -> tuple[Optional[str], list[dict]]:
    """Bot username plus the chats that have recently messaged the bot (via getUpdates).

    Telegram only reports chats that sent the bot a message in the last ~24h, so the user
    must message the bot (or post in a group it was added to) before running this.
    """
    base = f"https://api.telegram.org/bot{token}"
    me = httpx.get(f"{base}/getMe", timeout=20)
    if me.status_code != 200:
        raise ValueError(f"Telegram rejected the bot token (HTTP {me.status_code}): {me.text[:200]}")
    username = me.json().get("result", {}).get("username")
    updates = httpx.get(f"{base}/getUpdates", timeout=20).json().get("result", [])
    chats: dict[int, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = {
                "id": chat["id"], "type": chat.get("type"),
                "name": chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username") or "",
            }
    return username, list(chats.values())


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
    move = "n/a"
    if res.rise_pct is not None:
        if p.pair_created_at and res.low_ts is not None and abs(res.low_ts - p.pair_created_at) < 120:
            move = f"+{res.rise_pct:.0f}% since launch {duration(now - res.low_ts)} ago (first price {usd(res.low_mcap)})"
        else:
            move = f"+{res.rise_pct:.0f}% from {usd(res.low_mcap)} low {duration(now - res.low_ts)} ago"
    crossed = None
    if res.crossed_at is not None:
        lo, hi = now - res.crossed_at, now - res.crossed_after
        crossed = f"~{duration(hi)} ago" if hi - lo <= 180 else f"between {duration(lo)} and {duration(hi)} ago"
    age = duration(now - p.pair_created_at) if p.pair_created_at else "n/a"
    header = "🆕 Early launch" if res.reasons == ["early_launch"] else "🚀 Momentum"
    if res.reasons == ["volume_spike", "early_launch"]:
        header = "🚀🆕 Momentum + early launch"
    return "\n".join([
        f"{header}: <b>{e(p.name)} (${e(p.symbol)})</b> on {e(CHAIN_NAMES.get(p.chain, p.chain))}",
        f"Market cap: <b>{usd(p.market_cap)}</b> ({p.mcap_source})",
        f"Volume: <b>{res.volume_multiple or 0:.1f}x</b> its own 6h hourly avg (1h vol {usd(p.vol_h1)})",
        f"Liquidity: {usd(p.liquidity_usd)}",
        f"Move: {move}",
        *([f"Crossed {usd(band_min)}: {crossed}"] if crossed else []),
        f"Pair age: {age}",
        f"Chart: {e(chart)}",
        f"CA: <code>{e(p.token_address)}</code>",
        "<i>Pre-screen only. Do your own narrative research.</i>",
    ])
