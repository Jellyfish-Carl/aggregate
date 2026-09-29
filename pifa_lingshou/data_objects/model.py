from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple


PERIODS = 96
CONTRACT_PERIODS = 48
PACKAGES = ("F", "L", "S")


def _curve(values: Sequence[float], size: int, name: str) -> Tuple[float, ...]:
    curve = tuple(float(value) for value in values)
    if len(curve) != size:
        raise ValueError("%s 必须包含 %d 个点" % (name, size))
    if any(not math.isfinite(value) for value in curve):
        raise ValueError("%s 必须全部为有限数值" % name)
    return curve


@dataclass(frozen=True)
class Customer:
    customer_id: str
    fixed_price: Sequence[float]
    service_fee: Sequence[float]
    share_base_price: float
    share_up_ratio: float = 0.5
    share_down_ratio: float = 0.5
    base_bill_yuan: float = 0.0
    max_expected_bill_yuan: Optional[float] = None
    price_floor: Mapping[str, Sequence[float]] = field(default_factory=dict)
    price_cap: Mapping[str, Sequence[float]] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.customer_id:
            raise ValueError("customer_id 不能为空")
        if not math.isfinite(float(self.share_base_price)) or not math.isfinite(float(self.base_bill_yuan)):
            raise ValueError("客户基准价和基准账单必须为有限数值")
        _curve(self.fixed_price, PERIODS, self.customer_id + ".fixed_price")
        _curve(self.service_fee, PERIODS, self.customer_id + ".service_fee")
        if not 0.0 <= self.share_up_ratio <= 1.0:
            raise ValueError("share_up_ratio 必须位于 [0, 1]")
        if not 0.0 <= self.share_down_ratio <= 1.0:
            raise ValueError("share_down_ratio 必须位于 [0, 1]")
        for package, values in self.price_floor.items():
            if package not in PACKAGES:
                raise ValueError("未知套餐价格下限: " + package)
            _curve(values, PERIODS, self.customer_id + ".price_floor." + package)
        for package, values in self.price_cap.items():
            if package not in PACKAGES:
                raise ValueError("未知套餐价格上限: " + package)
            _curve(values, PERIODS, self.customer_id + ".price_cap." + package)
        for package in set(self.price_floor) & set(self.price_cap):
            if any(lo > hi for lo, hi in zip(self.price_floor[package], self.price_cap[package])):
                raise ValueError("套餐价格下限不能高于上限")
        if self.max_expected_bill_yuan is not None and (
            not math.isfinite(self.max_expected_bill_yuan) or self.max_expected_bill_yuan < 0
        ):
            raise ValueError("max_expected_bill_yuan 必须为非负有限数值")


@dataclass(frozen=True)
class JointScenario:
    scenario_id: str
    probability: float
    customer_load_mwh: Mapping[str, Sequence[float]]
    day_ahead_price: Sequence[float]
    real_time_price: Sequence[float]
    spot_reference_price: Sequence[float]


@dataclass(frozen=True)
class ContractProduct:
    product_id: str
    buy_price: Sequence[float]
    sell_price: Sequence[float]
    buy_limit_mwh: Sequence[float]
    sell_limit_mwh: Sequence[float]


@dataclass(frozen=True)
class StorageConfig:
    minimum_soc_mwh: float = 0.0
    maximum_soc_mwh: float = 0.0
    initial_soc_mwh: float = 0.0
    maximum_charge_mwh: float = 0.0
    maximum_discharge_mwh: float = 0.0
    efficiency: float = 1.0
    degradation_yuan_per_mwh: float = 0.0

    def validate(self) -> None:
        scalars = (
            self.minimum_soc_mwh,
            self.maximum_soc_mwh,
            self.initial_soc_mwh,
            self.maximum_charge_mwh,
            self.maximum_discharge_mwh,
            self.efficiency,
            self.degradation_yuan_per_mwh,
        )
        if any(not math.isfinite(float(value)) for value in scalars):
            raise ValueError("储能参数必须全部为有限数值")
        if self.minimum_soc_mwh < 0 or self.maximum_soc_mwh < self.minimum_soc_mwh:
            raise ValueError("储能 SOC 边界无效")
        if not self.minimum_soc_mwh <= self.initial_soc_mwh <= self.maximum_soc_mwh:
            raise ValueError("初始 SOC 必须位于储能边界内")
        if self.maximum_charge_mwh < 0 or self.maximum_discharge_mwh < 0:
            raise ValueError("储能充放电上限不能为负")
        if not 0.0 < self.efficiency <= 1.0:
            raise ValueError("储能效率必须位于 (0, 1]")
        if self.degradation_yuan_per_mwh < 0:
            raise ValueError("储能退化成本不能为负")


