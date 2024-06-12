import aiohttp
import multidict
import pytest
import yarl
from aiohttp import web

from skinarb.transport import HttpResponse, aiohttp_requester, redact_credentials


@pytest.fixture()
async def base_url(aiohttp_server):
    async def ok(request):
        return web.json_response({"echo": dict(request.query)})

    async def teapot(request):
        return web.Response(status=418, text="nope")

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/teapot", teapot)
    server = await aiohttp_server(app)
    return f"http://127.0.0.1:{server.port}"


async def test_successful_request_returns_parsed_json(base_url):
    async with aiohttp.ClientSession() as session:
        request = aiohttp_requester(session)
        response = await request("GET", f"{base_url}/ok", params={"a": "1"})

    assert response == HttpResponse(status=200, json={"echo": {"a": "1"}}, error=None)


async def test_non_json_response_keeps_the_status_and_reports_no_json(base_url):
    async with aiohttp.ClientSession() as session:
        request = aiohttp_requester(session)
        response = await request("GET", f"{base_url}/teapot")

    assert response.status == 418
    assert response.json is None


async def test_connection_failure_becomes_status_zero():
    async with aiohttp.ClientSession() as session:
        request = aiohttp_requester(session)
        response = await request("GET", "http://127.0.0.1:1/nothing", timeout=0.5)

    assert response.status == 0
    assert response.error


def test_redact_credentials_strips_userinfo_from_a_url():
    text = (
        "ClientHttpProxyError: 407, message='Proxy Authentication Required', "
        "url='http://bob:sup3rsecret@10.0.0.0:8000'"
    )

    assert redact_credentials(text) == (
        "ClientHttpProxyError: 407, message='Proxy Authentication Required', "
        "url='http://10.0.0.0:8000'"
    )


def test_redact_credentials_leaves_credential_free_text_alone():
    text = "ClientConnectorError: Cannot connect to host 10.0.0.0:8000 ssl:default"
    assert redact_credentials(text) == text


class _ProxyAuthFailureSession:
    """A stand-in whose `.request()` raises inside the `async with`, the way
    aiohttp does when a proxy CONNECT fails authentication."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def request(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *exc_info) -> bool:
        return False


async def test_a_proxy_auth_error_is_redacted_before_it_reaches_the_caller():
    proxy_url = "http://bob:sup3rsecret@10.0.0.0:8000"
    request_info = aiohttp.RequestInfo(
        url=yarl.URL(proxy_url),
        method="GET",
        headers=multidict.CIMultiDictProxy(multidict.CIMultiDict()),
        real_url=yarl.URL(proxy_url),
    )
    error = aiohttp.ClientHttpProxyError(
        request_info, history=(), status=407, message="Proxy Authentication Required"
    )

    request = aiohttp_requester(_ProxyAuthFailureSession(error))
    response = await request(
        "GET", "https://steamcommunity.com/market/priceoverview/", proxy=proxy_url
    )

    assert response.status == 0
    assert "sup3rsecret" not in response.error
    assert "bob" not in response.error
    assert "10.0.0.0:8000" in response.error
