from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pifa_lingshou.data_objects.domain import ContractFill
from pifa_lingshou.inputs.load_forecast import (
    IndustrialParkConfig,
    month_equivalent_days,
    monthly_curve,
    product_equivalent_days,
    quantile_curve,
)
from pifa_lingshou.problem_solver.milp import LinearMilp, MilpBackendUnavailable, MilpSolveError
from pifa_lingshou.data_objects.scenario import (
    JointTrajectoryScenario,
    discrete_load_scenarios,
    discrete_load_value,
)


L1_EVENTS = ("ANNUAL", "MONTHLY", "TEN_DAY")
L2_NEAR_TERM_EVENTS = ("D-3", "D-2")
TRADE_EVENTS = L1_EVENTS + L2_NEAR_TERM_EVENTS
ROLLING_EVENTS = ("D-3", "D-2")


@dataclass(frozen=True)
class TraderConfig:
    annual_coverage: float = 0.80
    monthly_coverage: float = 0.95
    ten_day_coverage: float = 0.98
    d3_coverage: float = 1.00
    d2_coverage: float = 1.00
    target_quantile: str = "P50"
    minimum_edge_yuan_per_mwh: float = 2.0
    deadband_ratio: float = 0.005
    maximum_node_adjustment_mwh: float = 8000.0
    allow_sell: bool = True
    rolling_interval_limit_ratio: float = 0.20
    rolling_daily_limit_ratio: float = 0.05
    rolling_sell_basis: str = "P50"
    rolling_user_buy_ceiling_yuan_per_mwh: float = 1000.0
    rolling_user_sell_floor_yuan_per_mwh: float = 0.0
    # Backward-compatible alias for callers using the former symmetric threshold.
    rolling_price_edge_yuan_per_mwh: Optional[float] = None
    rolling_min_fill_ratio: float = 0.10
    rolling_max_fill_ratio: float = 0.20
    rolling_fill_ramp_yuan_per_mwh: float = 25.0
    rolling_position_lower_ratio: float = 0.90
    rolling_position_upper_ratio: float = 1.10
    rolling_l3_marginal_gate: bool = True
    rolling_l3_min_net_benefit_yuan_per_mwh: float = 0.0
    annual_price_yuan_per_mwh: float = 405.0
    monthly_price_yuan_per_mwh: float = 416.0
    ten_day_price_yuan_per_mwh: float = 424.0
    rolling_price_edge_lower_yuan_per_mwh: float = 100.0
    # Direct strategy callers retain the former symmetric 100 yuan default;
    # the simulation/UI configuration exposes the explicit 100/200 bounds.
    rolling_price_edge_upper_yuan_per_mwh: float = 100.0

    def coverage(self, event: str) -> float:
        return {
            "ANNUAL": self.annual_coverage,
            "MONTHLY": self.monthly_coverage,
            "TEN_DAY": self.ten_day_coverage,
            "D-3": self.d3_coverage,
            "D-2": self.d2_coverage,
        }[event]

    @property
    def price_edge_lower_yuan_per_mwh(self) -> float:
        return (
            self.rolling_price_edge_yuan_per_mwh
            if self.rolling_price_edge_yuan_per_mwh is not None
            else self.rolling_price_edge_lower_yuan_per_mwh
        )

    @property
    def price_edge_upper_yuan_per_mwh(self) -> float:
        return (
            self.rolling_price_edge_yuan_per_mwh
            if self.rolling_price_edge_yuan_per_mwh is not None
            else self.rolling_price_edge_upper_yuan_per_mwh
        )

    def validate(self) -> None:
        values = [
            self.annual_coverage,
            self.monthly_coverage,
            self.ten_day_coverage,
            self.d3_coverage,
            self.d2_coverage,
        ]
        if any(value < 0.0 or value > 1.2 for value in values):
            raise ValueError("各节点交易员基线覆盖率必须位于 [0, 1.2]")
        prices = (
            self.annual_price_yuan_per_mwh,
            self.monthly_price_yuan_per_mwh,
            self.ten_day_price_yuan_per_mwh,
        )
        if any(value < -2000.0 or value > 5000.0 for value in prices):
            raise ValueError("中长期电价必须位于 [-2000, 5000] 元/MWh")
        if self.target_quantile not in {"P50", "P75", "P90"}:
            raise ValueError("target_quantile 只能为 P50、P75 或 P90")
        if self.maximum_node_adjustment_mwh <= 0:
            raise ValueError("maximum_node_adjustment_mwh 必须大于 0")
        if not 0.0 <= self.deadband_ratio <= 0.20:
            raise ValueError("deadband_ratio 必须位于 [0, 0.20]")
        if not 0.0 <= self.rolling_interval_limit_ratio <= 1.0:
            raise ValueError("rolling_interval_limit_ratio 必须位于 [0, 1]")
        if not 0.0 <= self.rolling_daily_limit_ratio <= 1.0:
            raise ValueError("rolling_daily_limit_ratio 必须位于 [0, 1]")
        if self.rolling_sell_basis not in {"P10", "P50", "P90"}:
            raise ValueError("rolling_sell_basis 只能为 P10、P50 或 P90")
        if self.rolling_user_buy_ceiling_yuan_per_mwh < -2000.0:
            raise ValueError("滚撮用户买入上限价不能低于 -2000 元/MWh")
        if self.rolling_user_sell_floor_yuan_per_mwh > 5000.0:
            raise ValueError("滚撮用户卖出下限价不能高于 5000 元/MWh")
        if self.price_edge_lower_yuan_per_mwh < 0.0 or self.price_edge_upper_yuan_per_mwh < 0.0:
            raise ValueError("滚撮成交价差上下限不能小于0元/MWh")
        if self.price_edge_lower_yuan_per_mwh > self.price_edge_upper_yuan_per_mwh:
            raise ValueError("滚撮成交价差下限不能高于上限")
        if not 0.0 < self.rolling_min_fill_ratio <= self.rolling_max_fill_ratio <= 1.0:
            raise ValueError("滚撮成交比例必须满足 0 < 最小比例 <= 最大比例 <= 1")
        if self.rolling_fill_ramp_yuan_per_mwh <= 0.0:
            raise ValueError("滚撮成交比例爬坡价差必须大于0元/MWh")
        if not 0.0 <= self.rolling_position_lower_ratio <= self.rolling_position_upper_ratio:
            raise ValueError("滚撮仓位比例下限不能超过上限")
        if self.rolling_l3_min_net_benefit_yuan_per_mwh < 0.0:
            raise ValueError("L3最小净收益门槛不能小于0元/MWh")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProductQuote:
    event: str
    buy_price: float
    sell_price: float
    variable_fee: float
    minimum_quantity: float
    maximum_quantity: float