@dataclass(frozen=True)
class LockedState:
    locked_packages: Mapping[str, str] = field(default_factory=dict)
    fixed_until: int = 0
    actual_aggregate_load_mwh: Sequence[float] = field(default_factory=tuple)
    declaration_mwh: Mapping[int, float] = field(default_factory=dict)
    real_time_buy_mwh: Mapping[int, float] = field(default_factory=dict)
    real_time_sell_mwh: Mapping[int, float] = field(default_factory=dict)
    charge_mwh: Mapping[int, float] = field(default_factory=dict)
    discharge_mwh: Mapping[int, float] = field(default_factory=dict)
    soc_mwh: Mapping[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregateInput:
    customers: Sequence[Customer]
    scenarios: Sequence[JointScenario]
    annual_reference_price: Sequence[float]
    monthly_reference_price: Sequence[float]
    contract_products: Sequence[ContractProduct]
    monthly_delivery_mwh: Sequence[float]
    old_contract_position_mwh: Sequence[float] = field(default_factory=lambda: (0.0,) * CONTRACT_PERIODS)
    old_annual_position_mwh: Sequence[float] = field(default_factory=lambda: (0.0,) * CONTRACT_PERIODS)
    old_contract_cost_yuan: float = 0.0
    annual_product_id: str = "ANNUAL"
    annual_reference_weight: float = 0.7
    monthly_reference_weight: float = 0.2
    spot_reference_weight: float = 0.1
    annual_coverage_minimum: float = 0.6
    overall_coverage_minimum: float = 0.9
    overall_coverage_maximum: float = 1.1
    physical_peak_mw: float = 20.0
    declaration_p10_mwh: Optional[Sequence[float]] = None
    declaration_p90_mwh: Optional[Sequence[float]] = None
    storage: StorageConfig = field(default_factory=StorageConfig)
    risk_lambdas: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0)
    cvar_alpha: float = 0.95
    deviation_ratio_limit: float = 0.1
    deviation_buy_penalty: float = 1000.0
    deviation_sell_penalty: float = 1000.0
    deviation_buy_slack_limit_mwh: Optional[Sequence[float]] = None
    deviation_sell_slack_limit_mwh: Optional[Sequence[float]] = None
    transaction_friction_yuan_per_mwh: float = 1.0
    declaration_slack_penalty_yuan_per_mwh: float = 10000.0
    customer_bill_limits_yuan: Mapping[str, float] = field(default_factory=dict)
    minimum_expected_profit_yuan: Optional[float] = None
    maximum_profit_loss_cvar_yuan: Optional[float] = None
    locked_state: LockedState = field(default_factory=LockedState)
    time_limit_seconds: float = 15.0
    mip_relative_gap: float = 1e-3

    def validate(self) -> None:
        if not self.customers or not self.scenarios or not self.contract_products:
            raise ValueError("客户、联合场景和合同产品集合不能为空")
        scalar_values = (
            self.annual_reference_weight,
            self.monthly_reference_weight,
            self.spot_reference_weight,
            self.annual_coverage_minimum,
            self.overall_coverage_minimum,
            self.overall_coverage_maximum,
            self.physical_peak_mw,
            self.cvar_alpha,
            self.deviation_ratio_limit,
            self.deviation_buy_penalty,
            self.deviation_sell_penalty,
            self.transaction_friction_yuan_per_mwh,
            self.declaration_slack_penalty_yuan_per_mwh,
            self.time_limit_seconds,
            self.mip_relative_gap,
        )
        if any(not math.isfinite(float(value)) for value in scalar_values):
            raise ValueError("集合体模型标量参数必须全部为有限数值")
        customer_ids = [customer.customer_id for customer in self.customers]
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        product_ids = [product.product_id for product in self.contract_products]
        if len(set(customer_ids)) != len(customer_ids):
            raise ValueError("客户 ID 不能重复")
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("场景 ID 不能重复")
        if len(set(product_ids)) != len(product_ids):
            raise ValueError("合同产品 ID 不能重复")
        if self.annual_product_id not in product_ids:
            raise ValueError("annual_product_id 必须对应一个合同产品")
        for customer in self.customers:
            customer.validate()
        _curve(self.annual_reference_price, PERIODS, "annual_reference_price")
        _curve(self.monthly_reference_price, PERIODS, "monthly_reference_price")
        if any(value < 0 for value in _curve(self.monthly_delivery_mwh, CONTRACT_PERIODS, "monthly_delivery_mwh")):
            raise ValueError("monthly_delivery_mwh 不能为负")
        _curve(self.old_contract_position_mwh, CONTRACT_PERIODS, "old_contract_position_mwh")
        _curve(self.old_annual_position_mwh, CONTRACT_PERIODS, "old_annual_position_mwh")
        weights = (self.annual_reference_weight, self.monthly_reference_weight, self.spot_reference_weight)
        if any(weight < 0 for weight in weights) or abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("年度、月度、现货参考价权重必须非负且和为 1")
        if self.annual_coverage_minimum < 0 or self.overall_coverage_minimum < 0:
            raise ValueError("合同覆盖比例不能为负")
        if self.overall_coverage_maximum < self.overall_coverage_minimum:
            raise ValueError("总体合同覆盖上限不能小于下限")
        if self.physical_peak_mw <= 0:
            raise ValueError("physical_peak_mw 必须大于 0")
        if not 0.0 <= self.cvar_alpha < 1.0:
            raise ValueError("cvar_alpha 必须位于 [0, 1)")
        if not 0.0 <= self.deviation_ratio_limit:
            raise ValueError("deviation_ratio_limit 不能为负")
        if min(
            self.deviation_buy_penalty,
            self.deviation_sell_penalty,
            self.transaction_friction_yuan_per_mwh,
            self.declaration_slack_penalty_yuan_per_mwh,
        ) < 0:
            raise ValueError("交易摩擦、偏差成本和申报松弛罚金不能为负")
        if not self.risk_lambdas or any(not math.isfinite(float(value)) or not 0.0 <= value <= 1.0 for value in self.risk_lambdas):
            raise ValueError("risk_lambdas 必须是 [0, 1] 内的非空序列")
        if self.minimum_expected_profit_yuan is not None and not math.isfinite(float(self.minimum_expected_profit_yuan)):
            raise ValueError("minimum_expected_profit_yuan 必须为有限数值")
        if self.maximum_profit_loss_cvar_yuan is not None and not math.isfinite(float(self.maximum_profit_loss_cvar_yuan)):
            raise ValueError("maximum_profit_loss_cvar_yuan 必须为有限数值")
        if self.time_limit_seconds <= 0 or self.mip_relative_gap < 0:
            raise ValueError("求解时限必须为正数，MIP gap 不能为负")
        self.storage.validate()
        for scenario in self.scenarios:
            if not math.isfinite(float(scenario.probability)) or scenario.probability < 0:
                raise ValueError("场景概率不能为负")
            if set(scenario.customer_load_mwh) != set(customer_ids):
                raise ValueError("场景 %s 的客户负荷 ID 必须与客户集合一致" % scenario.scenario_id)
            for customer_id, values in scenario.customer_load_mwh.items():
                if any(value < 0 for value in _curve(values, PERIODS, customer_id + ".load")):
                    raise ValueError("场景负荷不能为负")
            _curve(scenario.day_ahead_price, PERIODS, scenario.scenario_id + ".day_ahead_price")
            _curve(scenario.real_time_price, PERIODS, scenario.scenario_id + ".real_time_price")
            _curve(scenario.spot_reference_price, PERIODS, scenario.scenario_id + ".spot_reference_price")
        if abs(sum(item.probability for item in self.scenarios) - 1.0) > 1e-8:
            raise ValueError("场景概率之和必须为 1")
        if not math.isfinite(float(self.old_contract_cost_yuan)):
            raise ValueError("old_contract_cost_yuan 必须为有限数值")
        for product in self.contract_products:
            _curve(product.buy_price, CONTRACT_PERIODS, product.product_id + ".buy_price")
            _curve(product.sell_price, CONTRACT_PERIODS, product.product_id + ".sell_price")
            _curve(product.buy_limit_mwh, CONTRACT_PERIODS, product.product_id + ".buy_limit")
            _curve(product.sell_limit_mwh, CONTRACT_PERIODS, product.product_id + ".sell_limit")
            if any(value < 0 for value in product.buy_limit_mwh) or any(value < 0 for value in product.sell_limit_mwh):
                raise ValueError("合同产品可交易上限不能为负")
        for value, name in ((self.declaration_p10_mwh, "declaration_p10_mwh"), (self.declaration_p90_mwh, "declaration_p90_mwh"), (self.deviation_buy_slack_limit_mwh, "deviation_buy_slack_limit_mwh"), (self.deviation_sell_slack_limit_mwh, "deviation_sell_slack_limit_mwh")):
            if value is not None:
                curve = _curve(value, PERIODS, name)
                if any(item < 0 for item in curve):
                    raise ValueError(name + " 不能为负")
        if self.declaration_p10_mwh is not None and self.declaration_p90_mwh is not None:
            if any(
                float(lower) > float(upper)
                for lower, upper in zip(self.declaration_p10_mwh, self.declaration_p90_mwh)
            ):
                raise ValueError("日前申报 P10 不能高于 P90")
        state = self.locked_state
        if type(state.fixed_until) is not int or not 0 <= state.fixed_until <= PERIODS:
            raise ValueError("locked_state.fixed_until 必须位于 [0, 96]")
        if state.actual_aggregate_load_mwh and len(state.actual_aggregate_load_mwh) != PERIODS:
            raise ValueError("actual_aggregate_load_mwh 必须包含 96 个点")
        if state.fixed_until and len(state.actual_aggregate_load_mwh) != PERIODS:
            raise ValueError("锁定执行前缀需要 96 点 actual_aggregate_load_mwh")
        if state.actual_aggregate_load_mwh:
            if any(value < 0 for value in _curve(state.actual_aggregate_load_mwh, PERIODS, "actual_aggregate_load_mwh")):
                raise ValueError("实际聚合负荷不能为负")
        if state.fixed_until:
            required_prefix_maps = (
                (state.real_time_buy_mwh, "real_time_buy_mwh"),
                (state.real_time_sell_mwh, "real_time_sell_mwh"),
                (state.charge_mwh, "charge_mwh"),
                (state.discharge_mwh, "discharge_mwh"),
                (state.soc_mwh, "soc_mwh"),
            )
            for values, name in required_prefix_maps:
                missing = set(range(state.fixed_until)) - set(values)
                if missing:
                    raise ValueError("锁定执行前缀缺少 %s 时段数据" % name)
        if any(package not in PACKAGES for package in state.locked_packages.values()):
            raise ValueError("locked_packages 包含未知套餐")
        if set(state.locked_packages) - set(customer_ids):
            raise ValueError("locked_packages 包含未知客户")
        if set(self.customer_bill_limits_yuan) - set(customer_ids):
            raise ValueError("customer_bill_limits_yuan 包含未知客户")
        if any(
            not math.isfinite(float(value)) or float(value) < 0
            for value in self.customer_bill_limits_yuan.values()
        ):
            raise ValueError("客户账单上限必须为非负有限数值")
        for period_map in (state.declaration_mwh, state.real_time_buy_mwh, state.real_time_sell_mwh, state.charge_mwh, state.discharge_mwh, state.soc_mwh):
            if any(type(index) is not int or not 0 <= index < PERIODS for index in period_map):
                raise ValueError("锁定时段索引必须位于 [0, 95]")
            if any(not math.isfinite(value) or value < 0 for value in period_map.values()):
                raise ValueError("锁定电量和 SOC 必须为非负有限数值")
        for period_map in (state.real_time_buy_mwh, state.real_time_sell_mwh, state.charge_mwh, state.discharge_mwh, state.soc_mwh):
            if any(index >= state.fixed_until for index in period_map):
                raise ValueError("实时执行锁定数据不能超出 fixed_until")


@dataclass(frozen=True)
class RetailSettlement:
    customer_prices: Mapping[str, Tuple[float, ...]]
    customer_bills_yuan: Mapping[str, float]
    retail_revenue_yuan: float
