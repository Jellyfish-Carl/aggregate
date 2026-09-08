from __future__ import annotations

from time import perf_counter
from typing import List, Mapping, Sequence

from .milp import LinearMilp, MilpSolveResult
from .scenario import (
    JOINT_CANDIDATE_COUNT,
    L3_OPTIMIZATION_SCENARIO_COUNT,
    JointTrajectoryScenario,
    cvar,
    joint_scenario_diagnostics,
    reduced_joint_trajectories,
)
from .settlement import robust_declaration_band
from .timegrid import SPOT_HOURS, SPOT_PERIODS, spot_time


def solve_l3_milp(
    snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    execution_load: Sequence[float],
    risk_lambda: float,
    physical_peak_mw: float,
    execution_day_ahead_prices: Sequence[float],
    execution_real_time_prices: Sequence[float],
    rt_period: int,
    locked: bool,
    locked_contract_curve: Sequence[float] = (),
    locked_contract_cost_yuan: float = 0.0,
    initial_soc_mwh: float = 10.0,
    locked_declaration: Sequence[float] = (),
    fixed_storage_rows: Sequence[Mapping[str, object]] = (),
    fixed_until: int = 0,
    scenario_count: int = L3_OPTIMIZATION_SCENARIO_COUNT,
) -> Mapping[str, object]:
    """L3 96-point stochastic MILP for day-ahead, real-time recourse and storage.

    Correlated load/day-ahead/spread trajectories are generated before build.
    Storage direction remains integer and shared across scenarios; real-time
    imbalance is continuous scenario recourse. This keeps each MPC solve under
    the API's ten-second service budget while preserving joint price risk.
    """

    total_started = perf_counter()
    sequences = (
        snapshot["rows"],
        price_snapshot["rows"],
        execution_load,
        execution_day_ahead_prices,
        execution_real_time_prices,
    )
    if any(len(values) != SPOT_PERIODS for values in sequences):
        raise ValueError("L3负荷、价格和执行曲线必须各有96个点")
    if locked_declaration and len(locked_declaration) != SPOT_PERIODS:
        raise ValueError("锁定日前申报必须有96个点")
    if locked_contract_curve and len(locked_contract_curve) != SPOT_PERIODS:
        raise ValueError("锁定合同曲线必须有96个点")
    if not 0 <= fixed_until <= SPOT_PERIODS:
        raise ValueError("fixed_until 必须位于 [0, 96]")
    if fixed_until and len(fixed_storage_rows) < fixed_until:
        raise ValueError("固定储能轨迹短于 fixed_until")

    scenarios = reduced_joint_trajectories(snapshot, price_snapshot, scenario_count)
    candidate_scenario_count = JOINT_CANDIDATE_COUNT
    scenario_diagnostics = joint_scenario_diagnostics(
        snapshot, price_snapshot, scenarios
    )
    probabilities = tuple(item.probability for item in scenarios)
    capacity = 20.0
    minimum_soc = 2.0
    initial_soc = float(initial_soc_mwh)
    efficiency = 0.92
    storage_power_mw = 5.0
    max_energy = storage_power_mw * SPOT_HOURS
    degradation_cost = 2.0
    spot_trade_friction = 0.01
    physical_upper = physical_peak_mw * SPOT_HOURS
    band_slack_penalty = 10000.0
    contract_curve = (
        [float(value) for value in locked_contract_curve]
        if locked_contract_curve
        else [0.0] * SPOT_PERIODS
    )
    contract_fixed_cost = float(locked_contract_cost_yuan)

    model = LinearMilp("L3-96-POINT-JOINT-SCENARIO-MPC")
    declaration_names: List[str] = []
    day_ahead_buy_names: List[str] = []
    day_ahead_sell_names: List[str] = []
    charge_names: List[str] = []
    discharge_names: List[str] = []
    soc_names: List[str] = []
    slack_lower_names: List[str] = []
    slack_upper_names: List[str] = []
    declaration_bounds: List[tuple] = []
    contract_balance_rows: List[int] = []

    for index, row in enumerate(snapshot["rows"]):
        p10 = float(row["p10_mwh"])
        p90 = float(row["p90_mwh"])
        lower, upper = robust_declaration_band(p10, p90, physical_upper)
        declaration_bounds.append((lower, upper))
        declaration = model.add_var("declaration_%02d" % index, upper=physical_upper)
        slack_lower = model.add_var(
            "declaration_slack_lower_%02d" % index,
            upper=max(physical_upper, lower, p90),
        )
        slack_upper = model.add_var(
            "declaration_slack_upper_%02d" % index,
            upper=max(physical_upper, lower, p90),
        )
        model.add_constraint({declaration: 1.0, slack_lower: 1.0}, lower=lower)
        model.add_constraint({declaration: 1.0, slack_upper: -1.0}, upper=upper)
        model.add_to_objective(slack_lower, band_slack_penalty)
        model.add_to_objective(slack_upper, band_slack_penalty)
        if locked_declaration:
            fixed = float(locked_declaration[index])
            model.add_constraint({declaration: 1.0}, lower=fixed, upper=fixed)

        day_ahead_cap = physical_upper + abs(contract_curve[index])
        day_ahead_buy = model.add_var(
            "day_ahead_buy_%02d" % index, upper=day_ahead_cap
        )
        day_ahead_sell = model.add_var(
            "day_ahead_sell_%02d" % index, upper=day_ahead_cap
        )
        contract_balance_rows.append(
            model.add_constraint(
                {
                    declaration: 1.0,
                    day_ahead_buy: -1.0,
                    day_ahead_sell: 1.0,
                },
                lower=contract_curve[index],
                upper=contract_curve[index],
            )
        )

        charge = model.add_var("charge_%02d" % index, upper=max_energy)
        discharge = model.add_var("discharge_%02d" % index, upper=max_energy)
        use_charge = model.add_var("use_charge_%02d" % index, upper=1.0, integer=True)
        use_discharge = model.add_var("use_discharge_%02d" % index, upper=1.0, integer=True)
        soc = model.add_var("soc_%02d" % index, lower=minimum_soc, upper=capacity)
        model.add_constraint({charge: 1.0, use_charge: -max_energy}, upper=0.0)
        model.add_constraint({discharge: 1.0, use_discharge: -max_energy}, upper=0.0)
        model.add_constraint({use_charge: 1.0, use_discharge: 1.0}, upper=1.0)
        if index < fixed_until:
            fixed_row = fixed_storage_rows[index]
            fixed_charge = float(fixed_row["charge_from_grid_mwh"])
            fixed_discharge = float(fixed_row["discharge_to_load_mwh"])
            model.add_constraint({charge: 1.0}, lower=fixed_charge, upper=fixed_charge)
            model.add_constraint({discharge: 1.0}, lower=fixed_discharge, upper=fixed_discharge)
        balance = {soc: 1.0, charge: -efficiency, discharge: 1.0 / efficiency}
        if index:
            balance[soc_names[index - 1]] = -1.0
            rhs = 0.0
        else:
            rhs = initial_soc
        model.add_constraint(balance, lower=rhs, upper=rhs)

        declaration_names.append(declaration)
        day_ahead_buy_names.append(day_ahead_buy)
        day_ahead_sell_names.append(day_ahead_sell)
        slack_lower_names.append(slack_lower)
        slack_upper_names.append(slack_upper)
        charge_names.append(charge)
        discharge_names.append(discharge)
        soc_names.append(soc)
    # Close the daily MPC horizon so the optimizer cannot create artificial
    # value by ending with more stored energy than it started with.
    model.add_constraint(
        {soc_names[-1]: 1.0}, lower=initial_soc, upper=initial_soc
    )

    model.add_var("cvar_eta", lower=-1e8, upper=1e8)
    scenario_loss_names: List[str] = []
    scenario_rt_buy_names: List[List[str]] = []
    scenario_rt_sell_names: List[List[str]] = []
    for scenario_index, scenario in enumerate(scenarios):
        probability = scenario.probability
        loss = model.add_var("loss_%02d" % scenario_index, lower=-1e8, upper=1e8)
        excess = model.add_var("cvar_excess_%02d" % scenario_index, upper=1e8)
        loss_equation = {loss: 1.0}
        rt_buy_names: List[str] = []
        rt_sell_names: List[str] = []
        for index in range(SPOT_PERIODS):
            load = scenario.load_mwh[index]
            da = scenario.day_ahead_price[index]
            rt = scenario.real_time_price[index]
            rt_cap = physical_upper + max_energy
            rt_buy = model.add_var("rt_buy_%02d_%02d" % (scenario_index, index), upper=rt_cap)
            rt_sell = model.add_var("rt_sell_%02d_%02d" % (scenario_index, index), upper=rt_cap)
            # Future periods must remain inside the 10% spot-deviation band.
            # Realized prefix violations are assessed sunk costs and must not
            # make the remaining MPC horizon infeasible.
            representative_execution = scenario.scenario_id == "L50_DA50_SP50"
            excess_upper = (
                rt_cap
                if index < fixed_until or (not locked and not representative_execution)
                else 0.0
            )
            excess_buy = model.add_var(
                "excess_buy_%02d_%02d" % (scenario_index, index),
                upper=excess_upper,
            )
            excess_sell = model.add_var(
                "excess_sell_%02d_%02d" % (scenario_index, index),
                upper=excess_upper,
            )
            model.add_constraint(
                {
                    declaration_names[index]: 1.0,
                    rt_buy: 1.0,
                    rt_sell: -1.0,
                    charge_names[index]: -1.0,
                    discharge_names[index]: 1.0,
                },
                lower=load,
                upper=load,
            )
            deviation_band = 0.10 * load
            # Assess the net real-time trade against the period's load. Buy
            # and sell are two settlement directions of one net deviation.
            model.add_constraint(
                {
                    excess_buy: 1.0,
                    rt_buy: -1.0,
                    rt_sell: 1.0,
                },
                lower=-deviation_band,
            )
            model.add_constraint(
                {
                    excess_sell: 1.0,
                    rt_buy: 1.0,
                    rt_sell: -1.0,
                },
                lower=-deviation_band,
            )
            buy_recovery = 1.05 * max(da - rt, 0.0)
            sell_recovery = 1.05 * max(rt - da, 0.0)
            loss_equation[day_ahead_buy_names[index]] = -(da + spot_trade_friction)
            loss_equation[day_ahead_sell_names[index]] = da - spot_trade_friction
            loss_equation[rt_buy] = -(rt + spot_trade_friction)
            loss_equation[rt_sell] = rt - spot_trade_friction
            loss_equation[excess_buy] = -buy_recovery
            loss_equation[excess_sell] = -sell_recovery
            loss_equation[charge_names[index]] = -degradation_cost
            loss_equation[discharge_names[index]] = -degradation_cost
            rt_buy_names.append(rt_buy)
            rt_sell_names.append(rt_sell)
        model.add_constraint(loss_equation, lower=0.0, upper=0.0)
        model.add_constraint({loss: 1.0, "cvar_eta": -1.0, excess: -1.0}, upper=0.0)
        model.add_to_objective(loss, (1.0 - risk_lambda) * probability)
        if risk_lambda:
            model.add_to_objective(excess, risk_lambda * probability / 0.05)
        scenario_loss_names.append(loss)
        scenario_rt_buy_names.append(rt_buy_names)
        scenario_rt_sell_names.append(rt_sell_names)
    if risk_lambda:
        model.add_to_objective("cvar_eta", risk_lambda)

    solved = model.solve(time_limit_seconds=9.0, mip_relative_gap=1e-3)
    pricing = model.solve_fixed_integer_lp(solved.values, time_limit_seconds=9.0)
    marginal_value_96 = [
        -float(pricing.row_marginals[row_index])
        for row_index in contract_balance_rows
    ]
    half_hour_marginal_values = [
        0.5 * (marginal_value_96[index] + marginal_value_96[index + 1])
        for index in range(0, SPOT_PERIODS, 2)
    ]
    representative_index = next(
        (index for index, item in enumerate(scenarios) if item.scenario_id == "L50_DA50_SP50"),
        0,
    )
    declaration = _declaration_payload(
        snapshot,
        price_snapshot,
        declaration_names,
        declaration_bounds,
        slack_lower_names,
        slack_upper_names,
        solved,
        locked,
        contract_curve,
        day_ahead_buy_names,
        day_ahead_sell_names,
        len(scenarios),
    )
    storage = _storage_payload(
        execution_load,
        declaration,
        charge_names,
        discharge_names,
        soc_names,
        scenario_rt_buy_names[representative_index],
        scenario_rt_sell_names[representative_index],
        solved,
        execution_day_ahead_prices,
        execution_real_time_prices,
        rt_period,
        capacity,
        minimum_soc,
        initial_soc,
        efficiency,
        max_energy,
        contract_curve,
    )
    slack_penalty = band_slack_penalty * sum(
        solved.values[name] for name in slack_lower_names + slack_upper_names
    )
    scenario_costs = [
        solved.values[name]
        + contract_fixed_cost
        + slack_penalty
        for name in scenario_loss_names
    ]
    expected = sum(probability * cost for probability, cost in zip(probabilities, scenario_costs))
    result = {
        "declaration": declaration,
        "storage": storage,
        "execution_ledger": {
            "scenario": scenarios[representative_index].scenario_id,
            "fixed_until": fixed_until,
            "rows": storage["rows"],
        },
        "optimization": _optimization_payload(
            solved,
            scenarios,
            scenario_costs,
            expected,
            contract_fixed_cost,
            slack_penalty,
            perf_counter() - total_started,
            candidate_scenario_count,
            scenario_diagnostics,
            pricing,
            marginal_value_96,
            half_hour_marginal_values,
        ),
    }
    return result