@dataclass(frozen=True)
class TradeAction:
    strategy: str
    event: str
    side: str
    quantity_mwh: float
    previous_position_mwh: float
    target_position_mwh: float
    resulting_position_mwh: float
    execution_price_yuan_per_mwh: float
    fee_yuan: float
    score_yuan: float
    reason: str
    market_scope: str = "MONTH"
    forecast_basis_mwh: float = 0.0
    risk_target_position_mwh: float = 0.0
    rolling_sell_cap_mwh: float = 0.0
    rolling_sell_remaining_mwh: float = 0.0
    rolling_interval_limit_ratio: float = 0.0
    solver_backend: str = "TRADER_RULE"
    solver_status: str = "NOT_APPLICABLE"
    mip_gap: Optional[float] = None
    solver_message: str = ""
    equivalent_delivery_days: float = 1.0
    position_unit: str = "MWh/product_scope"
    previous_daily_position_mwh: float = 0.0
    target_daily_position_mwh: float = 0.0
    resulting_daily_position_mwh: float = 0.0
    # Internal 48-product delivery shape for a newly executed fill.  It is
    # intentionally omitted from the public action payload.
    delivery_curve: Mapping[str, float] = field(default_factory=dict, repr=False)
    order_decisions: Sequence[Mapping[str, object]] = field(default_factory=tuple, repr=False)
    valuation_summary: Mapping[str, object] = field(default_factory=dict, repr=False)
    buy_quantity_mwh: float = 0.0
    sell_quantity_mwh: float = 0.0
    net_quantity_mwh: float = 0.0
    solver_details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("delivery_curve", None)
        payload["order_decisions"] = [dict(item) for item in self.order_decisions]
        payload["valuation_summary"] = dict(self.valuation_summary)
        return payload


def default_quotes(config: Optional[TraderConfig] = None) -> Dict[str, ProductQuote]:
    config = config or TraderConfig()
    return {
        "ANNUAL": ProductQuote("ANNUAL", config.annual_price_yuan_per_mwh, config.annual_price_yuan_per_mwh - 8.0, 0.10, 100.0, 8000.0),
        "MONTHLY": ProductQuote("MONTHLY", config.monthly_price_yuan_per_mwh, config.monthly_price_yuan_per_mwh - 7.0, 0.10, 50.0, 3000.0),
        "TEN_DAY": ProductQuote("TEN_DAY", config.ten_day_price_yuan_per_mwh, config.ten_day_price_yuan_per_mwh - 6.0, 0.12, 25.0, 1500.0),
        "D-3": ProductQuote("D-3", 429.0, 424.0, 0.15, 1.0, 800.0),
        "D-2": ProductQuote("D-2", 432.0, 428.0, 0.15, 1.0, 600.0),
    }


def _monthly_target_energy(snapshot: Mapping[str, object], config: IndustrialParkConfig, quantile: str) -> float:
    return sum(monthly_curve(quantile_curve(snapshot, quantile), config))


def _daily_target_energy(snapshot: Mapping[str, object], quantile: str) -> float:
    return sum(quantile_curve(snapshot, quantile))


def _rolling_sell_cap(
    snapshot: Mapping[str, object],
    config: TraderConfig,
    prior_sell_curve: Sequence[float] = (),
) -> float:
    rows = snapshot["rows"]
    key = config.rolling_sell_basis.lower() + "_mwh"
    p50_total = sum(float(row["p50_mwh"]) for row in rows)
    prior = list(prior_sell_curve) if prior_sell_curve else [0.0] * len(rows)
    if len(prior) != len(rows):
        raise ValueError("历史滚撮卖出曲线长度必须与预测时段一致")
    interval_capacity = min(
        (
            (
                config.rolling_interval_limit_ratio * float(row[key])
                - prior[index]
            )
            / (float(row["p50_mwh"]) / p50_total)
            for index, row in enumerate(rows)
            if float(row["p50_mwh"]) > 0.0
        ),
        default=0.0,
    )
    daily_capacity = (
        config.rolling_daily_limit_ratio * sum(float(row[key]) for row in rows)
        - sum(prior)
    )
    return max(0.0, min(interval_capacity, daily_capacity))


def _target_position(
    event: str,
    snapshot: Mapping[str, object],
    previous_position: float,
    config: TraderConfig,
    park_config: IndustrialParkConfig,
) -> Tuple[float, float]:
    daily_forecast = _daily_target_energy(snapshot, config.target_quantile)
    scope_days = product_equivalent_days(event, park_config)
    forecast = daily_forecast * scope_days
    return config.coverage(event) * forecast, forecast


def _execution_price(action: float, quote: ProductQuote) -> float:
    return quote.buy_price if action >= 0 else quote.sell_price


def _curve_values(fills: Sequence[ContractFill], scope_days: float) -> List[float]:
    values = [0.0] * 48
    for fill in fills:
        scale = float(scope_days) / float(fill.equivalent_delivery_days)
        for index in range(48):
            values[index] += scale * float(
                fill.delivery_curve.get("P%02d" % (index + 1), 0.0)
            )
    return values


def _assessment_curve_values(
    fills: Sequence[ContractFill], product_classes: Sequence[str] = (), scope_days=None
) -> List[float]:
    """Return raw 48-point contract energy for settlement assessment."""

    allowed = set(product_classes)
    values = [0.0] * 48
    for fill in fills:
        if allowed and fill.product_class not in allowed:
            continue
        if not allowed and not fill.assessment_base_eligible:
            continue
        for index in range(48):
            scale = 1.0 if scope_days is None else scope_days / fill.equivalent_delivery_days
            values[index] += scale * float(
                fill.delivery_curve.get("P%02d" % (index + 1), 0.0)
            )
    return values


def _l1_assessment_floor_curve(
    event: str,
    snapshot: Mapping[str, object],
    config: TraderConfig,
    park_config: IndustrialParkConfig,
) -> List[float]:
    """Return the hard per-point L1 quantity required for assessment."""

    if event not in L1_EVENTS:
        return [0.0] * 48
    demand = [
        value * product_equivalent_days(event if park_config.delivery_days_override else "ANNUAL", park_config)
        for value in quantile_curve(snapshot, config.target_quantile)
    ]
    if event == "ANNUAL":
        ratio = max(0.60, config.annual_coverage)
    else:
        # The overall assessment lower bound is mandatory even when the
        # commercial coverage slider is set below it.
        ratio = max(0.90, min(config.coverage(event), 1.10))
    return [ratio * value for value in demand]


