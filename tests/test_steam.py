import asyncio

import pytest

from skinarb.proxies import Proxy, ProxyPool, ProxyState
from skinarb.steam import SteamClient, SteamOutcome
from skinarb.transport import HttpResponse


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.on_call = None

    async def __call__(self, method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
        self.calls.append({"url": url, "params": params, "proxy": proxy})
        if self.on_call is not None:
            self.on_call()
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_client(responses, proxy_count=1, **pool_kwargs):
    clock = FakeClock()
    proxies = [Proxy(f"10.0.0.{i}", 8000, "bob", "secret") for i in range(proxy_count)]
    pool = ProxyPool(proxies, clock=clock, cooldown=10.0, **pool_kwargs)
    recorder = Recorder(responses)
    client = SteamClient(recorder, pool, base_url="http://steam/priceoverview/", clock=clock)
    return client, pool, recorder, clock


async def test_successful_price_returns_cents_and_rests_the_proxy():
    payload = {"success": True, "lowest_price": "$1,093.84"}
    client, pool, recorder, _ = make_client([HttpResponse(200, payload)])

    result = await client.price("AWP | Dragon Lore (Factory New)")

    assert result.outcome is SteamOutcome.OK
    assert result.cents == 109384
    assert pool.acquire() is None
    assert recorder.calls[0]["proxy"] == "http://bob:secret@10.0.0.0:8000"
    assert recorder.calls[0]["params"]["market_hash_name"] == "AWP | Dragon Lore (Factory New)"


async def test_a_median_price_alone_is_not_a_live_ask():
    payload = {"success": True, "median_price": "$2.50"}
    client, pool, _, _ = make_client([HttpResponse(200, payload)])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.NOT_FOUND
    assert result.cents is None
    assert pool.stats()[0].ok == 1


async def test_rate_limit_is_reported_and_the_proxy_waits_longer():
    client, pool, _, clock = make_client([HttpResponse(429, None)])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.RATE_LIMITED
    clock.advance(10.0)
    assert pool.acquire() is None


async def test_foreign_currency_retires_the_proxy():
    payload = {"success": True, "lowest_price": "93,84 pуб."}
    client, pool, _, _ = make_client([HttpResponse(200, payload)])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.WRONG_CURRENCY
    assert result.currency == "RUB"
    assert pool.alive_count() == 0
    assert pool.stats()[0].state == ProxyState.DEAD


async def test_item_not_on_the_market_does_not_punish_the_proxy():
    client, pool, _, _ = make_client([HttpResponse(200, {"success": False})])

    result = await client.price("Delisted")

    assert result.outcome is SteamOutcome.NOT_FOUND
    assert pool.stats()[0].ok == 1


async def test_unreadable_price_is_an_error_but_the_proxy_stays_healthy():
    client, pool, _, _ = make_client([HttpResponse(200, {"success": True, "lowest_price": "--"})])

    result = await client.price("Weird")

    assert result.outcome is SteamOutcome.ERROR
    assert pool.stats()[0].ok == 1


async def test_transport_failure_counts_against_the_proxy():
    client, pool, _, _ = make_client([HttpResponse(0, None, "ClientConnectorError")])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.ERROR
    assert pool.stats()[0].errors == 1


async def test_a_200_with_an_unreadable_body_names_the_real_problem():
    client, pool, _, _ = make_client([HttpResponse(200, None)])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.ERROR
    assert result.error == "status 200 but the body was unreadable"
    assert pool.stats()[0].errors == 1


async def test_no_free_proxy_returns_without_calling_the_transport():
    client, pool, recorder, _ = make_client([HttpResponse(200, {"success": True, "lowest_price": "$1.00"})])
    pool.acquire()

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.NO_PROXY
    assert recorder.calls == []


async def test_latency_is_measured_with_the_injected_clock():
    client, pool, recorder, clock = make_client(
        [HttpResponse(200, {"success": True, "lowest_price": "$1.00"})]
    )
    recorder.on_call = lambda: clock.advance(0.25)

    await client.price("Anything")

    assert pool.stats()[0].median_latency_ms == 250


async def test_cancellation_reports_the_lease_before_propagating():
    client, pool, _, _ = make_client([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await client.price("Anything")

    assert pool.stats()[0].errors == 1
    assert pool.next_ready_in() != float("inf")


async def test_a_non_string_price_is_an_error_and_leaves_the_proxy_healthy():
    client, pool, _, _ = make_client([HttpResponse(200, {"success": True, "lowest_price": 1.5})])

    result = await client.price("Weird")

    assert result.outcome is SteamOutcome.ERROR
    assert pool.stats()[0].ok == 1


async def test_a_leaked_credential_in_an_escaping_exception_is_redacted():
    leaky = RuntimeError("boom: url='http://bob:sup3rsecret@10.0.0.0:8000'")
    client, pool, _, _ = make_client([leaky])

    result = await client.price("Anything")

    assert result.outcome is SteamOutcome.ERROR
    assert "sup3rsecret" not in result.error
    assert "bob" not in result.error
