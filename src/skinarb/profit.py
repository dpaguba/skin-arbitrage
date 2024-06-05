"""Fee arithmetic for the two trade directions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

DMARKET_FEE = 0.02
STEAM_PRICE_DIVISOR = 1.15


@dataclass(frozen=True)
class Profit:
    withdrawable_cents: int
    withdrawable_pct: float
    wallet_cents: int
    wallet_pct: float
    dmarket_fee_pct: float = DMARKET_FEE * 100


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def steam_seller_receives(steam_cents: int, divisor: float = STEAM_PRICE_DIVISOR) -> int:
    return _round_cents(Decimal(steam_cents) / Decimal(str(divisor)))


def _percent(profit_cents: int, invested_cents: int) -> float:
    if invested_cents <= 0:
        return 0.0
    return round(profit_cents / invested_cents * 100, 2)


def _resolve_dmarket_fee(dmarket_fee_pct: float | None, fallback_fee: float) -> tuple[float, float]:
    """Pick the fee fraction to charge and the percent it corresponds to.

    The reduced-fee list is assumed to report a percentage: 5.0 means five
    percent. That assumption is unverified against a live response (see
    docs/design.md section 6.2), so anything outside (0, 100] is treated as
    untrustworthy and the flat constant is used instead.
    """
    if dmarket_fee_pct is not None and 0 < dmarket_fee_pct <= 100:
        return dmarket_fee_pct / 100.0, dmarket_fee_pct
    return fallback_fee, fallback_fee * 100.0


def compute_profit(
    dmarket_cents: int,
    steam_cents: int,
    *,
    dmarket_fee: float = DMARKET_FEE,
    dmarket_fee_pct: float | None = None,
    steam_divisor: float = STEAM_PRICE_DIVISOR,
) -> Profit:
    fee, fee_pct = _resolve_dmarket_fee(dmarket_fee_pct, dmarket_fee)

    dmarket_payout = _round_cents(Decimal(dmarket_cents) * (1 - Decimal(str(fee))))
    withdrawable = dmarket_payout - steam_cents

    steam_payout = steam_seller_receives(steam_cents, steam_divisor)
    wallet = steam_payout - dmarket_cents

    return Profit(
        withdrawable_cents=withdrawable,
        withdrawable_pct=_percent(withdrawable, steam_cents),
        wallet_cents=wallet,
        wallet_pct=_percent(wallet, dmarket_cents),
        dmarket_fee_pct=fee_pct,
    )
