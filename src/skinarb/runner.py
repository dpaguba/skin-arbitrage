"""Two lanes: DMarket walks the list, Steam workers drain the queue behind it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from skinarb.dmarket import DMarketClient
from skinarb.proxies import ProxyPool, ProxyState
from skinarb.steam import SteamClient, SteamOutcome
from skinarb.store import Store

STOP = object()


class NoProxiesLeft(RuntimeError):
    """Every address in the pool is dead, there is nothing left to work with."""


@dataclass(frozen=True)
class RunConfig:
    game_id: str = "a8db"
    limit: int | None = None
    concurrency: int = 100
    min_proxies: int = 1
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.min_proxies < 1:
            raise ValueError(f"min_proxies must be at least 1, got {self.min_proxies}")


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    counts: dict[str, int] = field(default_factory=dict)
    alive_proxies: int = 0
    dead_proxies: int = 0
    stopped: bool = False


class Runner:
    def __init__(
        self,
        store: Store,
        dmarket: DMarketClient,
        steam: SteamClient,
        pool: ProxyPool,
        config: RunConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._dmarket = dmarket
        self._steam = steam
        self._pool = pool
        self._config = config
        self._sleep = sleep
        self._stopping = False
        self._proxies_exhausted = False
        self._worker_error: Exception | None = None

    def request_stop(self) -> None:
        self._stopping = True

    async def collect(self, run_id: int) -> int:
        items = await self._dmarket.low_fee_items(self._config.game_id)
        if self._config.limit is not None:
            items = items[: self._config.limit]
        return self._store.add_items(run_id, items)

    async def execute(self, run_id: int) -> RunSummary:
        self._worker_error = None
        self._proxies_exhausted = False

        if self._pool.alive_count() < self._config.min_proxies:
            raise NoProxiesLeft(
                f"{self._pool.alive_count()} live proxies, {self._config.min_proxies} required"
            )

        queue: asyncio.Queue = asyncio.Queue()
        for row in self._store.needing_steam(run_id):
            queue.put_nowait(row.title)

        worker_count = max(1, min(self._pool.alive_count(), self._config.concurrency))
        workers = [
            asyncio.create_task(self._steam_worker(run_id, queue)) for _ in range(worker_count)
        ]

        try:
            await self._dmarket_lane(run_id, queue)
            await queue.join()
        finally:
            for _ in workers:
                queue.put_nowait(STOP)
            await asyncio.gather(*workers)
            stats = self._pool.stats()
            self._store.save_proxy_stats(run_id, stats)

        if self._worker_error is not None:
            raise self._worker_error
        if self._proxies_exhausted:
            raise NoProxiesLeft(
                f"{self._pool.alive_count()} live proxies, {self._config.min_proxies} required"
            )

        if not self._stopping:
            self._store.finish_run(run_id)

        return RunSummary(
            run_id=run_id,
            counts=self._store.counts(run_id),
            alive_proxies=sum(1 for s in stats if s.state is not ProxyState.DEAD),
            dead_proxies=sum(1 for s in stats if s.state is ProxyState.DEAD),
            stopped=self._stopping,
        )

    async def _dmarket_lane(self, run_id: int, queue: asyncio.Queue) -> None:
        for row in self._store.pending(run_id):
            if self._stopping:
                return
            if row.status != "pending":
                continue

            cents = await self._dmarket.price(row.title, self._config.game_id)
            if cents is None:
                self._store.mark_skipped(run_id, row.title)
                continue

            self._store.set_dmarket_price(run_id, row.title, cents)
            queue.put_nowait(row.title)

    async def _steam_worker(self, run_id: int, queue: asyncio.Queue) -> None:
        while True:
            title = await queue.get()
            try:
                if title is STOP:
                    return
                await self._price_one(run_id, title)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._worker_error is None:
                    self._worker_error = error
                self._stopping = True
            finally:
                queue.task_done()

    async def _price_one(self, run_id: int, title: str) -> None:
        while not self._stopping:
            result = await self._steam.price(title)

            if result.outcome is SteamOutcome.NO_PROXY:
                if self._pool.alive_count() < self._config.min_proxies:
                    self._proxies_exhausted = True
                    self._stopping = True
                    return

                await self._sleep(min(self._pool.next_ready_in(), 1.0))
                continue

            if result.outcome is SteamOutcome.OK:
                self._store.set_steam_price(run_id, title, result.cents, result.currency)
                return

            if result.outcome is SteamOutcome.NOT_FOUND:
                self._store.mark_unlisted(run_id, title)
                return

            if result.outcome is SteamOutcome.WRONG_CURRENCY:
                continue

            reason = result.error or result.outcome.value
            if self._store.bump_attempt(run_id, title) >= self._config.max_attempts:
                self._store.mark_failed(run_id, title, reason)
                return
