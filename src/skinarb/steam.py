"""Steam Market prices, fetched through the proxy pool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from skinarb.money import UnparseablePrice, UnsupportedCurrency, parse_steam_price
from skinarb.proxies import ProxyPool
from skinarb.transport import Requester, redact_credentials

DEFAULT_BASE_URL = "https://steamcommunity.com/market/priceoverview/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class SteamOutcome(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    WRONG_CURRENCY = "wrong_currency"
    NOT_FOUND = "not_found"
    ERROR = "error"
    NO_PROXY = "no_proxy"


@dataclass(frozen=True)
class SteamResult:
    outcome: SteamOutcome
    cents: int | None = None
    currency: str | None = None
    error: str | None = None


class SteamClient:
    def __init__(
        self,
        requester: Requester,
        pool: ProxyPool,
        *,
        scheme: str = "http",
        base_url: str = DEFAULT_BASE_URL,
        app_id: str = "730",
        currency: str = "1",
        timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request = requester
        self._pool = pool
        self._scheme = scheme
        self._base_url = base_url
        self._app_id = app_id
        self._currency = currency
        self._timeout = timeout
        self._clock = clock

    async def price(self, market_hash_name: str) -> SteamResult:
        proxy = self._pool.acquire()
        if proxy is None:
            return SteamResult(SteamOutcome.NO_PROXY)

        params = {
            "appid": self._app_id,
            "currency": self._currency,
            "market_hash_name": market_hash_name,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://steamcommunity.com/",
        }

        started = self._clock()
        try:
            response = await self._request(
                "GET",
                self._base_url,
                params=params,
                headers=headers,
                proxy=proxy.url(self._scheme),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            self._pool.report_failure(proxy, "error")
            raise
        except Exception as error:
            self._pool.report_failure(proxy, "error")
            message = redact_credentials(f"{type(error).__name__}: {error}")
            return SteamResult(SteamOutcome.ERROR, error=message)

        latency_ms = int((self._clock() - started) * 1000)

        if response.status == 429:
            self._pool.report_failure(proxy, "rate_limited")
            return SteamResult(SteamOutcome.RATE_LIMITED)

        if response.status != 200 or response.json is None:
            self._pool.report_failure(proxy, "error")
            if response.error:
                diagnosis = response.error
            elif response.status == 200:
                diagnosis = "status 200 but the body was unreadable"
            else:
                diagnosis = f"status {response.status}"
            return SteamResult(SteamOutcome.ERROR, error=diagnosis)

        payload = response.json
        if not payload.get("success"):
            self._pool.report_success(proxy, latency_ms)
            return SteamResult(SteamOutcome.NOT_FOUND)

        raw_price = payload.get("lowest_price")

        if raw_price is None:
            self._pool.report_success(proxy, latency_ms)
            return SteamResult(SteamOutcome.NOT_FOUND)

        if not isinstance(raw_price, str):
            self._pool.report_success(proxy, latency_ms)
            return SteamResult(
                SteamOutcome.ERROR, error=f"price was {type(raw_price).__name__}, not a string"
            )

        try:
            money = parse_steam_price(raw_price)
        except UnsupportedCurrency as error:
            self._pool.report_wrong_currency(proxy, error.currency)
            return SteamResult(SteamOutcome.WRONG_CURRENCY, currency=error.currency)
        except UnparseablePrice as error:
            self._pool.report_success(proxy, latency_ms)
            return SteamResult(SteamOutcome.ERROR, error=str(error))

        self._pool.report_success(proxy, latency_ms)
        return SteamResult(SteamOutcome.OK, cents=money.cents, currency=money.currency)