def _period_spot_curve(snapshot: Mapping[str, object], expected_spot_price: float) -> List[float]:
    """Deterministic 48-point L2 price proxy when no detailed price forecast exists."""

    if 'spot_price_forecast' in snapshot:
        values = [float(v) for v in snapshot['spot_price_forecast']]
        if len(values) != 48:
            raise ValueError('节点现货预测须为48个半小时价格')
        return values
    rows = snapshot["rows"]
    if len(rows) != 48:
        raise ValueError("中长期分时优化必须使用48点产品曲线")
    loads = [max(float(row["p50_mwh"]), 0.0) for row in rows]
    mean_load = sum(loads) / max(len(loads), 1)
    spread = 30.0
    return [
        float(expected_spot_price)
        + spread * (load / max(mean_load, 1e-9) - 1.0)
        for load in loads
    ]


def _scenario_period_demands(
    snapshot: Mapping[str, object],
    scenarios: Sequence[object],
    scope_days: float,
    joint_scenarios: Sequence[JointTrajectoryScenario],
) -> List[List[float]]:
    if joint_scenarios:
        return [
            [
                (float(scenario.load_mwh[2 * index]) + float(scenario.load_mwh[2 * index + 1]))
                * scope_days
                for index in range(48)
            ]
            for scenario in joint_scenarios
        ]
    return [
        [
            discrete_load_value(row, scenario) * scope_days
            for row in snapshot["rows"]
        ]
        for scenario in scenarios
    ]


def _scenario_period_prices(
    scenarios: Sequence[object],
    expected_spot_price: float,
    proxy_curve: Sequence[float],
    joint_scenarios: Sequence[JointTrajectoryScenario],
) -> List[List[float]]:
    if joint_scenarios:
        return [
            [
                0.5
                * (
                    float(scenario.real_time_price[2 * index])
                    + float(scenario.real_time_price[2 * index + 1])
                )
                for index in range(48)
            ]
            for scenario in joint_scenarios
        ]
    return [
        [float(value) * scenario.real_time_price_factor for value in proxy_curve]
        for scenario in scenarios
    ]


def baseline_action(
    event: str,
    snapshot: Mapping[str, object],
    previous_position: float,
    config: TraderConfig,
    park_config: IndustrialParkConfig,
    quote: ProductQuote,
    expected_spot_price: object,
    rolling_sell_used: float,
    rolling_sell_curve: Sequence[float] = (),
) -> TradeAction:
    target, forecast_energy = _target_position(event, snapshot, previous_position, config, park_config)
    scope_days = product_equivalent_days(event, park_config)
    raw_gap = target - previous_position
    deadband = config.deadband_ratio * forecast_energy
    maximum = min(config.maximum_node_adjustment_mwh, quote.maximum_quantity)
    action = max(-maximum, min(maximum, raw_gap))
    sell_remaining = (
        _rolling_sell_cap(snapshot, config, rolling_sell_curve)
        if event in ROLLING_EVENTS
        else 0.0
    )
    sell_cap = rolling_sell_used + sell_remaining
    can_sell = config.allow_sell and event in ROLLING_EVENTS
    reason = "按 %s 预测的 %.0f%% 交易员基线目标补足仓位" % (
        config.target_quantile,
        100.0 * config.coverage(event),
    )
    if abs(raw_gap) <= deadband:
        action = 0.0
        reason = "目标缺口位于 %.1f MWh 无交易带内" % deadband
    elif action > 0 and quote.buy_price + quote.variable_fee > expected_spot_price - config.minimum_edge_yuan_per_mwh:
        action = 0.0
        reason = "买价未达到最小价差门槛，保留缺口等待下一节点"
    elif action < 0 and not can_sell:
        action = 0.0
        reason = "年度、月度和旬内只允许买入或持有；卖出仅在滚撮节点开放"
    elif action < 0:
        action = max(action, -sell_remaining)
        reason = "最新预测下调，在滚撮累计卖出上限内回售超额仓位"
    if action and abs(action) < quote.minimum_quantity:
        if action < 0 and quote.minimum_quantity > sell_remaining:
            action = 0.0
            reason = "滚撮剩余可卖量低于产品最小成交量"
        else:
            action = quote.minimum_quantity if action > 0 else -quote.minimum_quantity
    resulting = max(0.0, previous_position + action)
    action = resulting - previous_position
    side = "BUY" if action > 0 else "SELL" if action < 0 else "HOLD"
    price = _execution_price(action, quote) if action else 0.0
    return TradeAction(
        "BASELINE",
        event,
        side,
        round(abs(action), 3),
        round(previous_position, 3),
        round(target, 3),
        round(resulting, 3),
        price,
        round(abs(action) * quote.variable_fee, 2),
        0.0,
        reason,
        market_scope="DELIVERY_DAY" if event in ROLLING_EVENTS else "MONTH",
        forecast_basis_mwh=round(forecast_energy, 3),
        risk_target_position_mwh=round(target, 3),
        rolling_sell_cap_mwh=round(sell_cap, 3),
        rolling_sell_remaining_mwh=round(max(0.0, sell_remaining - max(-action, 0.0)), 3),
        rolling_interval_limit_ratio=config.rolling_interval_limit_ratio,
        equivalent_delivery_days=scope_days,
        previous_daily_position_mwh=round(previous_position / scope_days, 3),
        target_daily_position_mwh=round(target / scope_days, 3),
        resulting_daily_position_mwh=round(resulting / scope_days, 3),
    )


def _candidate_score(
    final_position: float,
    previous_position: float,
    previous_notional: float,
    quote: ProductQuote,
    scenario_demands: Sequence[float],
    expected_spot_price: float,
    risk_lambda: float,
    annual_position: float,
    assessment_demands: Sequence[float],
    assessed_position: float,
) -> float:
    action = final_position - previous_position
    contract_notional = previous_notional + action * _execution_price(action, quote)
    costs: List[float] = []
    scenarios = discrete_load_scenarios()
    for scenario, demand, assessment_demand in zip(
        scenarios, scenario_demands, assessment_demands
    ):
        spot = expected_spot_price * scenario.real_time_price_factor
        imbalance = demand - final_position
        balancing = max(imbalance, 0.0) * spot + min(imbalance, 0.0) * (spot - 18.0)
        ratio_penalty = 1.05 * abs(expected_spot_price - quote.buy_price) * (
            max(0.90 * assessment_demand - assessed_position, 0.0)
            + max(assessed_position - 1.10 * assessment_demand, 0.0)
        )
        annual_penalty = 1.05 * 12.0 * max(
            0.60 * assessment_demand - annual_position, 0.0
        )
        costs.append(contract_notional + balancing + ratio_penalty + annual_penalty + abs(action) * quote.variable_fee)
    mean = sum(
        scenario.probability * cost
        for scenario, cost in zip(scenarios, costs)
    )
    tail = max(costs)
    return (1.0 - risk_lambda) * mean + risk_lambda * tail