def _declaration_payload(
    snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    names: Sequence[str],
    bounds: Sequence[tuple],
    slack_lower_names: Sequence[str],
    slack_upper_names: Sequence[str],
    solved: MilpSolveResult,
    locked: bool,
    contract_curve: Sequence[float],
    day_ahead_buy_names: Sequence[str],
    day_ahead_sell_names: Sequence[str],
    scenario_count: int,
) -> Mapping[str, object]:
    rows = []
    for index, row in enumerate(snapshot["rows"]):
        price_row = price_snapshot["rows"][index]
        declared = max(0.0, solved.values[names[index]])
        lower, upper = bounds[index]
        da = float(price_row["day_ahead_p50"])
        rt = float(price_row["real_time_p50"])
        day_ahead_buy = max(0.0, solved.values[day_ahead_buy_names[index]])
        day_ahead_sell = max(0.0, solved.values[day_ahead_sell_names[index]])
        rows.append(
            {
                "period": index + 1,
                "time": spot_time(index + 1),
                "p10_mwh": round(float(row["p10_mwh"]), 4),
                "p50_mwh": round(float(row["p50_mwh"]), 4),
                "p90_mwh": round(float(row["p90_mwh"]), 4),
                "lower_mwh": round(lower, 4),
                "upper_mwh": round(upper, 4),
                "band_slack_lower_mwh": round(solved.values[slack_lower_names[index]], 6),
                "band_slack_upper_mwh": round(solved.values[slack_upper_names[index]], 6),
                "declared_mwh": round(declared, 4),
                "locked_contract_mwh": round(float(contract_curve[index]), 4),
                "day_ahead_buy_mwh": round(day_ahead_buy, 6),
                "day_ahead_sell_mwh": round(day_ahead_sell, 6),
                "day_ahead_net_mwh": round(day_ahead_buy - day_ahead_sell, 6),
                "day_ahead_price_yuan_per_mwh": round(da, 3),
                "real_time_price_forecast_yuan_per_mwh": round(rt, 3),
                "price_spread_yuan_per_mwh": round(rt - da, 3),
                "price_reason": "由联合负荷、日前价格和实时价差场景的期望成本与CVaR共同确定",
            }
        )
    return {
        "status": "LOCKED" if locked else "CONDITIONAL_PLAN",
        "deadline": "D-1 日前市场关闸（17:30真实日前价格公布前）",
        "period_count": SPOT_PERIODS,
        "interval_minutes": 15,
        "total_mwh": round(sum(row["declared_mwh"] for row in rows), 3),
        "locked_contract_total_mwh": round(
            sum(row["locked_contract_mwh"] for row in rows), 3
        ),
        "day_ahead_buy_total_mwh": round(
            sum(row["day_ahead_buy_mwh"] for row in rows), 3
        ),
        "day_ahead_sell_total_mwh": round(
            sum(row["day_ahead_sell_mwh"] for row in rows), 3
        ),
        "total_band_slack_mwh": round(
            sum(row["band_slack_lower_mwh"] + row["band_slack_upper_mwh"] for row in rows),
            6,
        ),
        "price_aware": True,
        "objective_note": "%d条96点相关联合轨迹随机MILP；日前价、实时价差和负荷均为场景量"
        % scenario_count,
        "rows": rows,
    }


