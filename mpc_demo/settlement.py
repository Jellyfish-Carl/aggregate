from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .domain import ContractFill, CostBreakdown


def contract_difference_cost(fills: Iterable[ContractFill], day_ahead_prices: Mapping[str, float]) -> float:
    """Calculate contract-for-difference cash flow fill by fill.

    Signed delivery quantities preserve the cash flow of a buy followed by a
    sell even when the net contract position is zero.
    """

    total = 0.0
    for fill in fills:
        if fill.status != "POSTED":
            continue
        for interval, signed_quantity in fill.delivery_curve.items():
            total += signed_quantity * (fill.price - day_ahead_prices[interval])
    return total


def day_ahead_recovery(
    actual_mwh: float,
    declared_mwh: float,
    day_ahead_price: float,
    real_time_price: float,
    band: float = 0.10,
    multiplier: float = 1.05,
) -> float:
    deviation = actual_mwh - declared_mwh
    excess = max(abs(deviation) - band * actual_mwh, 0.0)
    if deviation > 0:
        favorable_spread = max(day_ahead_price - real_time_price, 0.0)
    else:
        favorable_spread = max(real_time_price - day_ahead_price, 0.0)
    return multiplier * favorable_spread * excess


def annual_ratio_recovery(
    actual_mwh: float,
    annual_contract_mwh: float,
    annual_reference_price: float,
    monthly_reference_price: float,
    threshold: float = 0.60,
    multiplier: float = 1.05,
) -> float:
    return (
        multiplier
        * max(annual_reference_price - monthly_reference_price, 0.0)
        * max(threshold * actual_mwh - annual_contract_mwh, 0.0)
    )


def over_profit_recovery(
    actual_mwh: float,
    assessed_contract_mwh: float,
    monthly_reference_price: float,
    spot_reference_price: float,
    lower_ratio: float = 0.90,
    upper_ratio: float = 1.10,
    multiplier: float = 1.05,
) -> float:
    low = (
        multiplier
        * max(monthly_reference_price - spot_reference_price, 0.0)
        * max(lower_ratio * actual_mwh - assessed_contract_mwh, 0.0)
    )
    high = (
        multiplier
        * max(spot_reference_price - monthly_reference_price, 0.0)
        * max(assessed_contract_mwh - upper_ratio * actual_mwh, 0.0)
    )
    return low + high


def adjustment_energy_cost(
    meter_mwh: float,
    daily_mwh: float,
    official_energy: Sequence[float],
    official_rt_prices: Sequence[float],
) -> float:
    if len(official_energy) != len(official_rt_prices):
        raise ValueError("电量和实时价格数组长度不一致")
    denominator = sum(official_energy)
    if denominator == 0:
        raise ZeroDivisionError("日清累计电量为 0，调整电量待结算")
    weighted_price = sum(q * p for q, p in zip(official_energy, official_rt_prices)) / denominator
    return (meter_mwh - daily_mwh) * weighted_price


def robust_declaration_band(
    forecast_lower: float,
    forecast_upper: float,
    physical_upper: float,
) -> tuple:
    if forecast_lower < 0 or forecast_lower > forecast_upper:
        raise ValueError("INPUT_INVALID: 日前预测区间无效")
    if physical_upper < 0:
        raise ValueError("INPUT_INVALID: 物理申报上限不能为负")
    lower = forecast_lower
    upper = min(physical_upper, forecast_upper)
    return lower, upper


def minimax_declaration_slack(
    forecast_lower: float,
    forecast_upper: float,
    physical_upper: float,
) -> float:
    lower, upper = robust_declaration_band(forecast_lower, forecast_upper, physical_upper)
    return max(0.0, (lower - upper) / 2.0)


@dataclass(frozen=True)
class SettlementInputs:
    energy_cost: float
    trade_fee: float = 0.0
    day_ahead_recovery: float = 0.0
    annual_recovery: float = 0.0
    over_profit_recovery: float = 0.0
    curve_adjustment: float = 0.0
    adjustment_energy: float = 0.0
    external_items: float = 0.0
    flex_impact: float = 0.0
    inventory_value: float = 0.0


def settle_month(inputs: SettlementInputs) -> CostBreakdown:
    return CostBreakdown(
        energy=inputs.energy_cost,
        trade_fee=inputs.trade_fee,
        day_ahead_recovery=inputs.day_ahead_recovery,
        annual_recovery=inputs.annual_recovery,
        over_profit_recovery=inputs.over_profit_recovery,
        curve_adjustment=inputs.curve_adjustment,
        adjustment_energy=inputs.adjustment_energy,
        external_items=inputs.external_items,
        flex_impact=inputs.flex_impact,
        inventory_value=inputs.inventory_value,
    )