def _reference_optimized_action(
    event: str,
    snapshot: Mapping[str, object],
    previous_position: float,
    previous_notional: float,
    annual_position: float,
    config: TraderConfig,
    park_config: IndustrialParkConfig,
    quote: ProductQuote,
    expected_spot_price: float,
    risk_lambda: float,
    overall_position: float,
    rolling_sell_used: float,
    rolling_sell_curve: Sequence[float] = (),
    prior_fills: Sequence[ContractFill] = (),
) -> TradeAction:
    scenarios = discrete_load_scenarios()
    daily_demands = [
        sum(discrete_load_value(row, scenario) for row in snapshot["rows"])
        for scenario in scenarios
    ]
    scope_days = product_equivalent_days(event, park_config)
    target, forecast_energy = _target_position(event, snapshot, previous_position, config, park_config)
    demands = [demand * scope_days for demand in daily_demands]
    assessment_demands = [
        demand * product_equivalent_days("ANNUAL", park_config)
        for demand in daily_demands
    ]
    assessment_floor = _l1_assessment_floor_curve(
        event, snapshot, config, park_config
    )
    locked_assessment = _assessment_curve_values(
        prior_fills, ("ANNUAL",) if event == "ANNUAL" else ()
    )
    mandatory_curve = [
        max(floor - locked, 0.0)
        for floor, locked in zip(assessment_floor, locked_assessment)
    ]
    mandatory_quantity = sum(mandatory_curve)
    sell_remaining = (
        _rolling_sell_cap(snapshot, config, rolling_sell_curve)
        if event in ROLLING_EVENTS
        else 0.0
    )
    sell_cap = rolling_sell_used + sell_remaining
    can_sell = config.allow_sell and event in ROLLING_EVENTS
    sell_maximum = min(config.maximum_node_adjustment_mwh, quote.maximum_quantity, sell_remaining) if can_sell else 0.0
    lower = max(0.0, previous_position - sell_maximum)
    unconstrained_upper = previous_position + min(config.maximum_node_adjustment_mwh, quote.maximum_quantity)
    # Coverage defines the commercial target; risk aversion moves that target
    # continuously toward P90 instead of silently replacing it with P50.
    risk_envelope = target + risk_lambda * max(max(demands) - target, 0.0)
    hedge_upper = max(target, risk_envelope)
    if not can_sell:
        hedge_upper = max(previous_position, hedge_upper)
    upper = max(lower, min(unconstrained_upper, hedge_upper))
    step = max(1.0 if event in ROLLING_EVENTS else 10.0, (upper - lower) / 240.0)
    candidates = [lower + step * index for index in range(int((upper - lower) / step) + 1)]
    if lower <= previous_position <= upper:
        candidates.append(previous_position)
    candidates.append(max(lower, min(upper, target)))
    feasible: List[Tuple[float, float]] = []
    for candidate in candidates:
        action = candidate - previous_position
        if 0 < abs(action) < quote.minimum_quantity:
            continue
        if action > 0 and quote.buy_price + quote.variable_fee > expected_spot_price - config.minimum_edge_yuan_per_mwh:
            continue
        score = _candidate_score(
            candidate,
            previous_position,
            previous_notional,
            quote,
            demands,
            expected_spot_price,
            risk_lambda,
            annual_position if event != "ANNUAL" else candidate,
            assessment_demands,
            overall_position + action if event in TRADE_EVENTS else overall_position,
        )
        feasible.append((score, candidate))
    if not feasible:
        feasible.append(
            (
                _candidate_score(
                    previous_position,
                    previous_position,
                    previous_notional,
                    quote,
                    demands,
                    expected_spot_price,
                    risk_lambda,
                    annual_position,
                    assessment_demands,
                    overall_position,
                ),
                previous_position,
            )
        )
    score, resulting = min(feasible, key=lambda item: item[0])
    action = resulting - previous_position
    deadband_basis = forecast_energy if event in ROLLING_EVENTS else demands[2]
    if abs(action) <= config.deadband_ratio * deadband_basis:
        action = 0.0
        resulting = previous_position
    if event in L1_EVENTS and action < mandatory_quantity:
        action = mandatory_quantity
        resulting = previous_position + action
    side = "BUY" if action > 0 else "SELL" if action < 0 else "HOLD"
    delivery_curve: Dict[str, float] = {}
    if action > 0 and event in L1_EVENTS:
        resulting = previous_position + action
        previous_curve = _curve_values(prior_fills, scope_days)
        if event in {"ANNUAL", "MONTHLY"}:
            reference_curve = [
                value * scope_days
                for value in quantile_curve(snapshot, config.target_quantile)
            ]
            reference_total = sum(reference_curve)
            locked_ratio = max(
                (
                    previous / max(reference, 1e-9)
                    for previous, reference in zip(previous_curve, reference_curve)
                ),
                default=0.0,
            )
            target_ratio = max(
                locked_ratio,
                min(1.10, resulting / max(reference_total, 1e-9)),
            )
            allocated = [
                max(target_ratio * reference - previous, 0.0)
                for reference, previous in zip(reference_curve, previous_curve)
            ]
            action = sum(allocated)
            resulting = previous_position + action
            delivery_curve = {
                "P%02d" % (index + 1): round(value, 9)
                for index, value in enumerate(allocated)
            }
        else:
            period_capacity = [
                max(discrete_load_value(row, scenario) for scenario in scenarios)
                * scope_days
                for row in snapshot["rows"]
            ]
            proxy_prices = _period_spot_curve(snapshot, expected_spot_price)
            candidates_by_value = sorted(
                range(48),
                key=lambda index: (
                    proxy_prices[index] - quote.buy_price,
                    period_capacity[index] - previous_curve[index],
                ),
                reverse=True,
            )
            remaining = action - mandatory_quantity
            allocated = list(mandatory_curve)
            for index in candidates_by_value:
                capacity = max(
                    period_capacity[index]
                    - previous_curve[index]
                    - allocated[index],
                    0.0,
                )
                take = min(capacity, remaining)
                allocated[index] += take
                remaining -= take
                if remaining <= 1e-9:
                    break
            if remaining > 1e-9:
                weights = [max(float(row["p50_mwh"]), 0.0) for row in snapshot["rows"]]
                denominator = sum(weights)
                for index, weight in enumerate(weights):
                    allocated[index] += remaining * weight / max(denominator, 1e-9)
            residual = action - sum(allocated)
            allocated[-1] += residual
            delivery_curve = {
                "P%02d" % (index + 1): round(value, 9)
                for index, value in enumerate(allocated)
            }
    if event == "ANNUAL" and action:
        reason = (
            "日P50 %.1f MWh折算月度P50 %.1f MWh；年度只有60%%下限考核、无考核上限，"
            "但策略把超出P50的保量限制在未来滚撮可退出量内"
            % (daily_demands[2], demands[2])
        )
    elif event == "MONTHLY" and action:
        reason = "按最新月度预测补仓，并检查总体中长期累计比例是否位于90%-110%"
    elif event == "TEN_DAY" and not action and target < previous_position:
        reason = "旬内预测下调，但年月旬阶段禁止卖出，保留已成交仓位等待滚撮窗口"
    elif event in ROLLING_EVENTS and action:
        reason = "在当日预测和滚撮累计卖出上限 %.1f MWh 内调整中长期仓位" % sell_cap
    elif action:
        reason = "在P05/P25/P50/P75/P95需求、年度下限和年月中长期考核下搜索仓位"
    else:
        reason = "当前仓位已是约束范围内的最小风险成本点"
    return TradeAction(
        "OPTIMIZED",
        event,
        side,
        round(abs(action), 3),
        round(previous_position, 3),
        round(target, 3),
        round(resulting, 3),
        _execution_price(action, quote) if action else 0.0,
        round(abs(action) * quote.variable_fee, 2),
        round(score, 2),
        reason,
        market_scope="DELIVERY_DAY" if event in ROLLING_EVENTS else "MONTH",
        forecast_basis_mwh=round(forecast_energy, 3),
        risk_target_position_mwh=round(risk_envelope, 3),
        rolling_sell_cap_mwh=round(sell_cap, 3),
        rolling_sell_remaining_mwh=round(max(0.0, sell_remaining - max(-action, 0.0)), 3),
        rolling_interval_limit_ratio=config.rolling_interval_limit_ratio,
        equivalent_delivery_days=scope_days,
        previous_daily_position_mwh=round(previous_position / scope_days, 3),
        target_daily_position_mwh=round(target / scope_days, 3),
        resulting_daily_position_mwh=round(resulting / scope_days, 3),
        delivery_curve=delivery_curve,
    )


