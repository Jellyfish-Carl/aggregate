"""Layer 2: D-3/D-2 rolling threshold trading.

This module owns the rolling rule engine. It is intentionally not a MILP:
orders are triggered by the spot-P50 price edge and filled at 10%-20%, with
an optional L3 marginal-value gate.
"""

from __future__ import annotations

from typing import List, Mapping, Optional, Sequence

from pifa_lingshou.inputs.load_forecast import month_equivalent_days, product_equivalent_days
from pifa_lingshou.data_objects.scenario import JointTrajectoryScenario
from pifa_lingshou.service.trading import (
    ContractFill,
    ProductQuote,
    TradeAction,
    TraderConfig,
    _assessment_curve_values,
)


def rolling_partial_fill_ratio(
    price_edge: float, config: TraderConfig, threshold: Optional[float] = None
) -> float:
    """Layer 2 partial-fill ratio between the configured 10%-20% bounds."""

    threshold = config.price_edge_lower_yuan_per_mwh if threshold is None else threshold
    excess = max(0.0, price_edge - threshold)
    progress = min(1.0, excess / config.rolling_fill_ramp_yuan_per_mwh)
    return config.rolling_min_fill_ratio + progress * (
        config.rolling_max_fill_ratio - config.rolling_min_fill_ratio
    )


