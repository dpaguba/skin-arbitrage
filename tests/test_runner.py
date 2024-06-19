import pytest

from skinarb.dmarket import DMarketClient, DMarketRateLimited
from skinarb.proxies import Proxy, ProxyPool
from skinarb.runner import NoProxiesLeft, RunConfig, Runner
from skinarb.steam import SteamClient
from skinarb.store import Store
from skinarb.transport import HttpResponse

SECRET = "ab" * 32


class FakeNet:
    """Answers by endpoint instead of by call order, so concurrency stays deterministic."""

    def __init__(
        self,
        low_fee,
        dmarket_prices,
        steam_prices,
        steam_failures=None,
        corrupted_bodies=None,
        dropped_connections=None,
    ):
        self.low_fee = low_fee
        self.dmarket_prices = dmarket_prices
        self.steam_prices = steam_prices
        self.steam_failures = dict(steam_failures or {})
        self.corrupted_bodies = dict(corrupted_bodies or {})
        self.dropped_connections = dict(dropped_connections or {})
        self.steam_calls = []

    async def __call__(self, method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
        if "customized-fees" in url:
            return HttpResponse(200, {"reducedFees": [{"title": t, "fee": f} for t, f in self.low_fee]})

        if "market/items" in url:
            price = self.dmarket_prices.get(params["title"])
            objects = [{"price": {"USD": str(price)}}] if price is not None else []
            return HttpResponse(200, {"objects": objects})

        if "priceoverview" in url:
            name = params["market_hash_name"]
            self.steam_calls.append(name)
            if self.steam_failures.get(name):
                self.steam_failures[name] -= 1
                return HttpResponse(500, None)
            if self.corrupted_bodies.get(name):
                self.corrupted_bodies[name] -= 1
                return HttpResponse(200, None)
            if self.dropped_connections.get(name):
                self.dropped_connections[name] -= 1
                return HttpResponse(0, None, error="ClientConnectorError: Connection refused")
            price = self.steam_prices.get(name)
            if price is None:
                return HttpResponse(200, {"success": False})
            return HttpResponse(200, {"success": True, "lowest_price": price})

        raise AssertionError(f"unexpected url {url}")


def build(net, proxy_count=2, sleep=None, **config_kwargs):
    store = Store(":memory:")
    pool = ProxyPool(
        [Proxy(f"10.0.0.{i}", 8000, None, None) for i in range(proxy_count)], cooldown=0.0
    )
    async def no_sleep(seconds):
        return None

    dmarket = DMarketClient(net, "key", SECRET, base_url="http://dm", rps=1000.0, sleep=no_sleep, max_retries=2)
    steam = SteamClient(net, pool, base_url="http://steam/priceoverview/")
    config = RunConfig(concurrency=4, **config_kwargs)
    return Runner(store, dmarket, steam, pool, config, sleep=sleep or no_sleep), store, pool


async def test_full_pass_prices_everything():
    net = FakeNet(
        low_fee=[("A", 5.0), ("B", 2.0)],
        dmarket_prices={"A": 500, "B": 300},
        steam_prices={"A": "$8.00", "B": "$4.00"},
    )
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    assert await runner.collect(run_id) == 2
    summary = await runner.execute(run_id)

    assert summary.counts == {"priced": 2}
    assert sorted(net.steam_calls) == ["A", "B"]


async def test_item_without_a_dmarket_price_is_skipped_before_steam():
    net = FakeNet(
        low_fee=[("Delisted", None), ("B", 2.0)],
        dmarket_prices={"B": 300},
        steam_prices={"B": "$4.00"},
    )
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"skipped": 1, "priced": 1}
    assert net.steam_calls == ["B"]


async def test_item_missing_from_steam_is_unlisted():
    net = FakeNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={})
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    assert (await runner.execute(run_id)).counts == {"unlisted": 1}


async def test_skipped_and_unlisted_are_counted_separately():
    net = FakeNet(
        low_fee=[("Gone", None), ("Ghost", None)],
        dmarket_prices={"Ghost": 500},
        steam_prices={},
    )
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"skipped": 1, "unlisted": 1}


