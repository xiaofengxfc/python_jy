"""Strategy plugin API.

A strategy is a plain Python class that turns a candle DataFrame into a target
position column. The same ``generate`` call is used by the backtester and by
live trading, so signals stay consistent across modes.

Example::

    class MyStrategy(Strategy):
        key = "my_strategy"
        name = "我的策略"
        params_spec = [IntParam("period", "周期", 20, 2, 200)]

        def generate(self, df, params):
            ma = df["close"].rolling(params["period"]).mean()
            return pd.DataFrame({"target": (df["close"] > ma).astype(float)})

Target semantics (per bar, evaluated at the close, filled at next open):
    1.0  -> 100% long of allocated capital
    0.0  -> flat
   -1.0  -> 100% short (futures only; clamped to 0 for spot)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] = ()
    help: str = ""

    @property
    def kind(self) -> str:
        if self.choices:
            return "choice"
        if isinstance(self.default, bool):
            return "bool"
        if isinstance(self.default, int):
            return "int"
        if isinstance(self.default, float):
            return "float"
        return "text"


def IntParam(name, label, default, minimum=0, maximum=1000, step=1, help="") -> ParamSpec:
    return ParamSpec(name, label, default, minimum, maximum, step, help=help)


def FloatParam(name, label, default, minimum=0.0, maximum=1e9, step=0.01, help="") -> ParamSpec:
    return ParamSpec(name, label, default, minimum, maximum, step, help=help)


def BoolParam(name, label, default, help="") -> ParamSpec:
    return ParamSpec(name, label, default, help=help)


def ChoiceParam(name, label, default, choices, help="") -> ParamSpec:
    return ParamSpec(name, label, default, choices=tuple(choices), help=help)


@dataclass
class StrategyMeta:
    key: str
    name: str
    description: str = ""
    allow_short: bool = True
    min_bars: int = 2


class Strategy(ABC):
    """Base class for all strategies."""

    key: str = ""
    name: str = ""
    description: str = ""
    allow_short: bool = True
    params_spec: list[ParamSpec] = []

    def __init__(self) -> None:
        self._meta = StrategyMeta(
            key=self.key or self.__class__.__name__.lower(),
            name=self.name or self.__class__.__name__,
            description=self.description,
            allow_short=self.allow_short,
            min_bars=self.min_bars(),
        )

    @property
    def meta(self) -> StrategyMeta:
        return self._meta

    def min_bars(self) -> int:
        """Bars required before the first signal can be produced."""
        warmup = 2
        for spec in self.params_spec:
            if isinstance(spec.default, int) and not isinstance(spec.default, bool):
                warmup = max(warmup, int(spec.default) + 1)
        return warmup

    def default_params(self) -> dict[str, Any]:
        return {spec.name: spec.default for spec in self.params_spec}

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Coerce and clamp user supplied params to the declared spec."""
        clean: dict[str, Any] = {}
        for spec in self.params_spec:
            value = params.get(spec.name, spec.default)
            if spec.kind == "bool":
                value = bool(value)
            elif spec.kind == "int":
                value = int(value)
                if spec.minimum is not None:
                    value = max(int(spec.minimum), value)
                if spec.maximum is not None:
                    value = min(int(spec.maximum), value)
            elif spec.kind == "float":
                value = float(value)
                if spec.minimum is not None:
                    value = max(float(spec.minimum), value)
                if spec.maximum is not None:
                    value = min(float(spec.maximum), value)
            elif spec.kind == "choice":
                if value not in spec.choices:
                    value = spec.default
            clean[spec.name] = value
        return clean

    @abstractmethod
    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        """Return a DataFrame indexed like ``df`` with a numeric ``target`` column.

        Additional columns are treated as plot series for the chart.
        """

    def plot_series(self, df: pd.DataFrame, params: dict[str, Any]) -> dict[str, pd.Series]:
        """Extra lines to overlay on the price chart (optional)."""
        return {}

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{self.__class__.__name__} {self.key}>"


def clamp_target(value: float, allow_short: bool) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(value):
        return 0.0
    value = max(-1.0, min(1.0, value))
    if not allow_short and value < 0:
        return 0.0
    return value