def solve_l1_contract_milp(*args, **kwargs) -> TradeAction:
    """Compatibility wrapper; implementation lives in l1_contract_milp.py."""

    from pifa_lingshou.problem_solver.l1_contract_milp import solve_l1_contract_milp as implementation

    return implementation(*args, **kwargs)


def _milp_optimized_action(
    event: str,
    snapshot: Mapping[str, object],
    previous_position: float,
    previous_notional: float,
    annual_position: float,
    overall_position: float,
    config: TraderConfig,
    park_config: IndustrialParkConfig,
    quote: ProductQuote,
    expected_spot_price: float,
    risk_lambda: float,
    rolling_sell_used: float,
    rolling_sell_curve: Sequence[float] = (),
    joint_scenarios: Sequence[JointTrajectoryScenario] = (),
    prior_fills: Sequence[ContractFill] = (),
    order_book: Mapping[str, object] = (),
    near_term_valuation: Mapping[str, object] = (),
) -> TradeAction:
    if event in L1_EVENTS:
        return solve_l1_contract_milp(
            event,
            snapshot,
            previous_position,
            previous_notional,
            annual_position,
            overall_position,
            config,
            park_config,
            quote,
            expected_spot_price,
            risk_lambda,
            prior_fills,
        )
    if order_book:
        return apply_l2_rolling_threshold(
            event,
            snapshot,
            previous_position,
            previous_notional,
            annual_position,
            overall_position,
            config,
            park_config,
            quote,
            expected_spot_price,
            risk_lambda,
            rolling_sell_used,
            rolling_sell_curve,
            joint_scenarios,
            prior_fills,
            order_book,
            near_term_valuation,
        )
    scenarios = tuple(joint_scenarios) if joint_scenarios else discrete_load_scenarios()
    daily_demands = (
        [sum(scenario.load_mwh) for scenario in scenarios]
        if joint_scenarios
        else [
            sum(discrete_load_value(row, scenario) for row in snapshot["rows"])
            for scenario in scenarios
        ]
    )
    scope_days = product_equivalent_days(event, park_config)
    assessment_demands = [demand * product_equivalent_days("ANNUAL", park_config) for demand in daily_demands]
    target, forecast_energy = _target_position(
        event, snapshot, previous_position, config, park_config
    )
    demands = [demand * scope_days for demand in daily_demands]

    sell_remaining = (
        _rolling_sell_cap(snapshot, config, rolling_sell_curve)
        if event in ROLLING_EVENTS
        else 0.0
    )
    sell_cap = rolling_sell_used + sell_remaining
    can_sell = config.allow_sell and event in ROLLING_EVENTS
    sell_maximum = (
        min(config.maximum_node_adjustment_mwh, quote.maximum_quantity, sell_remaining)
        if can_sell
        else 0.0
    )
    lower = max(0.0, previous_position - sell_maximum)
    unconstrained_upper = previous_position + min(
        config.maximum_node_adjustment_mwh, quote.maximum_quantity
    )
    high_demand = max(demands)
    risk_envelope = target + risk_lambda * max(high_demand - target, 0.0)
    hedge_upper = max(target, risk_envelope)
    if not can_sell:
        hedge_upper = max(previous_position, hedge_upper)
    upper = max(lower, min(unconstrained_upper, hedge_upper))
    buy_maximum = max(0.0, upper - previous_position)
    if quote.buy_price + quote.variable_fee > expected_spot_price - config.minimum_edge_yuan_per_mwh:
        buy_maximum = 0.0
        upper = previous_position
    sell_maximum = max(0.0, previous_position - lower)
    deadband_basis = forecast_energy if event in ROLLING_EVENTS else demands[2]
    minimum_trade = max(quote.minimum_quantity, config.deadband_ratio * deadband_basis)

    model = LinearMilp("L1-%s" % event)
    model.add_var("position", lower=lower, upper=upper)
    model.add_var("buy", upper=buy_maximum)
    model.add_var("sell", upper=sell_maximum)
    model.add_var("use_buy", upper=1.0, integer=True)
    model.add_var("use_sell", upper=1.0, integer=True)
    model.add_var("cvar_eta", lower=-1e8, upper=1e8)
    model.add_constraint(
        {"position": 1.0, "buy": -1.0, "sell": 1.0},
        lower=previous_position,
        upper=previous_position,
    )
    model.add_constraint({"buy": 1.0, "use_buy": -buy_maximum}, upper=0.0)
    model.add_constraint({"sell": 1.0, "use_sell": -sell_maximum}, upper=0.0)
    if buy_maximum >= minimum_trade > 0.0:
        model.add_constraint({"buy": 1.0, "use_buy": -minimum_trade}, lower=0.0)
    else:
        model.add_constraint({"use_buy": 1.0}, upper=0.0)
    if sell_maximum >= minimum_trade > 0.0:
        model.add_constraint({"sell": 1.0, "use_sell": -minimum_trade}, lower=0.0)
    else:
        model.add_constraint({"use_sell": 1.0}, upper=0.0)
    model.add_constraint({"use_buy": 1.0, "use_sell": 1.0}, upper=1.0)

    for scenario_index, (scenario, demand, assessment_demand) in enumerate(
        zip(scenarios, demands, assessment_demands)
    ):
        probability = scenario.probability
        suffix = str(scenario_index)
        spot_buy = model.add_var("spot_buy_" + suffix, upper=max(demand, upper) + 1.0)
        spot_sell = model.add_var("spot_sell_" + suffix, upper=max(demand, upper) + 1.0)
        annual_under = model.add_var("annual_under_" + suffix, upper=assessment_demand)
        ratio_under = model.add_var("ratio_under_" + suffix, upper=assessment_demand)
        ratio_over = model.add_var("ratio_over_" + suffix, upper=max(upper, assessment_demand))
        loss = model.add_var("loss_" + suffix, lower=-1e8, upper=1e8)
        excess = model.add_var("cvar_excess_" + suffix, upper=1e8)

        model.add_constraint(
            {"position": 1.0, spot_buy: 1.0, spot_sell: -1.0},
            lower=demand,
            upper=demand,
        )
        if event == "ANNUAL":
            model.add_constraint(
                {annual_under: 1.0, "position": 1.0},
                lower=0.60 * assessment_demand,
            )
        else:
            model.add_constraint(
                {annual_under: 1.0},
                lower=max(0.60 * assessment_demand - annual_position, 0.0),
            )

        if event == "ANNUAL":
            model.add_constraint({ratio_under: 1.0}, lower=0.0, upper=0.0)
            model.add_constraint({ratio_over: 1.0}, lower=0.0, upper=0.0)
        elif event == "MONTHLY":
            model.add_constraint(
                {ratio_under: 1.0, "position": 1.0},
                lower=0.90 * assessment_demand,
            )
            model.add_constraint(
                {ratio_over: 1.0, "position": -1.0},
                lower=-1.10 * assessment_demand,
            )
        else:
            model.add_constraint(
                {ratio_under: 1.0, "buy": 1.0, "sell": -1.0},
                lower=0.90 * assessment_demand - overall_position,
            )
            model.add_constraint(
                {ratio_over: 1.0, "buy": -1.0, "sell": 1.0},
                lower=overall_position - 1.10 * assessment_demand,
            )

        spot_price = (
            sum(scenario.real_time_price) / len(scenario.real_time_price)
            if joint_scenarios
            else expected_spot_price * scenario.real_time_price_factor
        )
        ratio_penalty = 1.05 * abs(expected_spot_price - quote.buy_price)
        loss_terms = {
            loss: 1.0,
            "buy": -(quote.buy_price + quote.variable_fee),
            "sell": quote.sell_price - quote.variable_fee,
            spot_buy: -spot_price,
            spot_sell: spot_price - 18.0,
            annual_under: -(1.05 * 12.0),
            ratio_under: -ratio_penalty,
            ratio_over: -ratio_penalty,
        }
        model.add_constraint(loss_terms, lower=0.0, upper=0.0)
        model.add_constraint(
            {loss: 1.0, "cvar_eta": -1.0, excess: -1.0},
            upper=0.0,
        )
        model.add_to_objective(loss, (1.0 - risk_lambda) * probability)
        if risk_lambda:
            model.add_to_objective(excess, risk_lambda * probability / 0.05)
    if risk_lambda:
        model.add_to_objective("cvar_eta", risk_lambda)

    solved = model.solve()
    buy = max(0.0, solved.values["buy"])
    sell = max(0.0, solved.values["sell"])
    if buy > 1e-5:
        side = "BUY"
        quantity = buy
        price = quote.buy_price
    elif sell > 1e-5:
        side = "SELL"
        quantity = sell
        price = quote.sell_price
    else:
        side = "HOLD"
        quantity = 0.0
        price = 0.0
    resulting = max(0.0, solved.values["position"])
    if side == "HOLD":
        reason = "随机MILP在合同、补救成本、考核和CVaR约束下选择保持当前仓位"
    elif event == "ANNUAL":
        reason = (
            "日P50 %.1f MWh折算月度P50 %.1f MWh；"
            "L1随机MILP联合年度下限、五档负荷场景、补救成本代理和CVaR确定年度保量"
            % (daily_demands[2], demands[2])
        )
    elif event == "MONTHLY":
        reason = "L1随机MILP联合总体中长期90%-110%考核与L2补救成本代理确定月度补仓"
    elif event == "TEN_DAY":
        reason = "L1随机MILP在年月旬禁止卖出和考核约束下确定旬内动作"
    else:
        reason = "滚撮按现货P50价差捕捉订单，并在90%-110%仓位边界内部分成交"
    return TradeAction(
        strategy="OPTIMIZED",
        event=event,
        side=side,
        quantity_mwh=round(quantity, 3),
        previous_position_mwh=round(previous_position, 3),
        target_position_mwh=round(target, 3),
        resulting_position_mwh=round(resulting, 3),
        execution_price_yuan_per_mwh=price,
        fee_yuan=round(quantity * quote.variable_fee, 2),
        score_yuan=round(solved.objective + previous_notional, 2),
        reason=reason,
        market_scope="DELIVERY_DAY" if event in ROLLING_EVENTS else "MONTH",
        forecast_basis_mwh=round(forecast_energy, 3),
        risk_target_position_mwh=round(risk_envelope, 3),
        rolling_sell_cap_mwh=round(sell_cap, 3),
        rolling_sell_remaining_mwh=round(max(0.0, sell_remaining - sell), 3),
        rolling_interval_limit_ratio=config.rolling_interval_limit_ratio,
        solver_backend=solved.backend,
        solver_status=solved.status,
        mip_gap=solved.mip_gap,
        solver_message="%d变量/%d二进制/%d约束" % (
            solved.variable_count,
            solved.binary_count,
            solved.constraint_count,
        ),
        equivalent_delivery_days=scope_days,
        previous_daily_position_mwh=round(previous_position / scope_days, 3),
        target_daily_position_mwh=round(target / scope_days, 3),
        resulting_daily_position_mwh=round(resulting / scope_days, 3),
    )


