import pytest

from skinarb.profit import (
    DMARKET_FEE,
    STEAM_PRICE_DIVISOR,
    Profit,
    compute_profit,
    steam_seller_receives,
)


def test_seller_receives_less_than_the_listed_price():
    assert steam_seller_receives(115) == 100


def test_seller_share_rounds_half_up():
    assert steam_seller_receives(100) == 87


def test_withdrawable_direction_is_steam_to_dmarket():
    result = compute_profit(dmarket_cents=800, steam_cents=500)
    assert result.withdrawable_cents == 284
    assert result.withdrawable_pct == pytest.approx(56.8, abs=0.1)


def test_wallet_direction_is_dmarket_to_steam():
    result = compute_profit(dmarket_cents=500, steam_cents=800)
    assert result.wallet_cents == 196
    assert result.wallet_pct == pytest.approx(39.2, abs=0.1)


def test_wallet_can_be_positive_while_withdrawable_is_negative():
    result = compute_profit(dmarket_cents=500, steam_cents=800)
    assert result.wallet_cents > 0
    assert result.withdrawable_cents < 0


def test_zero_prices_do_not_divide_by_zero():
    result = compute_profit(dmarket_cents=0, steam_cents=0)
    assert result == Profit(0, 0.0, 0, 0.0)


def test_fees_match_the_spec():
    assert DMARKET_FEE == 0.02
    assert STEAM_PRICE_DIVISOR == 1.15


def test_a_sane_reduced_fee_overrides_the_flat_constant():
    result = compute_profit(dmarket_cents=800, steam_cents=500, dmarket_fee_pct=1.0)

    assert result.dmarket_fee_pct == 1.0
    assert result.withdrawable_cents == 792 - 500


@pytest.mark.parametrize("insane_pct", [0.0, -5.0, 100.5, 250.0])
def test_a_fee_outside_the_sane_range_falls_back_to_the_constant(insane_pct):
    default = compute_profit(dmarket_cents=800, steam_cents=500)
    overridden = compute_profit(dmarket_cents=800, steam_cents=500, dmarket_fee_pct=insane_pct)

    assert overridden == default
    assert overridden.dmarket_fee_pct == DMARKET_FEE * 100


def test_a_missing_fee_falls_back_to_the_constant():
    result = compute_profit(dmarket_cents=800, steam_cents=500, dmarket_fee_pct=None)

    assert result == compute_profit(dmarket_cents=800, steam_cents=500)
    assert result.dmarket_fee_pct == DMARKET_FEE * 100


def test_a_fee_of_exactly_100_percent_is_still_sane():
    result = compute_profit(dmarket_cents=800, steam_cents=500, dmarket_fee_pct=100.0)

    assert result.dmarket_fee_pct == 100.0
    assert result.withdrawable_cents == 0 - 500
