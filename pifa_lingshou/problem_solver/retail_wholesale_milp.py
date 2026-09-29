from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

from pifa_lingshou.problem_solver.milp import LinearMilp, MilpBackendUnavailable, MilpSolveError

from ..data_objects.model import AggregateInput, PERIODS, RetailSettlement
from ..accounting.retail import settle_locked_mix, summarize_distribution, weighted_mean, weighted_quantile


def _name(stem: str, *indices: int) -> str:
    return stem + "_" + "_".join(str(index) for index in indices)


def _weighted_period_quantile(inputs: AggregateInput, period: int, q: float) -> float:
    values = []
    probabilities = []
    for scenario in inputs.scenarios:
        total = sum(float(scenario.customer_load_mwh[item.customer_id][period]) for item in inputs.customers)
        values.append(total)
        probabilities.append(float(scenario.probability))
    return weighted_quantile(values, probabilities, q)


def _aggregate_load(inputs: AggregateInput, scenario_index: int, period: int) -> float:
    state = inputs.locked_state
    if period < state.fixed_until and state.actual_aggregate_load_mwh:
        return float(state.actual_aggregate_load_mwh[period])
    scenario = inputs.scenarios[scenario_index]
    return sum(float(scenario.customer_load_mwh[customer.customer_id][period]) for customer in inputs.customers)


def _retail_settlements(inputs: AggregateInput, package: str) -> Tuple[RetailSettlement, ...]:
    return tuple(settle_locked_mix(inputs, package, index) for index in range(len(inputs.scenarios)))


def _package_matrix(inputs: AggregateInput, package: str) -> Mapping[str, Mapping[str, int]]:
    return {
        customer.customer_id: {
            candidate: int(
                inputs.locked_state.locked_packages.get(customer.customer_id, package) == candidate
            )
            for candidate in ("F", "L", "S")
        }
        for customer in inputs.customers
    }


def _bill_precheck(inputs: AggregateInput, settlements: Sequence[RetailSettlement]) -> Dict[str, float]:
    probabilities = [float(item.probability) for item in inputs.scenarios]
    violations = {}
    for customer in inputs.customers:
        limit = inputs.customer_bill_limits_yuan.get(
            customer.customer_id,
            customer.max_expected_bill_yuan,
        )
        if limit is None:
            continue
        expected = weighted_mean(
            [settlement.customer_bills_yuan[customer.customer_id] for settlement in settlements],
            probabilities,
        )
        if expected > float(limit) + 1e-6:
            violations[customer.customer_id] = expected - float(limit)
    return violations


def _safe_bounds(inputs: AggregateInput, settlements: Sequence[RetailSettlement], trade_cap: float) -> float:
    price_scale = max(
        [1.0]
        + [abs(float(value)) for scenario in inputs.scenarios for value in scenario.day_ahead_price]
        + [abs(float(value)) for scenario in inputs.scenarios for value in scenario.real_time_price]
        + [abs(float(value)) for product in inputs.contract_products for value in product.buy_price]
        + [abs(float(value)) for product in inputs.contract_products for value in product.sell_price]
    )
    revenue = max((abs(item.retail_revenue_yuan) for item in settlements), default=0.0)
    contract_scale = sum(
        max(product.buy_limit_mwh) * max(abs(value) for value in product.buy_price)
        + max(product.sell_limit_mwh) * max(abs(value) for value in product.sell_price)
        for product in inputs.contract_products
    ) * 48.0
    return max(
        100000.0,
        revenue + abs(inputs.old_contract_cost_yuan) + contract_scale
        + PERIODS * (trade_cap * price_scale * 3.0 + inputs.deviation_buy_penalty * trade_cap
                     + inputs.deviation_sell_penalty * trade_cap)
        + 10000.0,
    )


