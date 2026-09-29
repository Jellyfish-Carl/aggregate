"""Retail inputs layered on the unchanged pifa load/price/contract inputs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..data_objects.model import Customer


@dataclass(frozen=True)
class FullModelInput:
    customers: Sequence[Customer] = field(default_factory=lambda: (
        Customer("C1", [455.0] * 96, [18.0] * 96, 430.0, .5, .45),
        Customer("C2", [462.0] * 96, [22.0] * 96, 435.0, .45, .5),
    ))
    # Client-specific interval load shapes, normalized to the original pifa
    # aggregate. C1 is daytime-heavy; C2 takes the remaining load.
    customer_shares: Sequence[Sequence[float]] = field(default_factory=lambda: (
        tuple(.65 if 8 <= j / 4 < 18 else .30 for j in range(96)),
        tuple(.35 if 8 <= j / 4 < 18 else .70 for j in range(96)),
    ))
    risk_lambdas: Sequence[float] = (0.0, .25, .5, .75, 1.0)
    annual_price: float = 405.0
    monthly_price: float = 416.0
    ten_day_price: float = 424.0
    reference_weights: Sequence[float] = (.7, .2, .1)
    event: str = "D-1"
    rt_period: int = 1
    customer_packages: Mapping[str, str] = field(default_factory=dict)
    annual_reference_price: float | None = None
    monthly_reference_price: float | None = None

    def validate(self):
        if not self.customers or len(self.customers) != len(self.customer_shares):
            raise ValueError("客户与分时负荷比例长度不匹配")
        for customer in self.customers:
            customer.validate()
        if len(set(c.customer_id for c in self.customers)) != len(self.customers):
            raise ValueError("客户ID不能重复")
        if set(self.customer_packages)-{c.customer_id for c in self.customers} or any(p not in ('F','L','S') for p in self.customer_packages.values()):
            raise ValueError('customer_packages须使用已知客户ID和F/L/S套餐')
        if any(len(row) != 96 for row in self.customer_shares):
            raise ValueError("客户负荷比例必须各有96点")
        import math
        if any(not math.isfinite(x) or x < 0 for row in self.customer_shares for x in row):
            raise ValueError("客户负荷比例必须非负有限")
        if any(abs(sum(row[j] for row in self.customer_shares) - 1) > 1e-9 for j in range(96)):
            raise ValueError("每个时段客户比例之和必须为1")
        if not self.risk_lambdas or any(not math.isfinite(x) or not 0 <= x <= 1 for x in self.risk_lambdas):
            raise ValueError("λ必须在[0,1]")
        if len(self.reference_weights) != 3 or any(not math.isfinite(x) or x < 0 for x in self.reference_weights) or abs(sum(self.reference_weights)-1)>1e-9:
            raise ValueError("参考价权重必须非负且和为1")
        if any(not math.isfinite(x) for x in (self.annual_price, self.monthly_price, self.ten_day_price)):
            raise ValueError("合同价格必须为有限数值")
        if any(v is not None and not math.isfinite(v) for v in (self.annual_reference_price,self.monthly_reference_price)):
            raise ValueError('市场参考价必须有限')


def retail_prices(config, package, rt_prices):
    a, m, s = config.reference_weights
    annual=config.annual_price if config.annual_reference_price is None else config.annual_reference_price
    monthly=config.monthly_price if config.monthly_reference_price is None else config.monthly_reference_price
    reference = [a * annual + m * monthly + s * rt for rt in rt_prices]
    prices = {}
    for customer in config.customers:
        selected=config.customer_packages.get(customer.customer_id,package)
        curve = []
        for j, ref in enumerate(reference):
            if selected == "F":
                price = customer.fixed_price[j]
            elif selected == "L":
                price = ref + customer.service_fee[j]
            elif selected == "S":
                delta = ref - customer.share_base_price
                price = customer.share_base_price + customer.share_up_ratio * max(delta, 0) - customer.share_down_ratio * max(-delta, 0)
            else:
                raise ValueError("套餐必须为F/L/S")
            floor = customer.price_floor.get(selected, [-1e6] * 96)[j]
            cap = customer.price_cap.get(selected, [1e6] * 96)[j]
            curve.append(min(cap, max(floor, price)))
        prices[customer.customer_id] = curve
    return prices


def revenue_builder(config, package):
    def build(scenarios):
        revenues = []
        for scenario in scenarios:
            prices = retail_prices(config, package, scenario.real_time_price)
            revenues.append(sum(
                scenario.load_mwh[j] * config.customer_shares[i][j] * prices[c.customer_id][j]
                for i, c in enumerate(config.customers) for j in range(96)
            ))
        return revenues
    return build