def rolling_partial_fill_ratio(*args, **kwargs) -> float:
    """Compatibility wrapper; implementation lives in l2_rolling_threshold.py."""

    from pifa_lingshou.problem_solver.l2_rolling_threshold import rolling_partial_fill_ratio as implementation

    return implementation(*args, **kwargs)


def apply_l2_rolling_threshold(*args, **kwargs) -> TradeAction:
    """Compatibility wrapper; implementation lives in l2_rolling_threshold.py."""

    from pifa_lingshou.problem_solver.l2_rolling_threshold import apply_l2_rolling_threshold as implementation

    return implementation(*args, **kwargs)


def optimized_action(
    event: str,
    snapshot: Mapping[str, object],
    previous_position: float,
    previous_notional: float,
    annual_position: float,
    overall_position: float,
    config: TraderConfig,
    park_config: IndustrialParkConfig,
    quote: ProductQuote,
    expected_spot_price: float,
    risk_lambda: float,
    rolling_sell_used: float,
    rolling_sell_curve: Sequence[float] = (),
    joint_scenarios: Sequence[JointTrajectoryScenario] = (),
    allow_diagnostic_policy: bool = False,
    prior_fills: Sequence[ContractFill] = (),
    order_book: Mapping[str, object] = (),
    near_term_valuation: Mapping[str, object] = (),
) -> TradeAction:
    if allow_diagnostic_policy:
        reference = _reference_optimized_action(
            event,
            snapshot,
            previous_position,
            previous_notional,
            annual_position,
            config,
            park_config,
            quote,
            expected_spot_price,
            risk_lambda,
            overall_position,
            rolling_sell_used,
            rolling_sell_curve,
            prior_fills,
        )
        return replace(
            reference,
            solver_backend="DIAGNOSTIC_POLICY_NOT_A_SOLVER",
            solver_status="DIAGNOSTIC_POLICY_ONLY",
            mip_gap=None,
            solver_message="显式 diagnostic_mode：未调用 MILP 求解器",
        )
    try:
        return _milp_optimized_action(
            event,
            snapshot,
            previous_position,
            previous_notional,
            annual_position,
            overall_position,
            config,
            park_config,
            quote,
            expected_spot_price,
            risk_lambda,
            rolling_sell_used,
            rolling_sell_curve,
            joint_scenarios,
            prior_fills,
            order_book,
            near_term_valuation,
        )
    except (MilpBackendUnavailable, MilpSolveError) as exc:
        if not allow_diagnostic_policy:
            raise
        reference = _reference_optimized_action(
            event,
            snapshot,
            previous_position,
            previous_notional,
            annual_position,
            config,
            park_config,
            quote,
            expected_spot_price,
            risk_lambda,
            overall_position,
            rolling_sell_used,
            rolling_sell_curve,
            prior_fills,
        )
        return replace(
            reference,
            solver_backend="DIAGNOSTIC_POLICY_NOT_A_SOLVER",
            solver_status="DIAGNOSTIC_POLICY_ONLY",
            mip_gap=None,
            solver_message=str(exc),
        )


