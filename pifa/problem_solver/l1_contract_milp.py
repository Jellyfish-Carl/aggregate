"""Layer 1: annual, monthly, and ten-day contract optimization.

This module owns the Layer 1 contract MILP. Portfolio orchestration remains in
:mod:`service.trading`.
"""

from __future__ import annotations

from typing import List, Mapping, Sequence

from problem_solver.milp import LinearMilp
from inputs.load_forecast import product_equivalent_days, quantile_curve
from data_objects.scenario import discrete_load_scenarios
from service.trading import (
    ContractFill,
    IndustrialParkConfig,
    JointTrajectoryScenario,
    ProductQuote,
    TradeAction,
    TraderConfig,
    _assessment_curve_values,
    _curve_values,
    _l1_assessment_floor_curve,
    _period_spot_curve,
    _scenario_period_demands,
    _scenario_period_prices,
    _target_position,
)


def solve_l1_contract_milp(
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
    prior_fills: Sequence[ContractFill],
    scenario_revenue_yuan: Sequence[float] = (),
) -> TradeAction:
    """Layer 1 MILP for annual/monthly/ten-day purchases on the 48-point grid."""

    scenarios = tuple(discrete_load_scenarios())
    if scenario_revenue_yuan and len(scenario_revenue_yuan) != len(scenarios):
        raise ValueError("L1零售收入必须与五档负荷场景对应")
    scope_days = product_equivalent_days(event, park_config)
    target, forecast_energy = _target_position(
        event, snapshot, previous_position, config, park_config
    )
    period_demands = _scenario_period_demands(
        snapshot, scenarios, scope_days, ()
    )
    total_demands = [sum(values) for values in period_demands]
    assessment_scale = product_equivalent_days("ANNUAL", park_config) / scope_days
    assessment_period_demands = [
        [value * assessment_scale for value in values]
        for values in period_demands
    ]
    locked_annual_curve = _assessment_curve_values(prior_fills, ("ANNUAL",))
    locked_overall_curve = _assessment_curve_values(prior_fills)
    assessment_floor = _l1_assessment_floor_curve(
        event, snapshot, config, park_config
    )
    locked_assessment = (
        locked_annual_curve if event == "ANNUAL" else locked_overall_curve
    )
    mandatory_buy_curve = [
        max(floor - locked, 0.0)
        for floor, locked in zip(assessment_floor, locked_assessment)
    ]
    mandatory_buy_total = sum(mandatory_buy_curve)
    previous_curve = _curve_values(prior_fills, scope_days)
    previous_total = sum(previous_curve)
    if abs(previous_total - previous_position) > 1e-3:
        raise ValueError("历史中长期分时曲线与总仓位不一致")
    position_reference_curve = [
        value * scope_days
        for value in quantile_curve(snapshot, config.target_quantile)
    ]
    position_reference_total = sum(position_reference_curve)
    minimum_proportional_total = previous_total
    if event in {"ANNUAL", "MONTHLY"}:
        minimum_proportional_total = max(
            (
                previous / max(reference, 1e-9) * position_reference_total
                for previous, reference in zip(previous_curve, position_reference_curve)
            ),
            default=previous_total,
        )
    required_buy_total = max(
        mandatory_buy_total,
        minimum_proportional_total - previous_total,
    )
    risk_envelope = target + risk_lambda * max(max(total_demands) - target, 0.0)
    total_upper = max(
        previous_total,
        min(
            previous_total + min(config.maximum_node_adjustment_mwh, quote.maximum_quantity),
            max(target, risk_envelope),
        ),
        previous_total + required_buy_total,
    )
    buy_maximum = max(0.0, total_upper - previous_total)
    proxy_curve = _period_spot_curve(snapshot, expected_spot_price)
    period_prices = _scenario_period_prices(
        scenarios, expected_spot_price, proxy_curve, ()
    )
    if (
        required_buy_total <= 1e-9
        and max(proxy_curve)
        <= quote.buy_price + quote.variable_fee + config.minimum_edge_yuan_per_mwh
    ):
        buy_maximum = 0.0
        total_upper = previous_total
    minimum_trade = max(
        quote.minimum_quantity,
        config.deadband_ratio * total_demands[2],
    )

    model = LinearMilp("L1-%s-48-PERIOD" % event)
    position_total = model.add_var(
        "position_total", lower=previous_total, upper=total_upper
    )
    buy_total = model.add_var("buy_total", upper=buy_maximum)
    use_buy = model.add_var("use_buy", upper=1.0, integer=True)
    model.add_var("cvar_eta", lower=-1e8, upper=1e8)
    buy_names: List[str] = []
    position_names: List[str] = []
    position_balance = {position_total: 1.0}
    buy_balance = {buy_total: 1.0}
    for index in range(48):
        period_upper = max(
            previous_curve[index],
            max(values[index] for values in period_demands),
            previous_curve[index] + mandatory_buy_curve[index],
        )
        position_name = model.add_var(
            "position_%02d" % index,
            lower=previous_curve[index],
            upper=period_upper,
        )
        buy_name = model.add_var(
            "buy_%02d" % index,
            upper=max(period_upper - previous_curve[index], 0.0),
        )
        model.add_constraint(
            {position_name: 1.0, buy_name: -1.0},
            lower=previous_curve[index],
            upper=previous_curve[index],
        )
        model.add_constraint(
            {buy_name: 1.0}, lower=mandatory_buy_curve[index]
        )
        position_balance[position_name] = -1.0
        buy_balance[buy_name] = -1.0
        position_names.append(position_name)
        buy_names.append(buy_name)
    model.add_constraint(position_balance, lower=0.0, upper=0.0)
    model.add_constraint(buy_balance, lower=0.0, upper=0.0)
    if event in {"ANNUAL", "MONTHLY"}:
        for index, position_name in enumerate(position_names):
            model.add_constraint(
                {
                    position_name: position_reference_total,
                    position_total: -position_reference_curve[index],
                },
                lower=0.0,
                upper=0.0,
            )
    model.add_constraint({buy_total: 1.0, use_buy: -buy_maximum}, upper=0.0)
    if buy_maximum >= minimum_trade > 0.0:
        model.add_constraint(
            {buy_total: 1.0, use_buy: -minimum_trade}, lower=0.0
        )
    else:
        model.add_constraint({use_buy: 1.0}, upper=0.0)

    for scenario_index, (scenario, demands, assessment_demands, spot_prices) in enumerate(
        zip(scenarios, period_demands, assessment_period_demands, period_prices)
    ):
        probability = scenario.probability
        suffix = str(scenario_index)
        loss = model.add_var("loss_" + suffix, lower=-1e8, upper=1e8)
        excess = model.add_var("cvar_excess_" + suffix, upper=1e8)
        loss_terms = {loss: 1.0}
        for index in range(48):
            cap = max(demands[index], total_upper) + 1.0
            spot_buy = model.add_var(
                "spot_buy_%s_%02d" % (suffix, index), upper=cap
            )
            spot_sell = model.add_var(
                "spot_sell_%s_%02d" % (suffix, index), upper=cap
            )
            model.add_constraint(
                {
                    position_names[index]: 1.0,
                    spot_buy: 1.0,
                    spot_sell: -1.0,
                },
                lower=demands[index],
                upper=demands[index],
            )
            loss_terms[buy_names[index]] = -(
                quote.buy_price + quote.variable_fee
            )
            loss_terms[spot_buy] = -spot_prices[index]
            loss_terms[spot_sell] = spot_prices[index] - 18.0
            assessment_demand = assessment_demands[index]
            annual_under = model.add_var(
                "annual_under_%s_%02d" % (suffix, index),
                upper=assessment_demand,
            )
            overall_under = model.add_var(
                "overall_under_%s_%02d" % (suffix, index),
                upper=assessment_demand,
            )
            overall_over = model.add_var(
                "overall_over_%s_%02d" % (suffix, index),
                upper=max(assessment_demand, total_upper),
            )
            if event == "ANNUAL":
                model.add_constraint(
                    {annual_under: 1.0, buy_names[index]: 1.0},
                    lower=0.60 * assessment_demand - locked_annual_curve[index],
                )
                model.add_constraint({overall_under: 1.0}, lower=0.0, upper=0.0)
                model.add_constraint({overall_over: 1.0}, lower=0.0, upper=0.0)
            else:
                model.add_constraint(
                    {annual_under: 1.0},
                    lower=max(
                        0.60 * assessment_demand - locked_annual_curve[index],
                        0.0,
                    ),
                )
                model.add_constraint(
                    {overall_under: 1.0, buy_names[index]: 1.0},
                    lower=0.90 * assessment_demand - locked_overall_curve[index],
                )
                model.add_constraint(
                    {overall_over: 1.0, buy_names[index]: -1.0},
                    lower=locked_overall_curve[index] - 1.10 * assessment_demand,
                )
            loss_terms[annual_under] = -(1.05 * 12.0)
            ratio_penalty = 1.05 * abs(expected_spot_price - quote.buy_price)
            loss_terms[overall_under] = -ratio_penalty
            loss_terms[overall_over] = -ratio_penalty
        revenue = float(scenario_revenue_yuan[scenario_index]) if scenario_revenue_yuan else 0.0
        model.add_constraint(loss_terms, lower=-revenue, upper=-revenue)
        model.add_constraint(
            {loss: 1.0, "cvar_eta": -1.0, excess: -1.0}, upper=0.0
        )
        model.add_to_objective(loss, (1.0 - risk_lambda) * probability)
        if risk_lambda:
            model.add_to_objective(excess, risk_lambda * probability / 0.05)
    if risk_lambda:
        model.add_to_objective("cvar_eta", risk_lambda)

    solved = model.solve()
    quantity = round(max(0.0, solved.values[buy_total]), 3)
    resulting = previous_total + quantity
    delivery_curve = {
        "P%02d" % (index + 1): round(max(0.0, solved.values[name]), 9)
        for index, name in enumerate(buy_names)
    }
    residual = quantity - sum(delivery_curve.values())
    adjustment_key = max(delivery_curve, key=delivery_curve.get)
    delivery_curve[adjustment_key] += residual
    active_periods = sum(value > 1e-6 for value in delivery_curve.values())
    side = "BUY" if quantity > 1e-5 else "HOLD"
    reason = (
        "L1分时随机MILP先满足48点中长期硬考核底线，在%d个点新增成交，"
        "再联合分时补救成本和CVaR确定%s仓位"
        % (active_periods, {"ANNUAL": "年度", "MONTHLY": "月度", "TEN_DAY": "旬内"}[event])
        if side == "BUY"
        else "L1分时随机MILP选择保持当前分时仓位"
    )
    return TradeAction(
        strategy="OPTIMIZED",
        event=event,
        side=side,
        quantity_mwh=quantity,
        previous_position_mwh=round(previous_total, 3),
        target_position_mwh=round(target, 3),
        resulting_position_mwh=round(resulting, 3),
        execution_price_yuan_per_mwh=quote.buy_price if side == "BUY" else 0.0,
        fee_yuan=round(quantity * quote.variable_fee, 2),
        score_yuan=round(solved.objective + previous_notional, 2),
        reason=reason,
        market_scope="MONTH",
        forecast_basis_mwh=round(forecast_energy, 3),
        risk_target_position_mwh=round(risk_envelope, 3),
        solver_backend=solved.backend,
        solver_status=solved.status,
        mip_gap=solved.mip_gap,
        solver_message="%d变量/%d二进制/%d约束；%d/48时段成交"
        % (
            solved.variable_count,
            solved.binary_count,
            solved.constraint_count,
            active_periods,
        ),
        equivalent_delivery_days=scope_days,
        previous_daily_position_mwh=round(previous_total / scope_days, 3),
        target_daily_position_mwh=round(target / scope_days, 3),
        resulting_daily_position_mwh=round(resulting / scope_days, 3),
        delivery_curve=delivery_curve,
        solver_details={key: getattr(solved, key) for key in (
            "backend", "status", "mip_gap", "solve_seconds", "variable_count", "binary_count", "constraint_count"
        )},
    )