async def test_steam_errors_are_retried_then_the_item_fails():
    net = FakeNet(
        low_fee=[("A", None)],
        dmarket_prices={"A": 500},
        steam_prices={"A": "$8.00"},
        steam_failures={"A": 10},
    )
    runner, store, _ = build(net, max_attempts=3)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"failed": 1}
    assert net.steam_calls == ["A", "A", "A"]
    assert "status 500" in store._select(run_id, ("failed",))[0].last_error


async def test_a_corrupted_body_is_retried_then_the_item_fails():
    net = FakeNet(
        low_fee=[("A", None)],
        dmarket_prices={"A": 500},
        steam_prices={"A": "$8.00"},
        corrupted_bodies={"A": 10},
    )
    runner, store, _ = build(net, max_attempts=3)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"failed": 1}
    assert net.steam_calls == ["A", "A", "A"]
    last_error = store._select(run_id, ("failed",))[0].last_error
    assert "status 200 but the body was unreadable" in last_error


async def test_a_dropped_connection_is_retried_then_the_item_fails():
    net = FakeNet(
        low_fee=[("A", None)],
        dmarket_prices={"A": 500},
        steam_prices={"A": "$8.00"},
        dropped_connections={"A": 10},
    )
    runner, store, _ = build(net, max_attempts=3)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"failed": 1}
    assert net.steam_calls == ["A", "A", "A"]
    last_error = store._select(run_id, ("failed",))[0].last_error
    assert "ClientConnectorError" in last_error
    assert "Connection refused" in last_error


async def test_a_retry_that_succeeds_prices_the_item():
    net = FakeNet(
        low_fee=[("A", None)],
        dmarket_prices={"A": 500},
        steam_prices={"A": "$8.00"},
        steam_failures={"A": 1},
    )
    runner, store, _ = build(net, max_attempts=3)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    assert (await runner.execute(run_id)).counts == {"priced": 1}


async def test_resume_only_touches_unfinished_items():
    net = FakeNet(
        low_fee=[("A", None), ("B", None)],
        dmarket_prices={"A": 500, "B": 600},
        steam_prices={"A": "$8.00", "B": "$9.00"},
    )
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    store.set_dmarket_price(run_id, "A", 500)
    store.set_steam_price(run_id, "A", 800, "USD")

    await runner.execute(run_id)

    assert net.steam_calls == ["B"]


async def test_stopping_leaves_the_rest_unfinished():
    net = FakeNet(
        low_fee=[("A", None), ("B", None)],
        dmarket_prices={"A": 500, "B": 600},
        steam_prices={"A": "$8.00", "B": "$9.00"},
    )
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    runner.request_stop()
    summary = await runner.execute(run_id)

    assert summary.counts.get("priced") is None
    assert len(store.pending(run_id)) == 2
    assert summary.stopped is True
    assert store.connection.execute(
        "SELECT finished_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0] is None


async def test_a_busy_pool_is_not_mistaken_for_a_dead_one():
    net = FakeNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={"A": "$8.00"})

    handed_back = []

    async def release_on_first_wait(seconds):
        if not handed_back:
            pool.report_success(leased, latency_ms=1)
            handed_back.append(True)

    runner, store, pool = build(net, proxy_count=1, sleep=release_on_first_wait)
    leased = pool.acquire()

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert handed_back == [True]
    assert summary.counts == {"priced": 1}


def test_min_proxies_below_one_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        RunConfig(min_proxies=0)
    with pytest.raises(ValueError, match="at least 1"):
        RunConfig(min_proxies=-1)


async def test_a_dead_pool_stops_the_run():
    net = FakeNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={"A": "$8.00"})
    runner, store, pool = build(net, proxy_count=1)
    pool.report_wrong_currency(pool.acquire(), "UAH")

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    with pytest.raises(NoProxiesLeft):
        await runner.execute(run_id)


