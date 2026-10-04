"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def norm_address(address: str) -> str:
    """EVM addresses are case-insensitive, Solana addresses are not."""
    address = address.strip()
    return address.lower() if address.startswith("0x") else address


@dataclass
class PairData:
    """Live market data for one token, taken from its most liquid pair."""

    chain: str
    token_address: str
    pair_address: str
    name: str
    symbol: str
    price_usd: Optional[float]
    market_cap: Optional[float]
    mcap_source: str  # "marketCap" or "FDV"
    fdv: Optional[float]
    liquidity_usd: Optional[float]
    vol_h1: Optional[float]
    vol_h6: Optional[float]
    vol_h24: Optional[float]
    price_change_h1: Optional[float]  # percent
    pair_created_at: Optional[float]  # epoch seconds
    url: str
    source: str  # "dexscreener" or "geckoterminal"


@dataclass
class Candidate:
    """A token seen by discovery, optionally with preliminary numbers to pre-filter on."""

    chain: str
    token_address: str
    via: str
    market_cap: Optional[float] = None
    liquidity_usd: Optional[float] = None


def to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
