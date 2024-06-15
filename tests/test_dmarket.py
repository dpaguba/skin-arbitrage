import pytest
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from skinarb.dmarket import (
    DMarketClient,
    DMarketError,
    DMarketRateLimited,
    DMarketUnavailable,
    TokenBucket,
)
from skinarb.transport import HttpResponse


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """Stands in for the requester and hands back canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "proxy": proxy})
        return self.responses.pop(0)


SECRET = SigningKey.generate().encode(encoder=HexEncoder).decode()


async def instant(seconds):
    return None


def make_client(responses, **kwargs):
    recorder = Recorder(responses)
    kwargs.setdefault("bucket", TokenBucket(rate=1.0, clock=FakeClock(), sleep=instant))
    client = DMarketClient(recorder, api_key="key", api_secret=SECRET, base_url="http://dm", **kwargs)
    return client, recorder


async def test_token_bucket_spaces_calls_out():
    clock = FakeClock()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    bucket = TokenBucket(rate=2.0, clock=clock, sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()

    assert slept == [pytest.approx(0.5)]


def test_signature_verifies_against_the_public_key():
    client, _ = make_client([])
    timestamp = "1700000000"
    signature = client.sign("GET", "/path?x=1", "", timestamp)

    verify_key = SigningKey(SECRET[:64], encoder=HexEncoder).verify_key
    verify_key.verify(b"GET/path?x=1" + timestamp.encode(), bytes.fromhex(signature))


async def test_low_fee_items_reads_the_reduced_fees_shape():
    payload = {"reducedFees": [{"title": "AK-47 | Redline", "fee": 5.0}, {"title": "Glock"}]}
    client, _ = make_client([HttpResponse(200, payload)])

    assert await client.low_fee_items() == [("AK-47 | Redline", 5.0), ("Glock", None)]


async def test_low_fee_items_reads_the_objects_shape():
    payload = {"objects": [{"title": "AWP | Asiimov", "fee": 2.5}]}
    client, _ = make_client([HttpResponse(200, payload)])

    assert await client.low_fee_items() == [("AWP | Asiimov", 2.5)]


async def test_low_fee_items_skips_entries_without_a_title():
    payload = {"reducedFees": [{"fee": 1.0}, {"title": "Real", "fee": 1.0}]}
    client, _ = make_client([HttpResponse(200, payload)])

    assert await client.low_fee_items() == [("Real", 1.0)]


async def test_low_fee_items_raises_on_a_bad_status():
    client, _ = make_client([HttpResponse(403, None, "forbidden")])

    with pytest.raises(RuntimeError, match="403"):
        await client.low_fee_items()


async def test_price_keeps_cents_as_integers():
    payload = {"objects": [{"price": {"USD": "1234"}}]}
    client, _ = make_client([HttpResponse(200, payload)])

    assert await client.price("AK-47 | Redline") == 1234


async def test_a_non_numeric_price_raises_dmarket_error_not_a_bare_value_error():
    payload = {"objects": [{"price": {"USD": "not-a-number"}}]}
    client, _ = make_client([HttpResponse(200, payload)])

    with pytest.raises(DMarketError, match="not-a-number"):
        await client.price("AK-47 | Redline")


@pytest.mark.parametrize(
    "payload",
    [{"objects": []}, {"objects": [{"price": {}}]}, {"objects": [{}]}, {}],
)
async def test_price_returns_none_only_for_an_answered_empty_market(payload):
    client, _ = make_client([HttpResponse(200, payload)])

    assert await client.price("Nothing") is None


async def test_a_dropped_connection_is_retried_then_raises():
    client, recorder = make_client(
        [HttpResponse(0, None, "ClientConnectorError")] * 3, sleep=instant, max_retries=3
    )

    with pytest.raises(DMarketUnavailable, match="ClientConnectorError"):
        await client.price("AK-47 | Redline")

    assert len(recorder.calls) == 3


async def test_a_transient_server_fault_recovers():
    payload = {"objects": [{"price": {"USD": "100"}}]}
    client, recorder = make_client(
        [HttpResponse(500, None), HttpResponse(200, payload)], sleep=instant
    )

    assert await client.price("AK-47 | Redline") == 100
    assert len(recorder.calls) == 2


async def test_a_client_error_is_not_retried():
    client, recorder = make_client([HttpResponse(403, None, "forbidden")])

    with pytest.raises(DMarketError, match="403"):
        await client.price("AK-47 | Redline")

    assert len(recorder.calls) == 1


async def test_price_requests_are_public_and_never_use_a_proxy():
    payload = {"objects": [{"price": {"USD": "100"}}]}
    client, recorder = make_client([HttpResponse(200, payload)])
    await client.price("AK-47 | Redline")

    call = recorder.calls[0]
    assert call["proxy"] is None
    assert call["headers"] is None


async def test_low_fee_requests_are_signed():
    client, recorder = make_client([HttpResponse(200, {"reducedFees": []})])
    await client.low_fee_items()

    headers = recorder.calls[0]["headers"]
    assert headers["X-Api-Key"] == "key"
    assert headers["X-Request-Sign"].startswith("dmar ed25519 ")


async def test_rate_limit_backs_off_and_retries():
    payload = {"objects": [{"price": {"USD": "100"}}]}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    client, recorder = make_client(
        [HttpResponse(429, None), HttpResponse(429, None), HttpResponse(200, payload)],
        sleep=sleep,
    )

    assert await client.price("AK-47 | Redline") == 100
    assert slept == [2.0, 4.0]
    assert len(recorder.calls) == 3


async def test_rate_limit_raises_after_the_retry_budget():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    client, recorder = make_client([HttpResponse(429, None)] * 4, sleep=sleep, max_retries=4)

    with pytest.raises(DMarketRateLimited):
        await client.price("AK-47 | Redline")

    assert len(recorder.calls) == 4
    assert slept == [2.0, 4.0, 8.0]
