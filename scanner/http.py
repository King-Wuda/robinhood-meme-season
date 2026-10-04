"""HTTP client with per-endpoint-family rate limiting and 429/5xx backoff."""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)


class RateLimiter:
    """Evenly spaced calls: at most `per_minute`, never in bursts (GeckoTerminal 429s on bursts)."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, int(per_minute))
        self.next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            time.sleep(self.next_at - now)
            now = self.next_at
        self.next_at = now + self.interval


class ApiError(Exception):
    pass


class ApiClient:
    def __init__(self, limits: dict[str, int], timeout: float = 20.0, max_retries: int = 5):
        self.limiters = {name: RateLimiter(n) for name, n in limits.items()}
        self.max_retries = max_retries
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "meme-momentum-scanner/1.0"},
        )

    def get_json(self, url: str, bucket: str, params: Optional[dict] = None):
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            self.limiters[bucket].wait()
            try:
                resp = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise ApiError(f"GET {url} failed: {exc}") from exc
                log.warning("GET %s network error (%s), retry in %.0fs", url, exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise ApiError(f"GET {url} -> HTTP {resp.status_code} after {attempt} retries")
                retry_after = _retry_after(resp)
                delay = max(backoff, retry_after or 0)
                log.warning("GET %s -> HTTP %s, retry in %.0fs", url, resp.status_code, delay)
                time.sleep(delay)
                backoff = min(backoff * 2, 120)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise ApiError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise ApiError(f"GET {url}: retries exhausted")

    def post_json(self, url: str, payload: dict):
        return self.client.post(url, json=payload)

    def close(self) -> None:
        self.client.close()


def _retry_after(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