def _fill_from_action(action: TradeAction, snapshot: Mapping[str, object]) -> Optional[ContractFill]:
    if action.side == "HOLD" or action.quantity_mwh <= 0:
        return None
    sign = 1.0 if action.side == "BUY" else -1.0
    signed_quantity = sign * action.quantity_mwh
    if action.delivery_curve:
        delivery = {
            "P%02d" % (index + 1): sign
            * abs(float(action.delivery_curve.get("P%02d" % (index + 1), 0.0)))
            for index in range(48)
        }
    else:
        curve = quantile_curve(snapshot, "P50")
        denominator = sum(curve)
        delivery = {
            "P%02d" % (index + 1): signed_quantity * value / denominator
            for index, value in enumerate(curve)
        }
    residual = signed_quantity - sum(delivery.values())
    adjustment_key = max(delivery, key=lambda key: abs(delivery[key]))
    delivery[adjustment_key] += residual
    return ContractFill(
        fill_id="%s-%s" % (action.strategy, action.event.replace("-", "")),
        product_class=action.event,
        event_id="DEMO-" + action.event,
        side=action.side,
        signed_quantity=signed_quantity,
        price=action.execution_price_yuan_per_mwh,
        fee=action.fee_yuan,
        delivery_curve=delivery,
        assessment_base_eligible=action.event in TRADE_EVENTS,
        equivalent_delivery_days=action.equivalent_delivery_days,
    )


def _fills_from_action(
    action: TradeAction, snapshot: Mapping[str, object]
) -> List[ContractFill]:
    """Expand a mixed order-book action into immutable per-order fills."""
    if action.order_decisions:
        fills: List[ContractFill] = []
        for decision in action.order_decisions:
            accepted = float(decision.get("accepted_quantity_mwh", 0.0))
            if accepted <= 1e-7:
                continue
            side = str(decision.get("our_side", "BUY"))
            if side not in {"BUY", "SELL"}:
                continue
            signed = accepted if side == "BUY" else -accepted
            period = int(decision["period"])
            delivery = {"P%02d" % (index + 1): 0.0 for index in range(48)}
            delivery["P%02d" % period] = signed
            fills.append(
                ContractFill(
                    fill_id="%s-%s" % (
                        action.event.replace("-", ""),
                        str(decision.get("order_id", period)),
                    ),
                    product_class=action.event,
                    event_id="DEMO-" + action.event,
                    side=side,
                    signed_quantity=signed,
                    price=float(decision.get("price_yuan_per_mwh", 0.0)),
                    fee=round(
                        accepted * action.fee_yuan / max(
                            action.buy_quantity_mwh + action.sell_quantity_mwh, 1e-9
                        ),
                        6,
                    ),
                    delivery_curve=delivery,
                    assessment_base_eligible=True,
                    equivalent_delivery_days=action.equivalent_delivery_days,
                )
            )
        return fills
    fill = _fill_from_action(action, snapshot)
    return [] if fill is None else [fill]


