"""GeckoTerminal public API (https://apiguide.geckoterminal.com).

Used for: broader discovery (new + trending pools per network), an optional live source
for chains DEXScreener covers poorly, and OHLCV history for backtesting.
Free tier is documented at roughly 30 calls/min but 429s on bursts well below that;
we default to 15/min, evenly spaced, with backoff.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Optional

from ..http import ApiClient
from ..models import Candidate, PairData, norm_address, to_float

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
BATCH = 30


def _id_address(rel_id: str) -> str:
    """'robinhood_0xabc' -> '0xabc' (network ids never contain '_' after the prefix split)."""
    return rel_id.split("_", 1)[1] if "_" in rel_id else rel_id


def parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def discover(api: ApiClient, network: str, pages: int = 1) -> list[Candidate]:
    out = []
    for kind, params in (("new_pools", {}), ("trending_pools", {"duration": "1h"})):
        for page in range(1, pages + 1):
            try:
                data = api.get_json(f"{BASE}/networks/{network}/{kind}", "gt", params={"page": page, **params}) or {}
            except Exception as exc:
                log.warning("geckoterminal %s/%s failed: %s", network, kind, exc)
                break
            for pool in data.get("data") or []:
                a = pool.get("attributes") or {}
                base_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
                if not base_id:
                    continue
                mc = to_float(a.get("market_cap_usd")) or to_float(a.get("fdv_usd"))
                out.append(Candidate("", norm_address(_id_address(base_id)), f"gt:{kind}", mc,
                                     to_float(a.get("reserve_in_usd"))))
    return out


def parse_pool(chain: str, pool: dict, tokens: dict[str, dict]) -> Optional[PairData]:
    a = pool.get("attributes") or {}
    base_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
    tok = tokens.get(base_id, {})
    mcap = to_float(a.get("market_cap_usd"))
    fdv = to_float(a.get("fdv_usd"))
    vol = a.get("volume_usd") or {}
    network = pool.get("id", "_").split("_", 1)[0]
    return PairData(
        chain=chain,
        token_address=norm_address(_id_address(base_id)),
        pair_address=a.get("address", ""),
        name=tok.get("name") or (a.get("name") or "").split(" / ")[0],
        symbol=tok.get("symbol") or (a.get("name") or "").split(" / ")[0],
        price_usd=to_float(a.get("base_token_price_usd")),
        market_cap=mcap if mcap else fdv,
        mcap_source="marketCap" if mcap else "FDV",
        fdv=fdv,
        liquidity_usd=to_float(a.get("reserve_in_usd")),
        vol_h1=to_float(vol.get("h1")),
        vol_h6=to_float(vol.get("h6")),
        vol_h24=to_float(vol.get("h24")),
        price_change_h1=to_float((a.get("price_change_percentage") or {}).get("h1")),
        pair_created_at=parse_ts(a.get("pool_created_at")),
        url=f"https://www.geckoterminal.com/{network}/pools/{a.get('address', '')}",
        source="geckoterminal",
    )


def fetch_tokens(api: ApiClient, chain: str, network: str, addresses: Iterable[str]) -> dict[str, PairData]:
    """Live data via each token's top pool: tokens/multi (top_pools) -> pools/multi."""
    addresses = list(dict.fromkeys(norm_address(a) for a in addresses))
    top_pool: dict[str, str] = {}
    for i in range(0, len(addresses), BATCH):
        chunk = addresses[i:i + BATCH]
        data = api.get_json(f"{BASE}/networks/{network}/tokens/multi/{','.join(chunk)}", "gt",
                            params={"include": "top_pools"}) or {}
        for tok in data.get("data") or []:
            pools = ((tok.get("relationships") or {}).get("top_pools") or {}).get("data") or []
            if pools:
                top_pool[norm_address(tok["attributes"]["address"])] = _id_address(pools[0]["id"])
    result: dict[str, PairData] = {}
    pool_addrs = list(dict.fromkeys(top_pool.values()))
    for i in range(0, len(pool_addrs), BATCH):
        chunk = pool_addrs[i:i + BATCH]
        data = api.get_json(f"{BASE}/networks/{network}/pools/multi/{','.join(chunk)}", "gt",
                            params={"include": "base_token"}) or {}
        tokens = {t["id"]: t.get("attributes", {}) for t in data.get("included") or []}
        for pool in data.get("data") or []:
            p = parse_pool(chain, pool, tokens)
            if p and top_pool.get(p.token_address) == p.pair_address:
                result[p.token_address] = p
    return result


# --- backtest helpers ---

def token_pools(api: ApiClient, network: str, token: str) -> list[dict]:
    data = api.get_json(f"{BASE}/networks/{network}/tokens/{token}/pools", "gt", params={"page": 1}) or {}
    return data.get("data") or []


def ohlcv(api: ApiClient, network: str, pool: str, token: str, before: Optional[int] = None,
          aggregate: int = 5, limit: int = 1000) -> list[list[float]]:
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": token}
    if before:
        params["before_timestamp"] = int(before)
    data = api.get_json(f"{BASE}/networks/{network}/pools/{pool}/ohlcv/minute", "gt", params=params) or {}
    return ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []


def network_exists(api: ApiClient, network: str) -> bool:
    data = api.get_json(f"{BASE}/networks/{network}/new_pools", "gt", params={"page": 1})
    return bool(data and data.get("data"))