def solve_candidate_milp(inputs: AggregateInput, package: str, risk_lambda: float) -> Mapping[str, object]:
    """Optimize one fixed retail package and one risk preference independently."""

    inputs.validate()
    if package not in {"F", "L", "S"}:
        raise ValueError("package 必须是 F、L 或 S")
    if not 0.0 <= risk_lambda <= 1.0:
        raise ValueError("risk_lambda 必须位于 [0, 1]")
    settlements = _retail_settlements(inputs, package)
    probabilities = [float(item.probability) for item in inputs.scenarios]
    bill_violations = _bill_precheck(inputs, settlements)
    if bill_violations:
        return {
            "package": package,
            "risk_lambda": risk_lambda,
            "status": "INFEASIBLE_PRECHECK",
            "reason": "客户期望账单超过上限",
            "customer_bill_violations_yuan": bill_violations,
        }

    physical_upper = float(inputs.physical_peak_mw) * 0.25
    contract_excursion = max(
        sum(max(product.buy_limit_mwh[h], product.sell_limit_mwh[h]) for product in inputs.contract_products)
        for h in range(48)
    ) * 0.5
    storage = inputs.storage
    old_position_excursion = max(
        max(abs(float(value)) for value in inputs.old_contract_position_mwh),
        max(abs(float(value)) for value in inputs.old_annual_position_mwh),
    ) * 0.5
    contract_curve_bound = old_position_excursion + contract_excursion
    maximum_scenario_load = max(
        sum(float(scenario.customer_load_mwh[customer.customer_id][period]) for customer in inputs.customers)
        for scenario in inputs.scenarios
        for period in range(PERIODS)
    )
    if inputs.locked_state.actual_aggregate_load_mwh:
        maximum_scenario_load = max(
            maximum_scenario_load,
            max(float(value) for value in inputs.locked_state.actual_aggregate_load_mwh),
        )
    trade_cap = max(
        physical_upper + contract_curve_bound + storage.maximum_charge_mwh + storage.maximum_discharge_mwh,
        contract_curve_bound + storage.maximum_charge_mwh + storage.maximum_discharge_mwh,
        maximum_scenario_load + storage.maximum_charge_mwh + storage.maximum_discharge_mwh,
        1.0,
    )
    cost_bound = _safe_bounds(inputs, settlements, trade_cap)
    model = LinearMilp("PIFA-LINGSHOU-%s-LAMBDA-%.3f" % (package, risk_lambda))

    product_ids = [item.product_id for item in inputs.contract_products]
    annual_product_index = product_ids.index(inputs.annual_product_id)
    buy_names = {}
    sell_names = {}
    position_names = {}
    annual_position_names = {}
    contract_curve_names = []
    for product_index, product in enumerate(inputs.contract_products):
        for half_hour in range(48):
            buy_name = _name("contract_buy", product_index, half_hour)
            sell_name = _name("contract_sell", product_index, half_hour)
            buy_names[product_index, half_hour] = model.add_var(
                buy_name, upper=float(product.buy_limit_mwh[half_hour])
            )
            sell_names[product_index, half_hour] = model.add_var(
                sell_name, upper=float(product.sell_limit_mwh[half_hour])
            )
    for half_hour in range(48):
        maximum_buy = sum(float(product.buy_limit_mwh[half_hour]) for product in inputs.contract_products)
        maximum_sell = sum(float(product.sell_limit_mwh[half_hour]) for product in inputs.contract_products)
        old = float(inputs.old_contract_position_mwh[half_hour])
        lower, upper = old - maximum_sell, old + maximum_buy
        position = model.add_var(_name("position", half_hour), lower=lower, upper=upper)
        position_names[half_hour] = position
        coefficients = {position: 1.0}
        for product_index in range(len(inputs.contract_products)):
            coefficients[buy_names[product_index, half_hour]] = -1.0
            coefficients[sell_names[product_index, half_hour]] = 1.0
        model.add_constraint(coefficients, lower=old, upper=old)

        annual_product = inputs.contract_products[annual_product_index]
        ann_old = float(inputs.old_annual_position_mwh[half_hour])
        ann_lower = ann_old - float(annual_product.sell_limit_mwh[half_hour])
        ann_upper = ann_old + float(annual_product.buy_limit_mwh[half_hour])
        annual_position = model.add_var(_name("annual_position", half_hour), lower=ann_lower, upper=ann_upper)
        annual_position_names[half_hour] = annual_position
        model.add_constraint(
            {
                annual_position: 1.0,
                buy_names[annual_product_index, half_hour]: -1.0,
                sell_names[annual_product_index, half_hour]: 1.0,
            },
            lower=ann_old,
            upper=ann_old,
        )
        monthly_demand = float(inputs.monthly_delivery_mwh[half_hour])
        model.add_constraint(
            {annual_position: 1.0},
            lower=inputs.annual_coverage_minimum * monthly_demand,
        )
        model.add_constraint(
            {position: 1.0},
            lower=inputs.overall_coverage_minimum * monthly_demand,
            upper=inputs.overall_coverage_maximum * monthly_demand,
        )

    for period in range(PERIODS):
        curve = model.add_var(_name("contract_curve", period), lower=-trade_cap, upper=trade_cap)
        contract_curve_names.append(curve)
        model.add_constraint(
            {curve: 1.0, position_names[period // 2]: -0.5},
            lower=0.0,
            upper=0.0,
        )

    contract_cost_name = model.add_var("contract_cost", lower=-cost_bound, upper=cost_bound)
    contract_cost_equation = {contract_cost_name: 1.0}
    for product_index, product in enumerate(inputs.contract_products):
        for half_hour in range(48):
            contract_cost_equation[buy_names[product_index, half_hour]] = -float(product.buy_price[half_hour])
            contract_cost_equation[sell_names[product_index, half_hour]] = float(product.sell_price[half_hour])
    model.add_constraint(
        contract_cost_equation,
        lower=float(inputs.old_contract_cost_yuan),
        upper=float(inputs.old_contract_cost_yuan),
    )

    p10 = [float(value) for value in inputs.declaration_p10_mwh] if inputs.declaration_p10_mwh is not None else [
        _weighted_period_quantile(inputs, period, 0.10) for period in range(PERIODS)
    ]
    p90 = [float(value) for value in inputs.declaration_p90_mwh] if inputs.declaration_p90_mwh is not None else [
        _weighted_period_quantile(inputs, period, 0.90) for period in range(PERIODS)
    ]
    declarations = []
    slack_lower_names = []
    slack_upper_names = []
    da_buy_names = []
    da_sell_names = []
    charge_names = []
    discharge_names = []
    soc_names = []
    charge_mode_names = []
    discharge_mode_names = []
    state = inputs.locked_state
    for period in range(PERIODS):
        declaration = model.add_var(_name("declaration", period), upper=physical_upper)
        slack_lower = model.add_var(_name("band_slack_lower", period), upper=max(physical_upper, p10[period], p90[period]))
        slack_upper = model.add_var(_name("band_slack_upper", period), upper=max(physical_upper, p10[period], p90[period]))
        model.add_constraint({declaration: 1.0, slack_lower: 1.0}, lower=p10[period])
        model.add_constraint({declaration: 1.0, slack_upper: -1.0}, upper=min(physical_upper, p90[period]))
        model.add_to_objective(slack_lower, inputs.declaration_slack_penalty_yuan_per_mwh)
        model.add_to_objective(slack_upper, inputs.declaration_slack_penalty_yuan_per_mwh)
        if period in state.declaration_mwh:
            locked = float(state.declaration_mwh[period])
            model.add_constraint({declaration: 1.0}, lower=locked, upper=locked)
        declarations.append(declaration)
        slack_lower_names.append(slack_lower)
        slack_upper_names.append(slack_upper)

        da_buy = model.add_var(_name("day_ahead_buy", period), upper=trade_cap)
        da_sell = model.add_var(_name("day_ahead_sell", period), upper=trade_cap)
        model.add_constraint(
            {declaration: 1.0, da_buy: -1.0, da_sell: 1.0, contract_curve_names[period]: -1.0},
            lower=0.0,
            upper=0.0,
        )
        da_buy_names.append(da_buy)
        da_sell_names.append(da_sell)

        charge = model.add_var(_name("charge", period), upper=storage.maximum_charge_mwh)
        discharge = model.add_var(_name("discharge", period), upper=storage.maximum_discharge_mwh)
        charge_mode = model.add_var(_name("charge_mode", period), upper=1.0, integer=True)
        discharge_mode = model.add_var(_name("discharge_mode", period), upper=1.0, integer=True)
        model.add_constraint({charge: 1.0, charge_mode: -storage.maximum_charge_mwh}, upper=0.0)
        model.add_constraint({discharge: 1.0, discharge_mode: -storage.maximum_discharge_mwh}, upper=0.0)
        model.add_constraint({charge_mode: 1.0, discharge_mode: 1.0}, upper=1.0)
        soc = model.add_var(_name("soc", period), lower=storage.minimum_soc_mwh, upper=storage.maximum_soc_mwh)
        soc_equation = {soc: 1.0, charge: -storage.efficiency, discharge: 1.0 / storage.efficiency}
        if period:
            soc_equation[soc_names[-1]] = -1.0
            rhs = 0.0
        else:
            rhs = storage.initial_soc_mwh
        model.add_constraint(soc_equation, lower=rhs, upper=rhs)
        charge_names.append(charge)
        discharge_names.append(discharge)
        charge_mode_names.append(charge_mode)
        discharge_mode_names.append(discharge_mode)
        soc_names.append(soc)

        if period < state.fixed_until:
            for name, mapping in ((charge, state.charge_mwh), (discharge, state.discharge_mwh), (soc, state.soc_mwh)):
                if period in mapping:
                    locked = float(mapping[period])
                    model.add_constraint({name: 1.0}, lower=locked, upper=locked)
        if period == PERIODS - 1:
            model.add_constraint({soc: 1.0}, lower=storage.initial_soc_mwh, upper=storage.initial_soc_mwh)

    wholesale_cost_names = []
    loss_names = []
    rt_buy_names = {}
    rt_sell_names = {}
    deviation_buy_names = {}
    deviation_sell_names = {}
    for scenario_index, scenario in enumerate(inputs.scenarios):
        wholesale_cost = model.add_var(_name("wholesale_cost", scenario_index), lower=-cost_bound, upper=cost_bound)
        loss = model.add_var(
            _name("net_loss", scenario_index),
            lower=-cost_bound,
            upper=cost_bound,
            objective=(1.0 - risk_lambda) * probabilities[scenario_index],
        )
        cost_equation = {wholesale_cost: 1.0, contract_cost_name: -1.0}
        for period in range(PERIODS):
            da_buy = da_buy_names[period]
            da_sell = da_sell_names[period]
            rt_buy = model.add_var(_name("real_time_buy", scenario_index, period), upper=trade_cap)
            rt_sell = model.add_var(_name("real_time_sell", scenario_index, period), upper=trade_cap)
            rt_buy_names[scenario_index, period] = rt_buy
            rt_sell_names[scenario_index, period] = rt_sell
            fixed_actual_buy = state.real_time_buy_mwh.get(period)
            fixed_actual_sell = state.real_time_sell_mwh.get(period)
            if period < state.fixed_until:
                if fixed_actual_buy is not None:
                    model.add_constraint({rt_buy: 1.0}, lower=float(fixed_actual_buy), upper=float(fixed_actual_buy))
                if fixed_actual_sell is not None:
                    model.add_constraint({rt_sell: 1.0}, lower=float(fixed_actual_sell), upper=float(fixed_actual_sell))

            load = _aggregate_load(inputs, scenario_index, period)
            balance = {
                declarations[period]: 1.0,
                rt_buy: 1.0,
                rt_sell: -1.0,
                charge_names[period]: -1.0,
                discharge_names[period]: 1.0,
            }
            model.add_constraint(balance, lower=load, upper=load)

            buy_slack_limit = (
                float(inputs.deviation_buy_slack_limit_mwh[period])
                if inputs.deviation_buy_slack_limit_mwh is not None
                else trade_cap
            )
            sell_slack_limit = (
                float(inputs.deviation_sell_slack_limit_mwh[period])
                if inputs.deviation_sell_slack_limit_mwh is not None
                else trade_cap
            )
            x_buy = model.add_var(_name("deviation_buy_slack", scenario_index, period), upper=buy_slack_limit)
            x_sell = model.add_var(_name("deviation_sell_slack", scenario_index, period), upper=sell_slack_limit)
            model.add_constraint(
                {rt_buy: 1.0, rt_sell: -1.0, x_buy: -1.0},
                upper=inputs.deviation_ratio_limit * load,
            )
            model.add_constraint(
                {rt_buy: -1.0, rt_sell: 1.0, x_sell: -1.0},
                upper=inputs.deviation_ratio_limit * load,
            )
            deviation_buy_names[scenario_index, period] = x_buy
            deviation_sell_names[scenario_index, period] = x_sell

            da_price = float(scenario.day_ahead_price[period])
            rt_price = float(scenario.real_time_price[period])
            friction = inputs.transaction_friction_yuan_per_mwh
            cost_equation[da_buy] = cost_equation.get(da_buy, 0.0) - (da_price + friction)
            cost_equation[da_sell] = cost_equation.get(da_sell, 0.0) + (da_price - friction)
            cost_equation[rt_buy] = -(rt_price + friction)
            cost_equation[rt_sell] = rt_price - friction
            cost_equation[x_buy] = -inputs.deviation_buy_penalty
            cost_equation[x_sell] = -inputs.deviation_sell_penalty
            cost_equation[charge_names[period]] = -storage.degradation_yuan_per_mwh
            cost_equation[discharge_names[period]] = -storage.degradation_yuan_per_mwh

        model.add_constraint(cost_equation, lower=0.0, upper=0.0)
        revenue = float(settlements[scenario_index].retail_revenue_yuan)
        model.add_constraint({loss: 1.0, wholesale_cost: -1.0}, lower=-revenue, upper=-revenue)
        wholesale_cost_names.append(wholesale_cost)
        loss_names.append(loss)

    eta = model.add_var("cvar_eta", lower=-cost_bound, upper=cost_bound, objective=risk_lambda)
    excess_names = []
    for scenario_index, loss in enumerate(loss_names):
        excess = model.add_var(
            _name("cvar_excess", scenario_index),
            upper=2.0 * cost_bound,
            objective=risk_lambda * probabilities[scenario_index] / (1.0 - inputs.cvar_alpha),
        )
        model.add_constraint({excess: 1.0, loss: -1.0, eta: 1.0}, lower=0.0)
        excess_names.append(excess)
    if inputs.minimum_expected_profit_yuan is not None:
        model.add_constraint(
            {name: probability for name, probability in zip(loss_names, probabilities)},
            upper=-float(inputs.minimum_expected_profit_yuan),
        )
    if inputs.maximum_profit_loss_cvar_yuan is not None:
        cvar_constraint = {eta: 1.0}
        for excess, probability in zip(excess_names, probabilities):
            cvar_constraint[excess] = probability / (1.0 - inputs.cvar_alpha)
        model.add_constraint(cvar_constraint, upper=float(inputs.maximum_profit_loss_cvar_yuan))

    try:
        solved = model.solve(
            time_limit_seconds=inputs.time_limit_seconds,
            mip_relative_gap=inputs.mip_relative_gap,
        )
    except MilpBackendUnavailable as exc:
        raise MilpBackendUnavailable(
            "pifa_lingshou 需要 SciPy/HiGHS；请在仓库根目录执行 "
            "python3 -m pip install -e pifa"
        ) from exc
    except MilpSolveError as exc:
        return {
            "package": package,
            "risk_lambda": risk_lambda,
            "status": "INFEASIBLE_OR_SOLVER_ERROR",
            "feasible": False,
            "reason": str(exc),
        }
    values = solved.values
    scenario_costs = [values[name] for name in wholesale_cost_names]
    scenario_revenues = [item.retail_revenue_yuan for item in settlements]
    scenario_profits = [revenue - cost for revenue, cost in zip(scenario_revenues, scenario_costs)]
    scenario_losses = [-value for value in scenario_profits]
    probabilities_tuple = tuple(probabilities)
    profit_stats = summarize_distribution(scenario_profits, probabilities_tuple, inputs.cvar_alpha)
    wholesale_stats = summarize_distribution(scenario_costs, probabilities_tuple, inputs.cvar_alpha)
    revenue_stats = summarize_distribution(scenario_revenues, probabilities_tuple, inputs.cvar_alpha)
    loss_cvar = summarize_distribution(scenario_losses, probabilities_tuple, inputs.cvar_alpha)["cvar"]
    # Keep the wholesale ledger auditable. The old result exposed only
    # wholesale_cost, which made the wholesale side look absent even though
    # the MILP contained all of these terms.
    wholesale_breakdowns = []
    for scenario_index, scenario in enumerate(inputs.scenarios):
        contract_cost = float(values[contract_cost_name])
        day_ahead_cost = 0.0
        real_time_cost = 0.0
        deviation_cost = 0.0
        storage_degradation_cost = 0.0
        friction = float(inputs.transaction_friction_yuan_per_mwh)
        for period in range(PERIODS):
            da_price = float(scenario.day_ahead_price[period])
            rt_price = float(scenario.real_time_price[period])
            day_ahead_cost += (
                (da_price + friction) * values[da_buy_names[period]]
                - (da_price - friction) * values[da_sell_names[period]]
            )
            real_time_cost += (
                (rt_price + friction) * values[rt_buy_names[scenario_index, period]]
                - (rt_price - friction) * values[rt_sell_names[scenario_index, period]]
            )
            deviation_cost += (
                float(inputs.deviation_buy_penalty) * values[deviation_buy_names[scenario_index, period]]
                + float(inputs.deviation_sell_penalty) * values[deviation_sell_names[scenario_index, period]]
            )
            storage_degradation_cost += float(storage.degradation_yuan_per_mwh) * (
                values[charge_names[period]] + values[discharge_names[period]]
            )
        wholesale_breakdowns.append({
            "scenario_id": scenario.scenario_id,
            "probability": float(scenario.probability),
            "contract_cost_yuan": contract_cost,
            "day_ahead_cost_yuan": day_ahead_cost,
            "real_time_cost_yuan": real_time_cost,
            "deviation_penalty_yuan": deviation_cost,
            "storage_degradation_yuan": storage_degradation_cost,
            "total_wholesale_cost_yuan": scenario_costs[scenario_index],
        })
    expected_wholesale_breakdown = {
        key: weighted_mean(
            [item[key] for item in wholesale_breakdowns], probabilities_tuple
        )
        for key in (
            "contract_cost_yuan",
            "day_ahead_cost_yuan",
            "real_time_cost_yuan",
            "deviation_penalty_yuan",
            "storage_degradation_yuan",
            "total_wholesale_cost_yuan",
        )
    }
    declaration_band_slack_lower_mwh = sum(values[name] for name in slack_lower_names)
    declaration_band_slack_upper_mwh = sum(values[name] for name in slack_upper_names)
    declaration_band_penalty = float(inputs.declaration_slack_penalty_yuan_per_mwh) * (
        declaration_band_slack_lower_mwh + declaration_band_slack_upper_mwh
    )
    customer_results = {}
    for customer in inputs.customers:
        bills = [item.customer_bills_yuan[customer.customer_id] for item in settlements]
        bill_stats = summarize_distribution(bills, probabilities_tuple, inputs.cvar_alpha)
        savings = [customer.base_bill_yuan - value for value in bills]
        saving_stats = summarize_distribution(savings, probabilities_tuple, inputs.cvar_alpha)
        load_by_scenario = [
            sum(float(value) for value in scenario.customer_load_mwh[customer.customer_id])
            for scenario in inputs.scenarios
        ]
        load_mean = sum(value * probability for value, probability in zip(load_by_scenario, probabilities_tuple))
        load_std = (
            sum(probability * (value - load_mean) ** 2 for value, probability in zip(load_by_scenario, probabilities_tuple))
            ** 0.5
        )
        allocated_profit = []
        for scenario_index, scenario in enumerate(inputs.scenarios):
            customer_load = load_by_scenario[scenario_index]
            aggregate_load = sum(
                sum(float(value) for value in scenario.customer_load_mwh[item.customer_id])
                for item in inputs.customers
            )
            share = customer_load / aggregate_load if aggregate_load > 0 else 0.0
            allocated_profit.append(share * scenario_profits[scenario_index])
        customer_results[customer.customer_id] = {
            "package": inputs.locked_state.locked_packages.get(customer.customer_id, package),
            "expected_bill_yuan": bill_stats["mean"],
            "bill_p10_yuan": bill_stats["p10"],
            "bill_p90_yuan": bill_stats["p90"],
            "expected_saving_yuan": saving_stats["mean"],
            "saving_p10_yuan": saving_stats["p10"],
            "saving_p90_yuan": saving_stats["p90"],
            "load_mean_mwh": load_mean,
            "load_std_mwh": load_std,
            "bill_range_yuan": bill_stats["p90"] - bill_stats["p10"],
            "allocated_profit_stats_yuan": summarize_distribution(allocated_profit, probabilities_tuple, inputs.cvar_alpha),
            "base_bill_yuan": float(customer.base_bill_yuan),

        }

    expected_bill_limits = {}
    for customer in inputs.customers:
        limit = inputs.customer_bill_limits_yuan.get(customer.customer_id, customer.max_expected_bill_yuan)
        if limit is not None:
            expected_bill_limits[customer.customer_id] = {
                "limit_yuan": float(limit),
                "actual_yuan": customer_results[customer.customer_id]["expected_bill_yuan"],
                "satisfied": customer_results[customer.customer_id]["expected_bill_yuan"] <= float(limit) + 1e-6,
            }
    expected_profit = profit_stats["mean"]
    feasibility = {
        "customer_bill_limits": expected_bill_limits,
        "minimum_expected_profit": {
            "limit_yuan": inputs.minimum_expected_profit_yuan,
            "actual_yuan": expected_profit,
            "satisfied": inputs.minimum_expected_profit_yuan is None or expected_profit + 1e-6 >= inputs.minimum_expected_profit_yuan,
        },
        "maximum_profit_loss_cvar": {
            "limit_yuan": inputs.maximum_profit_loss_cvar_yuan,
            "actual_yuan": loss_cvar,
            "satisfied": inputs.maximum_profit_loss_cvar_yuan is None or loss_cvar <= inputs.maximum_profit_loss_cvar_yuan + 1e-6,
        },
    }
    feasible = all(item["satisfied"] for item in expected_bill_limits.values()) and feasibility["minimum_expected_profit"]["satisfied"] and feasibility["maximum_profit_loss_cvar"]["satisfied"]

    half_hour_contract = [values[position_names[index]] for index in range(48)]
    annual_contract = [values[annual_position_names[index]] for index in range(48)]
    quarter_hour_contract = [values[name] for name in contract_curve_names]
    rows = []
    for period in range(PERIODS):
        rows.append({
            "period": period + 1,
            "aggregate_load_by_scenario_mwh": [_aggregate_load(inputs, scenario_index, period) for scenario_index in range(len(inputs.scenarios))],
            "declaration_mwh": values[declarations[period]],
            "day_ahead_price_by_scenario": [float(s.day_ahead_price[period]) for s in inputs.scenarios],
            "real_time_price_by_scenario": [float(s.real_time_price[period]) for s in inputs.scenarios],
            "declaration_slack_lower_mwh": values[slack_lower_names[period]],
            "declaration_slack_upper_mwh": values[slack_upper_names[period]],
            "contract_supply_mwh": quarter_hour_contract[period],
            "day_ahead_buy_mwh": values[da_buy_names[period]],
            "day_ahead_sell_mwh": values[da_sell_names[period]],
            "day_ahead_net_mwh": values[da_buy_names[period]] - values[da_sell_names[period]],
            "charge_mwh": values[charge_names[period]],
            "discharge_mwh": values[discharge_names[period]],
            "soc_mwh": values[soc_names[period]],
            "real_time_buy_by_scenario_mwh": [values[rt_buy_names[scenario_index, period]] for scenario_index in range(len(inputs.scenarios))],
            "real_time_sell_by_scenario_mwh": [values[rt_sell_names[scenario_index, period]] for scenario_index in range(len(inputs.scenarios))],
            "real_time_net_by_scenario_mwh": [
                values[rt_buy_names[scenario_index, period]] - values[rt_sell_names[scenario_index, period]]
                for scenario_index in range(len(inputs.scenarios))
            ],
            "deviation_buy_slack_by_scenario_mwh": [
                values[deviation_buy_names[scenario_index, period]]
                for scenario_index in range(len(inputs.scenarios))
            ],
            "deviation_sell_slack_by_scenario_mwh": [
                values[deviation_sell_names[scenario_index, period]]
                for scenario_index in range(len(inputs.scenarios))
            ],
        })

    max_balance_residual = 0.0
    for scenario_index in range(len(inputs.scenarios)):
        for period in range(PERIODS):
            residual = (
                values[declarations[period]]
                + values[rt_buy_names[scenario_index, period]]
                - values[rt_sell_names[scenario_index, period]]
                - values[charge_names[period]]
                + values[discharge_names[period]]
                - _aggregate_load(inputs, scenario_index, period)
            )
            max_balance_residual = max(max_balance_residual, abs(residual))
    soc_residuals = []
    previous_soc = float(storage.initial_soc_mwh)
    for period in range(PERIODS):
        current_soc = values[soc_names[period]]
        soc_residuals.append(
            current_soc - previous_soc
            - storage.efficiency * values[charge_names[period]]
            + values[discharge_names[period]] / storage.efficiency
        )
        previous_soc = current_soc
    expected_profit_reconciliation = (
        profit_stats["mean"] - (revenue_stats["mean"] - wholesale_stats["mean"])
    )

    return {
        "package": package,
        "package_selection_matrix": _package_matrix(inputs, package),
        "risk_lambda": float(risk_lambda),
        "status": solved.status,
        "feasible": feasible,
        "backend": solved.backend,
        "objective_yuan": solved.objective,
        "mip_gap": solved.mip_gap,
        "solve_seconds": solved.solve_seconds,
        "variable_count": solved.variable_count,
        "binary_count": solved.binary_count,
        "constraint_count": solved.constraint_count,
        "expected_wholesale_cost_yuan": wholesale_stats["mean"],
        "wholesale_cost_cvar_yuan": wholesale_stats["cvar"],
        "wholesale_cost_breakdown": {
            "expected": expected_wholesale_breakdown,
            "scenarios": wholesale_breakdowns,
        },
        "expected_retail_revenue_yuan": revenue_stats["mean"],
        "retail_revenue_p10_yuan": revenue_stats["p10"],
        "retail_revenue_p90_yuan": revenue_stats["p90"],
        "expected_profit_yuan": profit_stats["mean"],
        "profit_p10_yuan": profit_stats["p10"],
        "profit_p90_yuan": profit_stats["p90"],
        "profit_loss_cvar_yuan": loss_cvar,
        "declaration_band_slack_lower_mwh": declaration_band_slack_lower_mwh,
        "declaration_band_slack_upper_mwh": declaration_band_slack_upper_mwh,
        "declaration_band_penalty_yuan": declaration_band_penalty,
        "accounting_checks": {
            "max_energy_balance_residual_mwh": max_balance_residual,
            "max_soc_recursion_residual_mwh": max(map(abs, soc_residuals), default=0.0),
            "terminal_soc_residual_mwh": values[soc_names[-1]] - float(storage.initial_soc_mwh),
            "expected_profit_reconciliation_yuan": expected_profit_reconciliation,
            "max_wholesale_ledger_residual_yuan": max(abs(
                sum(item[key] for key in (
                    "contract_cost_yuan", "day_ahead_cost_yuan", "real_time_cost_yuan",
                    "deviation_penalty_yuan", "storage_degradation_yuan",
                )) - item["total_wholesale_cost_yuan"]
            ) for item in wholesale_breakdowns),
            "customer_revenue_reconciliation_yuan": sum(
                item["expected_bill_yuan"] for item in customer_results.values()
            ) - revenue_stats["mean"],
        },
        "customer_results": customer_results,
        "scenario_results": [
            {
                "scenario_id": scenario.scenario_id,
                "probability": scenario.probability,
                "wholesale_cost_yuan": scenario_costs[index],
                "retail_revenue_yuan": scenario_revenues[index],
                "profit_yuan": scenario_profits[index],
                "loss_yuan": scenario_losses[index],
            }
            for index, scenario in enumerate(inputs.scenarios)
        ],
        "contracts": {
            "product_ids": product_ids,
            "total_position_mwh_per_half_hour": half_hour_contract,
            "annual_position_mwh_per_half_hour": annual_contract,
            "mapped_supply_mwh_per_quarter_hour": quarter_hour_contract,
            "contract_cost_yuan": values[contract_cost_name],
            "old_contract_cost_yuan": float(inputs.old_contract_cost_yuan),
            "delivery_mwh_per_half_hour": list(inputs.monthly_delivery_mwh),
            "annual_coverage_minimum": inputs.annual_coverage_minimum,
            "overall_coverage_minimum": inputs.overall_coverage_minimum,
            "overall_coverage_maximum": inputs.overall_coverage_maximum,
            "trades_by_product": [
                {
                    "product_id": product.product_id,
                    "buy_mwh_per_half_hour": [values[buy_names[product_index, h]] for h in range(48)],
                    "sell_mwh_per_half_hour": [values[sell_names[product_index, h]] for h in range(48)],
                    "net_cost_yuan": sum(
                        float(product.buy_price[h]) * values[buy_names[product_index, h]]
                        - float(product.sell_price[h]) * values[sell_names[product_index, h]]
                        for h in range(48)
                    ),
                }
                for product_index, product in enumerate(inputs.contract_products)
            ],
        },
        "storage_summary": {
            "total_charge_mwh": sum(values[name] for name in charge_names),
            "total_discharge_mwh": sum(values[name] for name in discharge_names),
            "initial_soc_mwh": float(storage.initial_soc_mwh),
            "soc_lower_bound_mwh": float(storage.minimum_soc_mwh),
            "soc_upper_bound_mwh": float(storage.maximum_soc_mwh),
            "efficiency": float(storage.efficiency),
            "terminal_soc_mwh": values[soc_names[-1]],
            "maximum_soc_mwh": max(values[name] for name in soc_names),
            "degradation_cost_yuan": expected_wholesale_breakdown["storage_degradation_yuan"],
        },
        "schedule": rows,
        "constraints": feasibility,
        "scenario_losses_yuan": scenario_losses,
        "solver_message": solved.message,
    }