def _storage_payload(
    execution_load: Sequence[float],
    declaration: Mapping[str, object],
    charge_names: Sequence[str],
    discharge_names: Sequence[str],
    soc_names: Sequence[str],
    rt_buy_names: Sequence[str],
    rt_sell_names: Sequence[str],
    solved: MilpSolveResult,
    day_ahead_prices: Sequence[float],
    real_time_prices: Sequence[float],
    rt_period: int,
    capacity: float,
    minimum_soc: float,
    initial_soc: float,
    efficiency: float,
    max_energy: float,
    contract_curve: Sequence[float],
) -> Mapping[str, object]:
    rows = []
    soc_start = initial_soc
    for index, load in enumerate(execution_load):
        charge = max(0.0, solved.values[charge_names[index]])
        discharge = max(0.0, solved.values[discharge_names[index]])
        soc = max(minimum_soc, min(capacity, solved.values[soc_names[index]]))
        # Reconcile the representative execution path exactly. Scenario
        # recourse variables are retained for diagnostics, but the public
        # execution ledger must balance the selected forecast/actual path.
        grid_load = float(load) + charge - discharge
        declared = float(declaration["rows"][index]["declared_mwh"])
        rt_net = grid_load - declared
        rt_buy = max(rt_net, 0.0)
        rt_sell = max(-rt_net, 0.0)
        deviation_limit = 0.10 * float(load)
        deviation_ratio = abs(rt_net) / max(float(load), 1e-9)
        mode = "CHARGE" if charge > 1e-5 else "DISCHARGE" if discharge > 1e-5 else "IDLE"
        rows.append(
            {
                "period": index + 1,
                "time": spot_time(index + 1),
                "actual_load_mwh": round(float(load), 4),
                "declared_mwh": round(declared, 4),
                "locked_contract_mwh": round(float(contract_curve[index]), 4),
                "day_ahead_buy_mwh": declaration["rows"][index]["day_ahead_buy_mwh"],
                "day_ahead_sell_mwh": declaration["rows"][index]["day_ahead_sell_mwh"],
                "day_ahead_price": round(float(day_ahead_prices[index]), 3),
                "real_time_price": round(float(real_time_prices[index]), 3),
                "mode": mode,
                "storage_action_mwh": round(discharge - charge, 4),
                # These execution values are fixed by later MPC solves, so
                # display-level rounding must not alter the persisted state.
                "charge_from_grid_mwh": round(charge, 12),
                "charge_stored_mwh": round(charge * efficiency, 4),
                "discharge_to_load_mwh": round(discharge, 12),
                "discharge_from_soc_mwh": round(discharge / efficiency, 4),
                "grid_load_after_storage_mwh": round(grid_load, 4),
                "real_time_buy_mwh": round(rt_buy, 6),
                "real_time_sell_mwh": round(rt_sell, 6),
                "real_time_net_mwh": round(rt_net, 6),
                "spot_deviation_abs_mwh": round(abs(rt_net), 6),
                "spot_deviation_limit_mwh": round(deviation_limit, 6),
                "spot_deviation_ratio": round(deviation_ratio, 6),
                "spot_deviation_compliant": abs(rt_net) <= deviation_limit + 1e-7,
                "settled_supply_mwh": round(declared + rt_buy - rt_sell, 6),
                "balance_residual_mwh": round(grid_load - declared - rt_buy + rt_sell, 9),
                "soc_start_mwh": round(soc_start, 4),
                "soc_mwh": round(soc, 12),
                "scenario_rt_buy_mwh": round(max(0.0, solved.values[rt_buy_names[index]]), 6),
                "scenario_rt_sell_mwh": round(max(0.0, solved.values[rt_sell_names[index]]), 6),
            }
        )
        soc_start = soc
    charged = sum(row["charge_from_grid_mwh"] for row in rows)
    discharged = sum(row["discharge_to_load_mwh"] for row in rows)
    charge_cost = sum(row["charge_from_grid_mwh"] * row["real_time_price"] for row in rows)
    discharge_value = sum(row["discharge_to_load_mwh"] * row["real_time_price"] for row in rows)
    average_charge = charge_cost / max(charged, 1e-9)
    average_discharge = discharge_value / max(discharged, 1e-9)
    matched_discharge = min(discharged, charged * efficiency * efficiency)
    matched_charge = matched_discharge / (efficiency * efficiency) if matched_discharge else 0.0
    current_index = max(1, min(SPOT_PERIODS, rt_period)) - 1
    return {
        "capacity_mwh": capacity,
        "power_mw": max_energy / SPOT_HOURS,
        "efficiency": efficiency,
        "interval_minutes": 15,
        "charged_mwh": round(charged, 3),
        "discharged_mwh": round(discharged, 3),
        "gross_arbitrage_value_yuan": round(
            matched_discharge * average_discharge - matched_charge * average_charge, 2
        ),
        "average_charge_price_yuan_per_mwh": round(average_charge, 2),
        "average_discharge_price_yuan_per_mwh": round(average_discharge, 2),
        "charge_periods": sum(row["mode"] == "CHARGE" for row in rows),
        "discharge_periods": sum(row["mode"] == "DISCHARGE" for row in rows),
        "current": rows[current_index],
        "solver_backend": solved.backend,
        "solver_status": solved.status,
        "mip_gap": solved.mip_gap,
        "rows": rows,
    }


