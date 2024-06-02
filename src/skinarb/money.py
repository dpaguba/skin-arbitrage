"""Parsing of Steam Market price strings into integer cents."""

from __future__ import annotations

import re
from dataclasses import dataclass

CURRENCY_MARKERS: tuple[tuple[str, str], ...] = (
    ("CDN$", "CAD"),
    ("NZ$", "NZD"),
    ("MX$", "MXN"),
    ("A$", "AUD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("HK$", "HKD"),
    ("NT$", "TWD"),
    ("USD", "USD"),
    ("руб", "RUB"),
    ("pуб", "RUB"),
    ("₽", "RUB"),
    ("₴", "UAH"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("zł", "PLN"),
    ("Kč", "CZK"),
    ("₺", "TRY"),
    ("TL", "TRY"),
    ("¥", "JPY"),
    ("₹", "INR"),
    ("₩", "KRW"),
    ("R", "ZAR"),
    ("$", "USD"),
)

SPACES = " \u00a0\u202f\u2009\u2007\t\n\r"


class PriceError(Exception):
    """Base class for every price parsing failure."""


class UnparseablePrice(PriceError):
    """The string carries no number we can read."""


class UnsupportedCurrency(PriceError):
    """The price is real but not in United States dollars."""

    def __init__(self, currency: str) -> None:
        self.currency = currency
        super().__init__(f"unsupported currency: {currency}")


@dataclass(frozen=True)
class Money:
    cents: int
    currency: str = "USD"


def _strip_spaces(raw: str) -> str:
    return raw.translate({ord(ch): None for ch in SPACES})


def detect_currency(raw: str) -> str:
    compact = _strip_spaces(raw)
    for marker, code in CURRENCY_MARKERS:
        needle = _strip_spaces(marker)
        if not needle:
            continue
        if len(needle) == 1 and needle.isalpha():
            matched = compact.startswith(needle)
        else:
            matched = needle in compact
        if matched:
            return code
    raise UnparseablePrice(f"no currency marker in {raw!r}")


def parse_amount_to_cents(raw: str) -> int:
    compact = _strip_spaces(raw)
    digits = re.findall(r"[\d.,]+", compact)
    if not digits:
        raise UnparseablePrice(f"no digits in {raw!r}")

    number = max(digits, key=len)

    if not any(ch.isdigit() for ch in number):
        raise UnparseablePrice(f"no digits in {raw!r}")

    separators = [(i, ch) for i, ch in enumerate(number) if ch in ".,"]

    if not separators:
        whole, frac = number, "00"
    else:
        last_index, _ = separators[-1]
        tail = number[last_index + 1 :]
        if len(tail) == 2 and tail.isdigit():
            whole = number[:last_index]
            frac = tail
        else:
            whole, frac = number, "00"
        whole = whole.replace(".", "").replace(",", "")

    whole = whole or "0"
    if not whole.isdigit():
        raise UnparseablePrice(f"cannot read amount from {raw!r}")

    return int(whole) * 100 + int(frac)


def parse_steam_price(raw: str | None) -> Money:
    if not raw or not raw.strip():
        raise UnparseablePrice("empty price string")

    currency = detect_currency(raw)
    cents = parse_amount_to_cents(raw)

    if currency != "USD":
        raise UnsupportedCurrency(currency)

    return Money(cents=cents, currency="USD")
