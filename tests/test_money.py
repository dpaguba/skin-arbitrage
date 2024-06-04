import pytest

from skinarb.money import (
    Money,
    UnparseablePrice,
    UnsupportedCurrency,
    parse_steam_price,
)


@pytest.mark.parametrize(
    "raw,cents",
    [
        ("$0.09", 9),
        ("$7.09", 709),
        ("$1,093.84", 109384),
        ("$12,500.00", 1250000),
        ("$1,000", 100000),
        ("$1", 100),
        ("USD 4.20", 420),
        ("$ 3.50", 350),
    ],
)
def test_usd_prices(raw, cents):
    assert parse_steam_price(raw) == Money(cents=cents, currency="USD")


@pytest.mark.parametrize(
    "raw,currency",
    [
        ("0,09€", "EUR"),
        ("93,84 pуб.", "RUB"),
        ("93,84 руб.", "RUB"),
        ("1 093,84₴", "UAH"),
        ("CDN$ 5.00", "CAD"),
        ("A$ 5.00", "AUD"),
        ("R$ 5,00", "BRL"),
        ("£4.99", "GBP"),
        ("5,00 zł", "PLN"),
    ],
)
def test_foreign_currency_is_rejected(raw, currency):
    with pytest.raises(UnsupportedCurrency) as excinfo:
        parse_steam_price(raw)
    assert excinfo.value.currency == currency


@pytest.mark.parametrize("raw", ["", "   ", None, "—", "$", "no digits here", "$.", "$,", "€,,,"])
def test_unparseable(raw):
    with pytest.raises(UnparseablePrice):
        parse_steam_price(raw)


def test_narrow_and_non_breaking_spaces_are_stripped():
    assert parse_steam_price("$\u00a01\u202f234.50") == Money(cents=123450, currency="USD")


def test_money_is_hashable_and_frozen():
    assert len({Money(100, "USD"), Money(100, "USD")}) == 1