def apply_l2_rolling_threshold(
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
    rolling_sell_curve: Sequence[float],
    joint_scenarios: Sequence[JointTrajectoryScenario],
    prior_fills: Sequence[ContractFill],
    order_book: Mapping[str, object],
    valuation: Mapping[str, object],
) -> TradeAction:
    """Layer 2 rule engine: trigger rolling orders and apply partial fills."""

    scope_days = product_equivalent_days(event, park_config)
    orders = sorted(
        order_book.get("orders", ()),
        key=lambda item: int(item.get("arrival_sequence", 0)),
    )
    if not orders:
        raise ValueError("滚撮订单簿为空")
    marginal_values = [
        float(value)
        for value in valuation.get(
            "rolling_half_hour_marginal_value_yuan_per_mwh", ()
        )
    ]
    if len(marginal_values) != 48:
        marginal_values = [0.0] * 48
    has_l3_marginal_values = any(abs(value) > 1e-6 for value in marginal_values)
    # Rolling products use the 48 half-hour delivery grid.  Aggregate the
    # 96 quarter-hour forecast before applying the monthly equivalent-day
    # factor; comparing a 48-point contract curve with the first 48 quarter
    # hours would distort the 90%-110% position gate.
    rows = snapshot["rows"]
    if len(rows) == 96:
        basis = [
            float(rows[2 * index]["p50_mwh"])
            + float(rows[2 * index + 1]["p50_mwh"])
            for index in range(48)
        ]
    elif len(rows) == 48:
        # build_portfolio receives the aggregated half-hour snapshot, while
        # direct rule tests and legacy callers may pass that grid directly.
        basis = [float(row["p50_mwh"]) for row in rows]
    else:
        raise ValueError("滚撮仓位考核需要48个半小时或96个15分钟负荷预测点")
    assessment_basis = [
        value * (scope_days if park_config.delivery_days_override else month_equivalent_days(park_config)) for value in basis
    ]
    locked_assessment_curve = _assessment_curve_values(prior_fills, scope_days=scope_days if park_config.delivery_days_override else None)
    lower_position = [
        config.rolling_position_lower_ratio * value for value in assessment_basis
    ]
    upper_position = [
        config.rolling_position_upper_ratio * value for value in assessment_basis
    ]
    initial_sell_room = sum(
        max(0.0, locked - lower)
        for locked, lower in zip(locked_assessment_curve, lower_position)
    )
    sell_maximum = min(
        config.maximum_node_adjustment_mwh,
        quote.maximum_quantity,
        initial_sell_room,
    )
    buy_maximum = min(config.maximum_node_adjustment_mwh, quote.maximum_quantity)
    decisions = []
    delivery = {"P%02d" % (h + 1): 0.0 for h in range(48)}
    buy_total_value = sell_total_value = 0.0
    fee = float(quote.variable_fee)
    for order in orders:
        period = int(order["period"]) - 1
        quantity = float(order["quantity_mwh"])
        price = float(order["price_yuan_per_mwh"])
        marginal_value = marginal_values[period]
        spot_forecast = float(order["real_time_reference_yuan_per_mwh"])
        # A missing/zero pricing-LP result cannot satisfy an enabled L3 gate.
        # Keep the spot-P50 reference for diagnostics, but reject the order.
        l3_value_available = abs(marginal_value) > 1e-6
        l3_reference_price = (
            marginal_value if l3_value_available else spot_forecast
        )
        threshold = (
            config.price_edge_lower_yuan_per_mwh
            if order["our_side"] == "BUY"
            else config.price_edge_upper_yuan_per_mwh
        )
        effective_buy_ceiling = spot_forecast - config.price_edge_lower_yuan_per_mwh
        effective_sell_floor = spot_forecast + config.price_edge_upper_yuan_per_mwh
        price_edge = (
            spot_forecast - price
            if order["our_side"] == "BUY"
            else price - spot_forecast
        )
        price_triggered = price_edge + 1e-9 >= threshold
        target_fill_ratio = rolling_partial_fill_ratio(price_edge, config, threshold)
        target_quantity = quantity * target_fill_ratio
        minimum_fill_quantity = quantity * config.rolling_min_fill_ratio
        current_period_position = (
            locked_assessment_curve[period]
            + float(delivery["P%02d" % int(order["period"])])
        )
        accepted = False
        accepted_quantity = 0.0
        rejection_code = None
        l3_net_benefit = (
            l3_reference_price - price - fee
            if order["our_side"] == "BUY"
            else price - l3_reference_price - fee
        )
        l3_gate_available = bool(
            config.rolling_l3_marginal_gate and l3_value_available
        )
        l3_gate_passed = (
            not config.rolling_l3_marginal_gate
            or (
                l3_gate_available
                and l3_net_benefit + 1e-9
                >= config.rolling_l3_min_net_benefit_yuan_per_mwh
            )
        )
        if order["our_side"] == "BUY":
            available_quantity = min(
                max(0.0, buy_maximum - buy_total_value),
                max(0.0, upper_position[period] - current_period_position),
            )
            if price > config.rolling_user_buy_ceiling_yuan_per_mwh:
                rejection_code = "USER_BUY_CEILING"
            elif not price_triggered:
                rejection_code = "ASK_EDGE_BELOW_THRESHOLD"
            elif not l3_gate_passed:
                rejection_code = (
                    "L3_MARGINAL_VALUE_UNAVAILABLE"
                    if config.rolling_l3_marginal_gate and not l3_value_available
                    else "L3_MARGINAL_VALUE_INSUFFICIENT"
                )
            elif available_quantity + 1e-9 < minimum_fill_quantity:
                rejection_code = "BUY_NODE_LIMIT"
            else:
                accepted = True
                accepted_quantity = min(target_quantity, available_quantity)
        else:
            available_quantity = min(
                max(0.0, sell_maximum - sell_total_value),
                max(0.0, current_period_position - lower_position[period]),
            )
            if price < config.rolling_user_sell_floor_yuan_per_mwh:
                rejection_code = "USER_SELL_FLOOR"
            elif not config.allow_sell:
                rejection_code = "SELL_DISABLED"
            elif not price_triggered:
                rejection_code = "BID_EDGE_BELOW_THRESHOLD"
            elif not l3_gate_passed:
                rejection_code = (
                    "L3_MARGINAL_VALUE_UNAVAILABLE"
                    if config.rolling_l3_marginal_gate and not l3_value_available
                    else "L3_MARGINAL_VALUE_INSUFFICIENT"
                )
            elif available_quantity + 1e-9 < minimum_fill_quantity:
                rejection_code = "SELL_NODE_LIMIT"
            else:
                accepted = True
                accepted_quantity = min(target_quantity, available_quantity)
        signed_quantity = accepted_quantity if order["our_side"] == "BUY" else -accepted_quantity
        delivery["P%02d" % int(order["period"])] += signed_quantity
        buy_total_value += accepted_quantity if signed_quantity > 0 else 0.0
        sell_total_value += accepted_quantity if signed_quantity < 0 else 0.0
        decisions.append(
            {
                **order,
                "accepted_quantity_mwh": round(accepted_quantity, 6),
                "accepted_fill_ratio": round(
                    accepted_quantity / quantity if quantity > 0.0 else 0.0, 6
                ),
                "accepted": accepted,
                "model_marginal_value_yuan_per_mwh": round(marginal_value, 3),
                "l3_reference_price_yuan_per_mwh": round(l3_reference_price, 3),
                "l3_net_benefit_yuan_per_mwh": round(l3_net_benefit, 3),
                "l3_gate_available": l3_gate_available,
                "l3_gate_passed": l3_gate_passed,
                "spot_forecast_price_yuan_per_mwh": round(spot_forecast, 3),
                "price_edge_yuan_per_mwh": round(price_edge, 3),
                "required_price_edge_yuan_per_mwh": round(
                    threshold, 3
                ),
                "price_triggered": price_triggered,
                "target_fill_ratio": round(target_fill_ratio, 6),
                "effective_buy_ceiling_yuan_per_mwh": round(effective_buy_ceiling, 3),
                "effective_sell_floor_yuan_per_mwh": round(effective_sell_floor, 3),
                "trigger_rule": (
                    "ASK<=SPOT_P50-EDGE"
                    if order["our_side"] == "BUY"
                    else "BID>=SPOT_P50+EDGE"
                ),
                "rejection_code": rejection_code,
                "decision_reason": (
                    "价差达到阈值且仓位空间可用，按订单量的%.1f%%部分成交"
                    % (100.0 * accepted_quantity / max(quantity, 1e-9))
                    if accepted
                    else {
                        "BUY_NODE_LIMIT": "可买空间不足订单量的10%或已达到110%仓位上限",
                        "ASK_EDGE_BELOW_THRESHOLD": "卖价未低于现货P50预测价%.0f元/MWh"
                        % threshold,
                        "SELL_DISABLED": "当前配置禁止滚撮卖出",
                        "USER_BUY_CEILING": "订单卖价超过我方滚撮买入上限价",
                        "USER_SELL_FLOOR": "订单买价低于我方滚撮卖出下限价",
                        "SELL_NODE_LIMIT": "可卖空间不足订单量的10%或已达到90%仓位下限",
                        "BID_EDGE_BELOW_THRESHOLD": "买价未高于现货P50预测价%.0f元/MWh"
                        % threshold,
                        "L3_MARGINAL_VALUE_INSUFFICIENT": "L3边际价值扣除订单价和费用后的净收益低于门槛",
                        "L3_MARGINAL_VALUE_UNAVAILABLE": "L3边际价值不可用，启用门控时不允许成交",
                    }[str(rejection_code)]
                ),
            }
        )
    net = buy_total_value - sell_total_value
    resulting = max(0.0, previous_position + net)
    if buy_total_value > 1e-5 and sell_total_value > 1e-5:
        side = "MIXED"
    elif buy_total_value > 1e-5:
        side = "BUY"
    elif sell_total_value > 1e-5:
        side = "SELL"
    else:
        side = "HOLD"
    reason = (
        "按现货P50预测价买入下限%.0f、卖出上限%.0f元/MWh捕捉订单，并成交订单量的%.0f%%-%.0f%%"
        % (
            config.price_edge_lower_yuan_per_mwh,
            config.price_edge_upper_yuan_per_mwh,
            100.0 * config.rolling_min_fill_ratio,
            100.0 * config.rolling_max_fill_ratio,
        )
        if side != "HOLD"
        else "没有订单达到现货P50预测价差阈值，或90%-110%仓位空间不足"
    )
    remaining_sell_room = sum(
        max(
            0.0,
            locked_assessment_curve[index]
            + float(delivery["P%02d" % (index + 1)])
            - lower_position[index],
        )
        for index in range(48)
    )
    return TradeAction(
        strategy="OPTIMIZED", event=event, side=side,
        quantity_mwh=round(abs(net), 3), previous_position_mwh=round(previous_position, 3),
        target_position_mwh=round(resulting, 3), resulting_position_mwh=round(resulting, 3),
        execution_price_yuan_per_mwh=0.0, fee_yuan=round((buy_total_value + sell_total_value) * quote.variable_fee, 2),
        score_yuan=round(previous_notional, 2), reason=reason,
        market_scope="DELIVERY_DAY", forecast_basis_mwh=round(sum(basis) * scope_days, 3),
        rolling_sell_cap_mwh=round(rolling_sell_used + sell_maximum, 3),
        rolling_sell_remaining_mwh=round(remaining_sell_room, 3),
        rolling_interval_limit_ratio=0.0,
        solver_backend=str(valuation.get("backend", "L3_VALUATION_MILP")),
        solver_status=str(valuation.get("status", "OPTIMAL")),
        mip_gap=valuation.get("mip_gap"),
        solver_message="L2价格阈值顺序触发并部分成交；订单接受 %d/%d；L2订单层无优化变量，边际价值来自L3"
        % (sum(d["accepted"] for d in decisions), len(decisions)),
        equivalent_delivery_days=scope_days,
        previous_daily_position_mwh=round(previous_position / scope_days, 3),
        target_daily_position_mwh=round(resulting / scope_days, 3),
        resulting_daily_position_mwh=round(resulting / scope_days, 3),
        delivery_curve=delivery, order_decisions=decisions,
        buy_quantity_mwh=round(buy_total_value, 6),
        sell_quantity_mwh=round(sell_total_value, 6),
        net_quantity_mwh=round(net, 6),
        valuation_summary={
            "source_model": "REAL_TIME_P50_EDGE_PLUS_L3_MARGINAL_GATE",
            "order_selection_method": "SEQUENTIAL_SPOT_EDGE_PARTIAL_FILL",
            "l3_gate_method": (
                "DISABLED"
                if not config.rolling_l3_marginal_gate
                else "MARGINAL_VALUE_GATE"
                if has_l3_marginal_values
                else "UNAVAILABLE_REJECT"
            ),
            "l3_marginal_gate_enabled": config.rolling_l3_marginal_gate,
            "l3_min_net_benefit_yuan_per_mwh": config.rolling_l3_min_net_benefit_yuan_per_mwh,
            "price_edge_lower_yuan_per_mwh": config.price_edge_lower_yuan_per_mwh,
            "price_edge_upper_yuan_per_mwh": config.price_edge_upper_yuan_per_mwh,
            "fill_ratio_range": [
                config.rolling_min_fill_ratio,
                config.rolling_max_fill_ratio,
            ],
            "position_ratio_range": [
                config.rolling_position_lower_ratio,
                config.rolling_position_upper_ratio,
            ],
            "pricing_lp": dict(valuation.get("pricing_lp", {})),
        },
    )
