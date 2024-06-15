"""DMarket client. Limited by the API key, so it never touches the proxy pool."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from skinarb.transport import Requester

DEFAULT_BASE_URL = "https://api.dmarket.com"
DEFAULT_GAME_ID = "a8db"


class DMarketError(RuntimeError):
    """DMarket did not give us an answer we can act on."""


class DMarketRateLimited(DMarketError):
    """The key stayed rate limited for the whole retry budget."""


class DMarketUnavailable(DMarketError):
    """The endpoint kept failing or kept dropping the connection."""


def _worth_retrying(status: int) -> bool:
    return status == 0 or status == 429 or 500 <= status < 600


class TokenBucket:
    """Lets one call through every 1 / rate seconds."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._next_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next_at = self._clock() + self._interval


class DMarketClient:
    def __init__(
        self,
        requester: Requester,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        rps: float = 1.0,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 4,
        backoff_base: float = 2.0,
    ) -> None:
        self._request = requester
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._bucket = bucket or TokenBucket(rps)
        self._sleep = sleep
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def sign(self, method: str, full_path: str, body: str, timestamp: str) -> str:
        message = method + full_path + body + timestamp
        signing_key = SigningKey(self._api_secret[:64], encoder=HexEncoder)
        return signing_key.sign(message.encode()).signature.hex()

    def _headers(self, method: str, full_path: str, body: str = "") -> dict[str, str]:
        timestamp = str(int(time.time()))
        return {
            "X-Api-Key": self._api_key,
            "X-Sign-Date": timestamp,
            "X-Request-Sign": f"dmar ed25519 {self.sign(method, full_path, body, timestamp)}",
            "Content-Type": "application/json",
        }

    async def _get(self, url: str, *, params=None, headers=None, timeout: float = 20.0):
        """One GET through the bucket, retrying what is worth retrying."""
        delay = self._backoff_base
        response = None

        for attempt in range(self._max_retries):
            await self._bucket.acquire()
            response = await self._request("GET", url, params=params, headers=headers, timeout=timeout)

            if not _worth_retrying(response.status):
                return response

            if attempt < self._max_retries - 1:
                await self._sleep(delay)
                delay *= 2

        if response.status == 429:
            raise DMarketRateLimited(
                f"DMarket kept answering 429 after {self._max_retries} attempts"
            )

        detail = f", {response.error}" if response.error else ""
        raise DMarketUnavailable(
            f"DMarket did not answer after {self._max_retries} attempts: "
            f"last status {response.status}{detail}"
        )

    async def low_fee_items(
        self, game_id: str = DEFAULT_GAME_ID, limit: int = 11000
    ) -> list[tuple[str, float | None]]:
        full_path = f"/exchange/v1/customized-fees?gameId={game_id}&offerType=dmarket&limit={limit}"
        response = await self._get(
            self._base_url + full_path, headers=self._headers("GET", full_path), timeout=30.0
        )

        if response.status != 200 or response.json is None:
            raise DMarketError(f"DMarket returned {response.status}: {response.error or 'no payload'}")

        raw: list[dict[str, Any]] = response.json.get("reducedFees") or response.json.get("objects") or []
        items: list[tuple[str, float | None]] = []
        for entry in raw:
            title = entry.get("title")
            if not title:
                continue
            fee = entry.get("fee")
            items.append((title, float(fee) if fee is not None else None))
        return items

    async def price(self, title: str, game_id: str = DEFAULT_GAME_ID) -> int | None:
        params = {
            "gameId": game_id,
            "title": title,
            "limit": 1,
            "currency": "USD",
            "orderBy": "price",
            "orderDir": "asc",
        }
        response = await self._get(
            f"{self._base_url}/exchange/v1/market/items", params=params, timeout=20.0
        )

        if response.status != 200 or response.json is None:
            raise DMarketError(f"DMarket answered {response.status} for {title!r}")

        objects = response.json.get("objects") or []
        if not objects:
            return None

        raw_price = (objects[0].get("price") or {}).get("USD")
        if raw_price in (None, ""):
            return None
        try:
            return int(raw_price)
        except (TypeError, ValueError) as error:
            raise DMarketError(
                f"DMarket returned a non-numeric price {raw_price!r} for {title!r}"
            ) from error
