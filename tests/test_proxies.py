import pytest

from skinarb.proxies import (
    Proxy,
    ProxyPool,
    ProxyState,
    load_proxies,
    parse_proxy_line,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(count=2, **kwargs):
    clock = FakeClock()
    proxies = [Proxy(f"10.0.0.{i}", 8000 + i, None, None) for i in range(count)]
    return ProxyPool(proxies, clock=clock, **kwargs), clock, proxies


def test_parse_plain_and_authenticated_lines():
    assert parse_proxy_line("1.2.3.4:8080") == Proxy("1.2.3.4", 8080, None, None)
    assert parse_proxy_line("1.2.3.4:8080:bob:secret") == Proxy("1.2.3.4", 8080, "bob", "secret")


@pytest.mark.parametrize("line", ["", "   ", "# a comment", "\t# indented"])
def test_blank_and_comment_lines_are_skipped(line):
    assert parse_proxy_line(line) is None


@pytest.mark.parametrize("line", ["not-a-proxy", "1.2.3.4", "1.2.3.4:port", "1.2.3.4:80:onlyuser"])
def test_malformed_lines_raise(line):
    with pytest.raises(ValueError):
        parse_proxy_line(line)


def test_load_proxies_reports_the_offending_line_number(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:8080\n# fine\nbroken\n")
    with pytest.raises(ValueError, match="line 3"):
        load_proxies(path)


def test_a_bad_port_error_names_the_shape_not_the_line(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:notaport:bob:zqx9-super-secret-leak\n")

    with pytest.raises(ValueError, match="line 1") as excinfo:
        load_proxies(path)

    message = str(excinfo.value)
    assert "zqx9-super-secret-leak" not in message
    assert "bob" not in message
    assert "port is not a number" in message


def test_a_wrong_field_count_error_names_the_shape_not_the_line(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:8080:bob:zqx9-super-secret-leak:extra\n")

    with pytest.raises(ValueError, match="line 1") as excinfo:
        load_proxies(path)

    message = str(excinfo.value)
    assert "zqx9-super-secret-leak" not in message
    assert "bob" not in message


def test_url_carries_credentials():
    proxy = Proxy("1.2.3.4", 8080, "bob", "p@ss word")
    assert proxy.url("http") == "http://bob:p%40ss%20word@1.2.3.4:8080"
    assert Proxy("1.2.3.4", 8080, None, None).url("http") == "http://1.2.3.4:8080"


def test_repr_str_and_fstring_never_leak_credentials():
    proxy = Proxy("1.2.3.4", 8080, "bob", "sup3rsecret")
    repr_str = repr(proxy)
    str_str = str(proxy)
    fstring = f"{proxy}"

    assert "1.2.3.4:8080" in repr_str
    assert "1.2.3.4:8080" in str_str
    assert "1.2.3.4:8080" in fstring

    assert "bob" not in repr_str
    assert "sup3rsecret" not in repr_str
    assert "bob" not in str_str
    assert "sup3rsecret" not in str_str
    assert "bob" not in fstring
    assert "sup3rsecret" not in fstring


def test_each_proxy_is_leased_once_then_the_pool_is_empty():
    pool, _, _ = make_pool(count=2)
    assert pool.acquire() is not None
    assert pool.acquire() is not None
    assert pool.acquire() is None


def test_a_used_proxy_comes_back_after_the_cooldown():
    pool, clock, _ = make_pool(count=1, cooldown=15.0)
    proxy = pool.acquire()
    pool.report_success(proxy, latency_ms=120)

    assert pool.acquire() is None
    assert pool.next_ready_in() == pytest.approx(15.0)

    clock.advance(15.0)
    assert pool.acquire() == proxy


def test_rate_limited_proxy_waits_four_cooldowns():
    pool, clock, _ = make_pool(count=1, cooldown=10.0)
    proxy = pool.acquire()
    pool.report_failure(proxy, "rate_limited")

    clock.advance(10.0)
    assert pool.acquire() is None

    clock.advance(30.0)
    assert pool.acquire() == proxy


def test_three_failures_quarantine_and_a_second_quarantine_kills():
    pool, clock, _ = make_pool(count=1, cooldown=1.0, quarantine=60.0, failures_before_quarantine=3)
    for _ in range(3):
        proxy = pool.acquire()
        pool.report_failure(proxy, "error")
        clock.advance(1.0)

    assert pool.acquire() is None
    assert pool.stats()[0].state == ProxyState.QUARANTINED

    clock.advance(60.0)
    for _ in range(3):
        proxy = pool.acquire()
        pool.report_failure(proxy, "error")
        clock.advance(1.0)

    assert pool.stats()[0].state == ProxyState.DEAD
    assert pool.alive_count() == 0


def test_wrong_currency_kills_the_proxy_immediately():
    pool, _, _ = make_pool(count=2)
    proxy = pool.acquire()
    pool.report_wrong_currency(proxy, "UAH")

    assert pool.alive_count() == 1
    stat = next(s for s in pool.stats() if s.key == proxy.key)
    assert stat.state == ProxyState.DEAD
    assert stat.wrong_currency == 1


def test_success_resets_the_failure_streak():
    pool, clock, _ = make_pool(count=1, cooldown=0.0, failures_before_quarantine=3)
    for _ in range(2):
        pool.report_failure(pool.acquire(), "error")
    pool.report_success(pool.acquire(), latency_ms=50)
    for _ in range(2):
        pool.report_failure(pool.acquire(), "error")

    assert pool.stats()[0].state != ProxyState.QUARANTINED


def test_stats_report_median_latency_and_never_leak_credentials():
    clock = FakeClock()
    pool = ProxyPool([Proxy("1.2.3.4", 8080, "bob", "secret")], clock=clock, cooldown=0.0)
    for latency in (100, 300, 200):
        pool.report_success(pool.acquire(), latency_ms=latency)

    stat = pool.stats()[0]
    assert stat.key == "1.2.3.4:8080"
    assert "secret" not in stat.key
    assert stat.median_latency_ms == 200
    assert stat.ok == 3


def test_next_ready_in_is_infinite_when_everything_is_dead():
    pool, _, _ = make_pool(count=1)
    pool.report_wrong_currency(pool.acquire(), "EUR")
    assert pool.next_ready_in() == float("inf")