async def test_a_throttled_dmarket_aborts_instead_of_skipping_everything():
    class Throttled(FakeNet):
        async def __call__(self, method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            if "market/items" in url:
                return HttpResponse(429, None)
            return await super().__call__(method, url, params=params, headers=headers, proxy=proxy, timeout=timeout)

    net = Throttled(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={"A": "$8.00"})
    runner, store, _ = build(net)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    with pytest.raises(DMarketRateLimited):
        await runner.execute(run_id)

    assert store.counts(run_id).get("skipped") is None
    assert store.connection.execute(
        "SELECT finished_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0] is None


async def test_summary_counts_proxies():
    net = FakeNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={"A": "$8.00"})
    runner, store, _ = build(net, proxy_count=3)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.alive_proxies == 3
    assert summary.dead_proxies == 0


async def test_pool_draining_mid_run_stays_resumable_not_failed():
    net = FakeNet(
        low_fee=[("A", None), ("B", None)],
        dmarket_prices={"A": 500, "B": 600},
        steam_prices={"A": "93,84 pуб.", "B": "$9.00"},
    )
    runner, store, _ = build(net, proxy_count=1)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    with pytest.raises(NoProxiesLeft) as excinfo:
        await runner.execute(run_id)

    assert "0 live proxies" in str(excinfo.value)
    assert "1 required" in str(excinfo.value)

    assert store.counts(run_id).get("failed") is None
    assert len(store.pending(run_id)) == 2
    assert store.connection.execute(
        "SELECT finished_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0] is None


async def test_an_exception_escaping_price_one_aborts_instead_of_hanging():
    net = FakeNet(
        low_fee=[("A", None), ("B", None)],
        dmarket_prices={"A": 500, "B": 600},
        steam_prices={"A": "$8.00", "B": "$9.00"},
    )
    runner, store, _ = build(net, proxy_count=1)

    def broken_write(*args, **kwargs):
        raise OSError("disk full")

    store.set_steam_price = broken_write

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    with pytest.raises(OSError, match="disk full"):
        await runner.execute(run_id)

    assert store.connection.execute(
        "SELECT finished_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0] is None


async def test_a_second_execute_call_does_not_replay_the_first_runs_error():
    net = FakeNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={"A": "$8.00"})
    runner, store, _ = build(net, proxy_count=1)

    def broken_write(*args, **kwargs):
        raise OSError("disk full")

    store.set_steam_price = broken_write

    first_run = store.create_run("a8db")
    await runner.collect(first_run)
    with pytest.raises(OSError, match="disk full"):
        await runner.execute(first_run)

    second_run = store.create_run("a8db")

    summary = await runner.execute(second_run)

    assert summary.counts == {}


async def test_credentials_leaked_in_a_steam_exception_never_reach_the_store():
    class LeakyNet(FakeNet):
        async def __call__(self, method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            if "priceoverview" in url:
                raise RuntimeError("boom: url='http://bob:sup3rsecret@10.0.0.0:8000'")
            return await super().__call__(
                method, url, params=params, headers=headers, proxy=proxy, timeout=timeout
            )

    net = LeakyNet(low_fee=[("A", None)], dmarket_prices={"A": 500}, steam_prices={})
    runner, store, _ = build(net, max_attempts=1)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)
    summary = await runner.execute(run_id)

    assert summary.counts == {"failed": 1}
    last_error = store._select(run_id, ("failed",))[0].last_error
    assert "sup3rsecret" not in last_error
    assert "bob" not in last_error


async def test_wrong_currency_retries_without_spending_an_attempt():
    net = FakeNet(
        low_fee=[("A", None)],
        dmarket_prices={"A": 500},
        steam_prices={"A": "93,84 pуб."},
    )
    runner, store, _ = build(net, proxy_count=2)

    run_id = store.create_run("a8db")
    await runner.collect(run_id)

    with pytest.raises(NoProxiesLeft):
        await runner.execute(run_id)

    assert len(net.steam_calls) == 2
    row = store._select(run_id, ("dmarket_done",))[0]
    assert row.attempts == 0
