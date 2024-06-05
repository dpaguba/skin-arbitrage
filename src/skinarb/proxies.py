"""A pool of static proxies with per address cooldown and health tracking."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import quote


class ProxyState(str, Enum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    QUARANTINED = "quarantined"
    DEAD = "dead"


@dataclass(frozen=True, repr=False)
class Proxy:
    host: str
    port: int
    username: str | None
    password: str | None

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"Proxy({self.key})"

    def url(self, scheme: str) -> str:
        if self.username and self.password:
            credentials = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        else:
            credentials = ""
        return f"{scheme}://{credentials}{self.key}"


@dataclass(frozen=True)
class ProxyStat:
    key: str
    requests: int
    ok: int
    rate_limited: int
    errors: int
    wrong_currency: int
    median_latency_ms: int | None
    state: ProxyState


@dataclass
class _Record:
    proxy: Proxy
    state: ProxyState = ProxyState.ACTIVE
    ready_at: float = 0.0
    leased: bool = False
    consecutive_failures: int = 0
    quarantines: int = 0
    requests: int = 0
    ok: int = 0
    rate_limited: int = 0
    errors: int = 0
    wrong_currency: int = 0
    latencies: list[int] = field(default_factory=list)


def parse_proxy_line(line: str) -> Proxy | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    parts = text.split(":")
    if len(parts) not in (2, 4):
        raise ValueError(f"expected host:port or host:port:user:pass, got {len(parts)} fields")

    host, port = parts[0], parts[1]
    if not port.isdigit():
        raise ValueError("port is not a number")

    username, password = (parts[2], parts[3]) if len(parts) == 4 else (None, None)
    return Proxy(host=host, port=int(port), username=username, password=password)


def load_proxies(path: str | Path) -> list[Proxy]:
    proxies: list[Proxy] = []
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        try:
            proxy = parse_proxy_line(line)
        except ValueError as error:
            raise ValueError(f"{path}, line {number}: {error}") from error
        if proxy is not None:
            proxies.append(proxy)
    return proxies


class ProxyPool:
    def __init__(
        self,
        proxies: list[Proxy],
        *,
        cooldown: float = 15.0,
        quarantine: float = 300.0,
        failures_before_quarantine: int = 3,
        rate_limit_penalty: float = 4.0,
        quarantines_before_death: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._records = {proxy.key: _Record(proxy=proxy) for proxy in proxies}
        self._cooldown = cooldown
        self._quarantine = quarantine
        self._failures_before_quarantine = failures_before_quarantine
        self._rate_limit_penalty = rate_limit_penalty
        self._quarantines_before_death = quarantines_before_death
        self._clock = clock

    def _available(self) -> list[_Record]:
        now = self._clock()
        return [
            record
            for record in self._records.values()
            if not record.leased and record.state is not ProxyState.DEAD and record.ready_at <= now
        ]

    def acquire(self) -> Proxy | None:
        candidates = self._available()
        if not candidates:
            return None

        record = min(candidates, key=lambda item: item.ready_at)
        record.leased = True
        return record.proxy

    def next_ready_in(self) -> float:
        now = self._clock()
        waiting = [
            record.ready_at
            for record in self._records.values()
            if not record.leased and record.state is not ProxyState.DEAD
        ]
        if not waiting:
            return float("inf")
        return max(0.0, min(waiting) - now)

    def alive_count(self) -> int:
        return sum(1 for record in self._records.values() if record.state is not ProxyState.DEAD)

    def report_success(self, proxy: Proxy, latency_ms: int) -> None:
        record = self._records[proxy.key]
        record.leased = False
        record.requests += 1
        record.ok += 1
        record.consecutive_failures = 0
        record.latencies.append(latency_ms)
        record.state = ProxyState.COOLDOWN
        record.ready_at = self._clock() + self._cooldown

    def report_failure(self, proxy: Proxy, kind: str) -> None:
        record = self._records[proxy.key]
        record.leased = False
        record.requests += 1
        record.consecutive_failures += 1

        if kind == "rate_limited":
            record.rate_limited += 1
            penalty = self._cooldown * self._rate_limit_penalty
        else:
            record.errors += 1
            penalty = self._cooldown

        if record.consecutive_failures >= self._failures_before_quarantine:
            record.consecutive_failures = 0
            record.quarantines += 1
            if record.quarantines >= self._quarantines_before_death:
                record.state = ProxyState.DEAD
                record.ready_at = float("inf")
                return
            record.state = ProxyState.QUARANTINED
            record.ready_at = self._clock() + self._quarantine
            return

        record.state = ProxyState.COOLDOWN
        record.ready_at = self._clock() + penalty

    def report_wrong_currency(self, proxy: Proxy, currency: str) -> None:
        record = self._records[proxy.key]
        record.leased = False
        record.requests += 1
        record.wrong_currency += 1
        record.state = ProxyState.DEAD
        record.ready_at = float("inf")

    def stats(self) -> list[ProxyStat]:
        return [
            ProxyStat(
                key=record.proxy.key,
                requests=record.requests,
                ok=record.ok,
                rate_limited=record.rate_limited,
                errors=record.errors,
                wrong_currency=record.wrong_currency,
                median_latency_ms=int(statistics.median(record.latencies)) if record.latencies else None,
                state=record.state,
            )
            for record in self._records.values()
        ]
