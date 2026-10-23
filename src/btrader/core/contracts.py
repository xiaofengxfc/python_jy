"""Contract specifications for spot, linear and inverse (coin-margined) markets.

Binance quotes three different amount units:

* spot / linear futures - ``amount`` is the number of base coins (BTC);
* coin-margined (inverse) futures - ``amount`` is the number of *contracts*,
  each worth ``contractSize`` USD (e.g. BTCUSD perpetual = 100 USD per contract).

The UI, the strategies and the risk manager all think in base coins and USD
notional, so every boundary that talks to the exchange converts through this
class instead of doing its own maths.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """How one unit of ``amount`` maps to base coins and USD notional."""

    contract_size: float = 1.0
    inverse: bool = False
    base: str = ""
    quote: str = ""
    settle: str = ""
    min_amount: float = 0.0
    # 名义额下限（计价币）：币安现货的 NOTIONAL、合约的 MIN_NOTIONAL，ccxt 把它
    # 映射到 ``limits.cost.min``。0 表示交易所没公布——下单预览如实报 null，不拿
    # min_amount × 价格去凑一个数（那是另一个口径，凑出来的值会骗人）。
    min_cost: float = 0.0
    # 数量步长（交易所的 ``precision.amount``，现货与线性合约是币、币本位是张）。
    # 回测按它截断调仓数量、按 min_amount 跳过太小的调仓；0 表示交易所没公布，
    # 这时不截断，也不替它猜一个（猜错会把数量放大或缩小几个数量级）。
    amount_step: float = 0.0

    @property
    def is_inverse(self) -> bool:
        return self.inverse

    def is_tradable(self, amount: float) -> bool:
        """Whether an exchange amount clears the market minimum.

        The step (``precision.amount``) is read once by the REST layer into
        ``amount_step`` and is *not* consulted here: ccxt reports an absolute
        step in current versions and a digit count in older ones, and
        misreading it scales orders by orders of magnitude. Rounding stays
        delegated to ``amount_to_precision``; ``amount_step`` is for callers
        that must truncate offline (the backtest).
        """
        if amount <= 0:
            return False
        if self.min_amount > 0:
            return amount >= self.min_amount - 1e-12
        return True

    def contracts_to_base(self, contracts: float, price: float) -> float:
        """Exchange amount -> base coins.

        For inverse contracts one contract is worth ``contract_size`` USD, so
        ``contracts * size / price`` coins change hands. Linear markets simply
        multiply by the contract size (1.0 for spot).
        """
        if not self.inverse:
            return contracts * self.contract_size
        if price <= 0:
            return 0.0
        return contracts * self.contract_size / price

    def base_to_contracts(self, base_amount: float, price: float) -> float:
        """Base coins -> exchange amount (the inverse of ``contracts_to_base``)."""
        if not self.inverse:
            return base_amount / self.contract_size if self.contract_size else base_amount
        if price <= 0:
            return 0.0
        return base_amount * price / self.contract_size

    def notional(self, contracts: float, price: float) -> float:
        """Absolute USD notional value of a position."""
        if self.inverse:
            return abs(contracts) * self.contract_size
        return abs(contracts) * self.contract_size * price

    def describe(self) -> str:
        if not self.inverse:
            return f"1 张 = {self.contract_size:g} {self.base or '币'}"
        return f"1 张 = {self.contract_size:g} USD（币本位，按 {self.settle or self.base} 结算）"


DEFAULT_SPEC = ContractSpec()
