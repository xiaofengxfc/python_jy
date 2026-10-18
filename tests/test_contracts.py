"""Contract-spec maths: spot / linear / inverse (coin-margined) conversion."""

from __future__ import annotations

import pytest

from btrader.core.contracts import DEFAULT_SPEC, ContractSpec


def test_spot_spec_is_identity():
    assert DEFAULT_SPEC.contracts_to_base(1.5, price=100.0) == 1.5
    assert DEFAULT_SPEC.base_to_contracts(1.5, price=100.0) == 1.5
    assert DEFAULT_SPEC.notional(1.5, price=100.0) == pytest.approx(150.0)


def test_linear_contract_multiplier():
    spec = ContractSpec(contract_size=10.0, inverse=False, base="ETH", quote="USDT")
    assert spec.contracts_to_base(3.0, 2000.0) == 30.0
    assert spec.base_to_contracts(30.0, 2000.0) == 3.0
    assert spec.notional(3.0, 2000.0) == pytest.approx(60_000.0)


def test_inverse_btcusd_matches_binance():
    """BTCUSD perpetual: 1 contract = 100 USD, settled in BTC."""
    spec = ContractSpec(contract_size=100.0, inverse=True, base="BTC", quote="USD", settle="BTC")
    # 10 contracts at 50,000 USD = 1,000 USD notional = 0.02 BTC
    assert spec.contracts_to_base(10.0, 50_000.0) == pytest.approx(0.02)
    assert spec.base_to_contracts(0.02, 50_000.0) == pytest.approx(10.0)
    assert spec.notional(10.0, 50_000.0) == pytest.approx(1_000.0)


def test_inverse_round_trip_is_stable():
    spec = ContractSpec(contract_size=10.0, inverse=True, base="ETH", quote="USD")
    for price in (1_000.0, 2_500.5, 3_999.99):
        contracts = spec.base_to_contracts(0.5, price)
        assert spec.contracts_to_base(contracts, price) == pytest.approx(0.5)


def test_inverse_requires_price():
    spec = ContractSpec(contract_size=100.0, inverse=True, base="BTC", quote="USD")
    assert spec.contracts_to_base(5.0, 0.0) == 0.0
    assert spec.base_to_contracts(1.0, 0.0) == 0.0


def test_describe_is_human_readable():
    inverse = ContractSpec(contract_size=100.0, inverse=True, base="BTC", quote="USD", settle="BTC")
    assert "100" in inverse.describe()
    assert "USD" in inverse.describe()
    linear = ContractSpec(contract_size=10.0, base="ETH")
    assert "ETH" in linear.describe()
    assert "10" in linear.describe()
