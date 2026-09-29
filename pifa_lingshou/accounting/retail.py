from __future__ import annotations

from typing import Mapping, Sequence

from pifa_lingshou.data_objects.scenario import (
    cvar as _pifa_cvar,
    weighted_mean as _pifa_weighted_mean,
    weighted_quantile as _pifa_weighted_quantile,
)

from ..data_objects.model import AggregateInput, PACKAGES, RetailSettlement


def _clamp(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        raise ValueError("套餐价格下限不能高于上限")
    return min(upper, max(lower, value))


def settlement_prices(inputs: AggregateInput, package: str, scenario_index: int) -> Mapping[str, tuple]:
    if package not in PACKAGES:
        raise ValueError("package 必须是 F、L 或 S")
    scenario = inputs.scenarios[scenario_index]
    weights = (
        inputs.annual_reference_weight,
        inputs.monthly_reference_weight,
        inputs.spot_reference_weight,
    )
    reference = tuple(
        weights[0] * float(annual)
        + weights[1] * float(monthly)
        + weights[2] * float(spot)
        for annual, monthly, spot in zip(
            inputs.annual_reference_price,
            inputs.monthly_reference_price,
            scenario.spot_reference_price,
        )
    )
    prices = {}
    for customer in inputs.customers:
        raw_prices = []
        for index, ref_price in enumerate(reference):
            if package == "F":
                raw = float(customer.fixed_price[index])
            elif package == "L":
                raw = ref_price + float(customer.service_fee[index])
            else:
                delta = ref_price - customer.share_base_price
                raw = (
                    customer.share_base_price
                    + customer.share_up_ratio * max(delta, 0.0)
                    - customer.share_down_ratio * max(-delta, 0.0)
                )
            lower_curve = customer.price_floor.get(package)
            upper_curve = customer.price_cap.get(package)
            lower = float(lower_curve[index]) if lower_curve is not None else -1e6
            upper = float(upper_curve[index]) if upper_curve is not None else 1e6
            raw_prices.append(_clamp(raw, lower, upper))
        prices[customer.customer_id] = tuple(raw_prices)
    return prices


def settle_scenario(inputs: AggregateInput, package: str, scenario_index: int) -> RetailSettlement:
    scenario = inputs.scenarios[scenario_index]
    prices = settlement_prices(inputs, package, scenario_index)
    bills = {
        customer.customer_id: sum(
            float(load) * price
            for load, price in zip(scenario.customer_load_mwh[customer.customer_id], prices[customer.customer_id])
        )
        for customer in inputs.customers
    }
    return RetailSettlement(
        customer_prices=prices,
        customer_bills_yuan=bills,
        retail_revenue_yuan=sum(bills.values()),
    )


def settle_locked_mix(inputs: AggregateInput, package: str, scenario_index: int) -> RetailSettlement:
    """Settle a candidate with previously signed customers kept on their package."""

    scenario = inputs.scenarios[scenario_index]
    prices = {}
    bills = {}
    for customer in inputs.customers:
        selected = inputs.locked_state.locked_packages.get(customer.customer_id, package)
        customer_curve = settlement_prices(inputs, selected, scenario_index)[customer.customer_id]
        prices[customer.customer_id] = customer_curve
        bills[customer.customer_id] = sum(
            float(load) * price
            for load, price in zip(scenario.customer_load_mwh[customer.customer_id], customer_curve)
        )
    return RetailSettlement(prices, bills, sum(bills.values()))


def weighted_mean(values: Sequence[float], probabilities: Sequence[float]) -> float:
    return _pifa_weighted_mean(values, probabilities)


def weighted_quantile(values: Sequence[float], probabilities: Sequence[float], quantile: float) -> float:
    if len(values) != len(probabilities) or not values:
        raise ValueError("分位数输入长度不一致或为空")
    return _pifa_weighted_quantile(values, probabilities, quantile)


def weighted_cvar(values: Sequence[float], probabilities: Sequence[float], alpha: float) -> float:
    return _pifa_cvar(values, probabilities, alpha)


def summarize_distribution(values: Sequence[float], probabilities: Sequence[float], alpha: float = 0.95) -> dict:
    return {
        "mean": weighted_mean(values, probabilities),
        "p10": weighted_quantile(values, probabilities, 0.10),
        "p90": weighted_quantile(values, probabilities, 0.90),
        "cvar": weighted_cvar(values, probabilities, alpha),
    }