def build_portfolio(
    forecasts: Mapping[str, dict],
    trader_config: TraderConfig,
    park_config: IndustrialParkConfig,
    expected_spot_price: float,
    risk_lambda: float,
    strategy: str,
    joint_scenarios_by_event: Optional[Mapping[str, Sequence[JointTrajectoryScenario]]] = None,
    allow_diagnostic_policy: bool = False,
    order_books: Optional[Mapping[str, Mapping[str, object]]] = None,
    near_term_valuations: Optional[Mapping[str, Mapping[str, object]]] = None,
    near_term_valuation_builder: Optional[
        Callable[[str, Sequence[ContractFill]], Mapping[str, object]]
    ] = None,
) -> Mapping[str, object]:
    trader_config.validate()
    if strategy not in {"BASELINE", "OPTIMIZED"}:
        raise ValueError("strategy 只能为 BASELINE 或 OPTIMIZED")
    daily_position = 0.0
    notional = 0.0
    annual_position = 0.0
    overall_position = 0.0
    rolling_sell_used = 0.0
    rolling_sell_cap = 0.0
    rolling_sell_curve = [0.0] * 48
    actions: List[TradeAction] = []
    fills: List[ContractFill] = []
    quotes = default_quotes(trader_config)
    for event in TRADE_EVENTS:
        snapshot = forecasts[event]
        quote = quotes[event]
        scope_days = product_equivalent_days(event, park_config)
        position = daily_position * scope_days
        event_expected_spot = (
            float(expected_spot_price[event])
            if isinstance(expected_spot_price, Mapping)
            else float(expected_spot_price)
        )
        if strategy == "BASELINE":
            action = baseline_action(
                event,
                snapshot,
                position,
                trader_config,
                park_config,
                quote,
                event_expected_spot,
                rolling_sell_used,
                rolling_sell_curve,
            )
        else:
            near_term_valuation = (
                near_term_valuation_builder(event, fills)
                if event in ROLLING_EVENTS and near_term_valuation_builder is not None
                else (near_term_valuations or {}).get(event, {})
            )
            action = optimized_action(
                event,
                snapshot,
                position,
                notional,
                annual_position,
                overall_position,
                trader_config,
                park_config,
                quote,
                event_expected_spot,
                risk_lambda,
                rolling_sell_used,
                rolling_sell_curve,
                (joint_scenarios_by_event or {}).get(event, ()),
                allow_diagnostic_policy,
                fills,
                (order_books or {}).get(event, {}),
                near_term_valuation,
            )
        if action.order_decisions:
            notional += sum(
                (1.0 if str(item.get("our_side")) == "BUY" else -1.0)
                * float(item.get("accepted_quantity_mwh", 0.0))
                * float(item.get("price_yuan_per_mwh", 0.0))
                for item in action.order_decisions
            )
        else:
            signed_action = action.quantity_mwh if action.side == "BUY" else -action.quantity_mwh if action.side == "SELL" else 0.0
            notional += signed_action * action.execution_price_yuan_per_mwh
        if event in ROLLING_EVENTS:
            rolling_sell_cap = action.rolling_sell_cap_mwh
            rolling_sell_used += action.sell_quantity_mwh or (
                action.quantity_mwh if action.side == "SELL" else 0.0
            )
        action_fills = _fills_from_action(action, snapshot)
        if action_fills:
            fills.extend(action_fills)
            for fill in action_fills:
                if event in ROLLING_EVENTS and fill.side == "SELL":
                    for index in range(48):
                        rolling_sell_curve[index] += max(
                            -float(fill.delivery_curve.get("P%02d" % (index + 1), 0.0)),
                            0.0,
                        )
        if event in ROLLING_EVENTS and near_term_valuation_builder is not None:
            post_trade_valuation = near_term_valuation_builder(event, fills)
            action = replace(
                action,
                valuation_summary={
                    **dict(action.valuation_summary),
                    "pre_trade_objective_yuan": near_term_valuation.get("objective_yuan"),
                    "post_trade_objective_yuan": post_trade_valuation.get("objective_yuan"),
                    "delta_j_l3_yuan": (
                        round(
                            float(post_trade_valuation.get("objective_yuan"))
                            - float(near_term_valuation.get("objective_yuan")),
                            6,
                        )
                        if near_term_valuation.get("objective_yuan") is not None
                        and post_trade_valuation.get("objective_yuan") is not None
                        else None
                    ),
                    "pre_trade_cvar95_yuan": near_term_valuation.get("cvar95_yuan"),
                    "post_trade_cvar95_yuan": post_trade_valuation.get("cvar95_yuan"),
                    "delta_cvar95_yuan": (
                        round(
                            float(post_trade_valuation.get("cvar95_yuan"))
                            - float(near_term_valuation.get("cvar95_yuan")),
                            6,
                        )
                        if near_term_valuation.get("cvar95_yuan") is not None
                        and post_trade_valuation.get("cvar95_yuan") is not None
                        else None
                    ),
                    "post_trade_status": post_trade_valuation.get("status"),
                    "post_trade_end_to_end_seconds": post_trade_valuation.get(
                        "end_to_end_seconds"
                    ),
                    "post_trade_contract_mwh": post_trade_valuation.get(
                        "locked_contract_total_mwh"
                    ),
                },
            )
        actions.append(action)
        daily_position = sum(
            fill.signed_quantity / float(fill.equivalent_delivery_days)
            for fill in fills
        )
        annual_position = sum(
            fill.signed_quantity
            for fill in fills
            if fill.product_class == "ANNUAL"
        )
        overall_position = sum(
            fill.signed_quantity
            for fill in fills
            if fill.assessment_base_eligible
        )
    return {
        "strategy": strategy,
        "actions": [action.to_dict() for action in actions],
        "fills": fills,
        "final_position_mwh": round(daily_position, 3),
        "final_position_unit": "MWh/delivery_day",
        "annual_position_mwh": round(annual_position, 3),
        "overall_position_mwh": round(overall_position, 3),
        "rolling_sell_used_mwh": round(rolling_sell_used, 3),
        "rolling_sell_cap_mwh": round(rolling_sell_cap, 3),
        "contract_notional_yuan": round(notional, 2),
        "optimization": {
            "model_structure": "L1_CONTRACT_MILP_PLUS_L2_THRESHOLD_ROLLING_TRIGGER_PLUS_L3_MPC",
            "l1_events": list(L1_EVENTS),
            "l2_near_term_events": list(L2_NEAR_TERM_EVENTS),
            "l2_decision_rule": "SPOT_P50_PRICE_EDGE_THRESHOLD_THEN_10_TO_20_PERCENT_PARTIAL_FILL",
            "l2_solver_role": "RULE_TRIGGER_WITH_L3_MARGINAL_VALUE_GATE",
            "l1_allocation_grid": "48_HALF_HOUR_PRODUCTS",
            "l1_assessment_granularity": "48_HALF_HOUR_PERIODS",
            "overall_assessment_products": list(TRADE_EVENTS),
            "l1_period_curve_role": "HEDGE_DELIVERY_RECOURSE_VALUATION_AND_PERIOD_ASSESSMENT",
            "spot_assessment_granularity": "96_QUARTER_HOUR_PERIODS",
            "locking_rule": "每个节点只执行当前新增买卖；历史成交不可撤回，只能新增反向成交",
            "pre_spot_load_scenario_catalog": [
                asdict(item) for item in discrete_load_scenarios()
            ],
            "near_term_joint_scenario_rule": "100 seeded correlated 96-point trajectories",
                "near_term_order_selection": "real-time P50 forecast +/- price edge -> sequential 10%-20% partial fill within 90%-110% position band",
            "milp_executed": strategy == "OPTIMIZED"
            and all(
                action.solver_status in {"OPTIMAL", "LIMIT_REACHED"}
                for action in actions
            ),
            "backend": (
                actions[0].solver_backend
                if strategy == "OPTIMIZED" and actions
                else "TRADER_BASELINE"
            ),
            "statuses": [action.solver_status for action in actions],
            "max_mip_gap": max(
                (action.mip_gap for action in actions if action.mip_gap is not None),
                default=None,
            ),
            "messages": [
                action.solver_message for action in actions if action.solver_message
            ],
        },
    }