def _optimization_payload(
    solved: MilpSolveResult,
    scenarios: Sequence[JointTrajectoryScenario],
    scenario_costs: Sequence[float],
    expected_cost: float,
    contract_fixed_cost: float,
    slack_penalty: float,
    total_seconds: float,
    candidate_scenario_count: int,
    scenario_diagnostics: Mapping[str, object],
    pricing: object,
    marginal_value_96: Sequence[float],
    half_hour_marginal_values: Sequence[float],
) -> Mapping[str, object]:
    probabilities = tuple(item.probability for item in scenarios)
    return {
        "layer": "L3",
        "model_structure": "96_POINT_JOINT_SCENARIO_L3_MILP",
        "candidate_scenario_count": candidate_scenario_count,
        "optimization_scenario_count": len(scenarios),
        "scenario_generation": dict(scenario_diagnostics),
        "scenario_catalog": [
            {
                "scenario_id": item.scenario_id,
                "probability": round(item.probability, 8),
                "load_state": item.load_quantile,
                "day_ahead_state": item.day_ahead_quantile,
                "spread_state": item.spread_quantile,
            }
            for item in scenarios
        ],
        "milp_executed": True,
        "backend": solved.backend,
        "status": solved.status,
        "mip_gap": solved.mip_gap,
        "node_count": solved.node_count,
        "solve_seconds": round(solved.solve_seconds, 4),
        "end_to_end_seconds": round(total_seconds, 4),
        "time_limit_seconds": solved.time_limit_seconds,
        "service_budget_seconds": 10.0,
        "within_service_budget": total_seconds <= 10.0,
        "objective_yuan": round(
            solved.objective + contract_fixed_cost, 2
        ),
        "expected_cost_yuan": round(expected_cost, 2),
        "cvar95_yuan": round(cvar(scenario_costs, probabilities, 0.95), 2),
        "scenario_costs_yuan": [round(value, 2) for value in scenario_costs],
        "locked_contract_cost_yuan": round(contract_fixed_cost, 2),
        "declaration_slack_penalty_yuan": round(slack_penalty, 2),
        "objective_definition": (
            "L3 = 锁定中长期合同成本 + 日前净买卖成本 + 实时偏差补救成本 "
            "+ 储能退化成本 + 声明带松弛惩罚，并按期望成本/CVaR加权"
        ),
        "constraint_definition": {
            "declaration_band": "日前申报位于负荷P10-P90物理带内，越界使用高额松弛变量",
            "contract_balance": "日前申报 + 日前买入 - 日前卖出 = 已锁定中长期合同曲线",
            "spot_energy_balance": "日前申报 + 实时买入 - 实时卖出 - 充电 + 放电 = 场景负荷",
            "execution_deviation_band": "代表性执行路径的实时净买卖绝对值 <= 实际负荷的10%",
            "scenario_deviation_recourse": "非代表性场景允许超出10%的补救量，并计入偏差回收成本",
            "storage_soc": "SOC按充电效率和放电效率递推，范围为2-20MWh",
            "storage_mode": "充放电二进制互斥，单点功率上限为5MW*15分钟",
            "terminal_soc": "日末SOC回到初始SOC",
            "realtime_lock": "实时阶段锁定已执行前缀，仅重算未实现区间",
        },
        "variable_count": solved.variable_count,
        "binary_count": solved.binary_count,
        "constraint_count": solved.constraint_count,
        "message": solved.message,
        "pricing_lp": {
            "backend": pricing.backend,
            "status": pricing.status,
            "solve_seconds": round(pricing.solve_seconds, 4),
            "interpretation": "固定MILP储能整数模式后的局部LP边际价值",
        },
        "contract_marginal_value_yuan_per_mwh": [
            round(value, 6) for value in marginal_value_96
        ],
        "rolling_half_hour_marginal_value_yuan_per_mwh": [
            round(value, 6) for value in half_hour_marginal_values
        ],
    }
