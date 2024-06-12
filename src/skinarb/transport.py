"""One thin wrapper over aiohttp so clients can be tested without a network."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp


@dataclass(frozen=True)
class HttpResponse:
    status: int
    json: dict[str, Any] | None
    error: str | None = None


Requester = Callable[..., Awaitable[HttpResponse]]

_USERINFO_IN_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@")


def redact_credentials(text: str) -> str:
    """Strip `user:pass@`-style userinfo from any URL-shaped substring."""
    return _USERINFO_IN_URL.sub(r"\1", text)


def aiohttp_requester(session: aiohttp.ClientSession) -> Requester:
    async def request(
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        try:
            async with session.request(
                method,
                url,
                params=params,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict):
                    payload = None
                return HttpResponse(status=response.status, json=payload)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            message = redact_credentials(f"{type(error).__name__}: {error}")
            return HttpResponse(status=0, json=None, error=message)

    return request
