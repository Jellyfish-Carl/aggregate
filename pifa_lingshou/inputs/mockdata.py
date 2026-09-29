from __future__ import annotations

import math
from typing import List

from ..data_objects.model import AggregateInput, ContractProduct, Customer, JointScenario, StorageConfig


def _load(customer_index: int, scenario_index: int) -> List[float]:
    values = []
    for period in range(96):
        hour = period / 4.0
        base = 0.8 + 0.14 * math.sin(2.0 * math.pi * (hour - 6.0) / 24.0)
        peak = 0.25 if 9.0 <= hour < 12.0 or 14.0 <= hour < 18.0 else 0.0
        scenario_factor = (scenario_index - 1) * 0.06
        values.append(max(0.05, (base + peak + scenario_factor) * (1.0 + customer_index * 0.12) * 0.25))
    return values


def build_demo_input(storage_case: str = "baseline") -> AggregateInput:
    """Keep the original economics by default; arbitrage is an explicit sensitivity case."""
    if storage_case not in {"baseline", "arbitrage"}:
        raise ValueError("storage_case 只能是 baseline 或 arbitrage")
    probabilities = (0.25, 0.5, 0.25)
    baseline_prices = (430.0, 435.0)
    baseline_bills = [
        sum(
            probability * sum(_load(customer_index, scenario_index)) * baseline_prices[customer_index]
            for scenario_index, probability in enumerate(probabilities)
        )
        for customer_index in range(2)
    ]
    customers = [
        Customer(
            customer_id="C1",
            fixed_price=[455.0] * 96,
            service_fee=[18.0] * 96,
            share_base_price=430.0,
            share_up_ratio=0.5,
            share_down_ratio=0.45,
            base_bill_yuan=baseline_bills[0],
        ),
        Customer(
            customer_id="C2",
            fixed_price=[462.0] * 96,
            service_fee=[22.0] * 96,
            share_base_price=435.0,
            share_up_ratio=0.45,
            share_down_ratio=0.5,
            base_bill_yuan=baseline_bills[1],
        ),
    ]
    day_ahead = [390.0 + 75.0 * (1.0 if 9 <= period / 4 < 12 or 14 <= period / 4 < 18 else 0.0) for period in range(96)]
    rt_amplitude = 18.0 if storage_case == "baseline" else 55.0
    deviation_penalty = 1000.0 if storage_case == "baseline" else 10.0
    real_time = [value + rt_amplitude * math.sin(period / 8.0) for period, value in enumerate(day_ahead)]
    scenarios = []
    for scenario_index, scenario_id in enumerate(("LOW", "BASE", "HIGH")):
        factor = (scenario_index - 1) * 0.08
        scenarios.append(
            JointScenario(
                scenario_id=scenario_id,
                probability=(0.25, 0.5, 0.25)[scenario_index],
                customer_load_mwh={customer.customer_id: _load(index, scenario_index) for index, customer in enumerate(customers)},
                day_ahead_price=[value * (1.0 + factor * 0.25) for value in day_ahead],
                real_time_price=[value * (1.0 + factor) for value in real_time],
                spot_reference_price=[value * (1.0 + factor) for value in real_time],
            )
        )
    baseline_loads = [_load(index, 1) for index in range(len(customers))]
    monthly = [
        sum(load[2 * half_hour] + load[2 * half_hour + 1] for load in baseline_loads)
        for half_hour in range(48)
    ]
    products = [
        ContractProduct("ANNUAL", [390.0] * 48, [385.0] * 48, [2.0] * 48, [0.2] * 48),
        ContractProduct("MONTHLY", [405.0] * 48, [400.0] * 48, [1.5] * 48, [0.2] * 48),
        ContractProduct("D3", [425.0] * 48, [420.0] * 48, [1.5] * 48, [0.2] * 48),
    ]
    return AggregateInput(
        customers=customers,
        scenarios=scenarios,
        annual_reference_price=[405.0] * 96,
        monthly_reference_price=[418.0] * 96,
        contract_products=products,
        monthly_delivery_mwh=monthly,
        physical_peak_mw=12.0,
        storage=StorageConfig(
            minimum_soc_mwh=0.0,
            maximum_soc_mwh=1.0,
            initial_soc_mwh=0.5,
            maximum_charge_mwh=0.25,
            maximum_discharge_mwh=0.25,
            efficiency=0.92,
            degradation_yuan_per_mwh=2.0,
        ),
        risk_lambdas=(0.0, 0.5, 1.0),
        deviation_buy_penalty=deviation_penalty,
        deviation_sell_penalty=deviation_penalty,
        time_limit_seconds=10.0,
    )
