"""DEXScreener public API (https://docs.dexscreener.com/api/reference).

Rate limits per the docs: 60 req/min for profile/boost/takeover feeds,
300 req/min for pairs/search/tokens. /tokens/v1 accepts up to 30 comma-separated addresses.
There is no list-all-pairs endpoint and no history, so discovery uses the feeds + search.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from ..http import ApiClient
from ..models import Candidate, PairData, norm_address, to_float

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
FEEDS = [
    "/token-profiles/latest/v1",
    "/token-profiles/recent-updates/v1",
    "/token-boosts/latest/v1",
    "/token-boosts/top/v1",
    "/community-takeovers/latest/v1",
]
BATCH = 30


def discover_feeds(api: ApiClient, chain_ids: set[str]) -> list[tuple[str, Candidate]]:
    """Returns (dexscreener_chain_id, Candidate) for feed entries on wanted chains."""
    out = []
    for path in FEEDS:
        try:
            data = api.get_json(BASE + path, "ds_feeds") or []
        except Exception as exc:  # one feed failing shouldn't kill discovery
            log.warning("dexscreener feed %s failed: %s", path, exc)
            continue
        for item in data if isinstance(data, list) else []:
            cid, addr = item.get("chainId"), item.get("tokenAddress")
            if cid in chain_ids and addr:
                out.append((cid, Candidate(chain="", token_address=norm_address(addr), via="ds:" + path.split("/")[1])))
    return out


def discover_search(api: ApiClient, query: str, chain_ids: set[str]) -> list[tuple[str, Candidate]]:
    data = api.get_json(BASE + "/latest/dex/search", "ds_pairs", params={"q": query}) or {}
    out = []
    for pair in data.get("pairs") or []:
        cid = pair.get("chainId")
        if cid in chain_ids:
            addr = (pair.get("baseToken") or {}).get("address")
            if addr:
                liq = to_float((pair.get("liquidity") or {}).get("usd"))
                mc = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
                out.append((cid, Candidate("", norm_address(addr), "ds:search", mc, liq)))
    return out


def fetch_tokens(api: ApiClient, chain: str, ds_chain_id: str, addresses: Iterable[str]) -> dict[str, PairData]:
    """Live data for tokens, keyed by normalized token address, using each token's most liquid pair."""
    addresses = list(dict.fromkeys(norm_address(a) for a in addresses))
    result: dict[str, PairData] = {}
    for i in range(0, len(addresses), BATCH):
        chunk = addresses[i:i + BATCH]
        data = api.get_json(f"{BASE}/tokens/v1/{ds_chain_id}/{','.join(chunk)}", "ds_pairs") or []
        wanted = set(chunk)
        for pair in data if isinstance(data, list) else []:
            p = parse_pair(chain, pair)
            if p is None or p.token_address not in wanted:
                continue
            cur = result.get(p.token_address)
            if cur is None or (p.liquidity_usd or 0) > (cur.liquidity_usd or 0):
                result[p.token_address] = p
    return result


def parse_pair(chain: str, pair: dict) -> Optional[PairData]:
    base = pair.get("baseToken") or {}
    if not base.get("address") or not pair.get("pairAddress"):
        return None
    mcap = to_float(pair.get("marketCap"))
    fdv = to_float(pair.get("fdv"))
    vol = pair.get("volume") or {}
    created = to_float(pair.get("pairCreatedAt"))
    return PairData(
        chain=chain,
        token_address=norm_address(base["address"]),
        pair_address=pair["pairAddress"],
        name=base.get("name") or "",
        symbol=base.get("symbol") or "",
        price_usd=to_float(pair.get("priceUsd")),
        market_cap=mcap if mcap else fdv,
        mcap_source="marketCap" if mcap else "FDV",
        fdv=fdv,
        liquidity_usd=to_float((pair.get("liquidity") or {}).get("usd")),
        vol_h1=to_float(vol.get("h1")),
        vol_h6=to_float(vol.get("h6")),
        vol_h24=to_float(vol.get("h24")),
        price_change_h1=to_float((pair.get("priceChange") or {}).get("h1")),
        pair_created_at=created / 1000 if created else None,
        url=pair.get("url") or "",
        source="dexscreener",
    )


def chart_url(ds_chain_id: str, pair_address: str) -> str:
    return f"https://dexscreener.com/{ds_chain_id}/{pair_address}"
