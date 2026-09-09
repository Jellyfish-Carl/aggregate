from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .domain import ContractFill, MpcState
from .load_forecast import (
    IndustrialParkConfig,
    aggregate_spot_snapshot,
    build_forecast_snapshots,
    industrial_park_baseline_mw,
    month_equivalent_days,
    monthly_curve,
    public_snapshot,
    select_snapshot,
    snapshot_index,
)
from .l3_spot_storage_milp import solve_l3_milp
from .milp import MilpBackendUnavailable, MilpSolveError
from .mockdata import (
    HIGH_PRICE_WINDOWS,
    MIDDAY_PV_WINDOW,
    build_price_forecasts,
    build_rolling_order_book,
    price_curves,
)
from .mock_scenario import DEFAULT_LOAD_SCENARIO_SEED, DEFAULT_PRICE_SCENARIO_SEED
from .retail import RetailMarketConfig, load_weighted_price, retail_settlement
from .scenario import (
    JOINT_CANDIDATE_COUNT,
    L3_OPTIMIZATION_SCENARIO_COUNT,
    cvar,
    discrete_load_scenarios,
    discrete_load_value,
    joint_scenario_diagnostics,
    reduced_joint_trajectories,
)
from .settlement import (
    SettlementInputs,
    annual_ratio_recovery,
    contract_difference_cost,
    day_ahead_recovery,
    over_profit_recovery,
    robust_declaration_band,
    settle_month,
)
from .timegrid import SPOT_HOURS, SPOT_PERIODS, aggregate_quarter_hour_energy, expand_half_hour_energy, spot_time
from .trading import L1_EVENTS, L2_NEAR_TERM_EVENTS, ROLLING_EVENTS, TRADE_EVENTS, TraderConfig, build_portfolio


EVENT_ORDER = ("ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2", "D-1", "D-1_PRICE", "REAL_TIME", "MONTH_END")
EVENT_LABELS = {
    "ANNUAL": "年度",
    "MONTHLY": "月度",
    "TEN_DAY": "旬内",
    "D-3": "D-3滚撮",
    "D-2": "D-2滚撮",
    "D-1": "D-1",
    "D-1_PRICE": "D-1 17:30",
    "REAL_TIME": "实时",
    "MONTH_END": "月末",
}


class _SimulationTrace:
    def __init__(self, logger: Optional[Callable[[str], None]], event: str) -> None:
        self._logger = logger
        self._event = event
        self._started = perf_counter()
        self._last = self._started
        self._records: List[dict] = []
        if self._logger is not None:
            self._logger("开始计算 event=%s" % event)

    def mark(self, label: str) -> None:
        now = perf_counter()
        step_seconds = now - self._last
        total_seconds = now - self._started
        self._last = now
        self._records.append(
            {
                "label": label,
                "step_seconds": round(step_seconds, 4),
                "total_seconds": round(total_seconds, 4),
            }
        )
        if self._logger is not None:
            self._logger(
                "%s | %s %.3fs，累计 %.3fs"
                % (self._event, label, step_seconds, total_seconds)
            )

    def l3(self, label: str, path: Mapping[str, object]) -> None:
        run = path.get("milp")
        if run is None:
            if self._logger is not None:
                self._logger("%s | %s 未执行MILP：%s" % (self._event, label, path.get("error")))
            return
        optimization = dict(run.get("optimization", {}))
        pricing = dict(optimization.get("pricing_lp", {}))
        if self._logger is not None:
            self._logger(
                "%s | %s L3 MILP status=%s solve=%.3fs pricing_lp=%.3fs end_to_end=%.3fs vars=%s bin=%s cons=%s"
                % (
                    self._event,
                    label,
                    optimization.get("status"),
                    float(optimization.get("solve_seconds") or 0.0),
                    float(pricing.get("solve_seconds") or 0.0),
                    float(optimization.get("end_to_end_seconds") or 0.0),
                    optimization.get("variable_count"),
                    optimization.get("binary_count"),
                    optimization.get("constraint_count"),
                )
            )

    def finish(self) -> None:
        self.mark("响应组装完成")
        if self._logger is not None:
            self._logger(
                "%s | 全流程完成 %.3fs"
                % (self._event, perf_counter() - self._started)
            )

    def payload(self) -> dict:
        return {
            "total_seconds": round(perf_counter() - self._started, 4),
            "steps": list(self._records),
        }


def _money(value: float) -> float:
    return round(value, 2)


def _event_index(event: str) -> int:
    try:
        return EVENT_ORDER.index(event)
    except ValueError as exc:
        raise ValueError("未知事件: " + event) from exc


def _price_forecast_for_event(
    event: str,
    forecasts: Mapping[str, dict],
    official_prices: Mapping[str, Sequence[float]],
    rt_period: int = 1,
) -> dict:
    if event == "D-3":
        return forecasts["D-3"]
    if event == "D-2":
        return forecasts["D-2"]
    source = forecasts["D-1"]
    if _event_index(event) < _event_index("D-1_PRICE"):
        return source
    conditioned = {**source, "snapshot_id": source["snapshot_id"] + "-DA-OFFICIAL"}
    rows = []
    for index, source_row in enumerate(source["rows"]):
        row = dict(source_row)
        day_ahead = float(official_prices["day_ahead"][index])
        row.update(
            {
                "day_ahead_p10": day_ahead,
                "day_ahead_p50": day_ahead,
                "day_ahead_p90": day_ahead,
                "real_time_p10": day_ahead + float(row["spread_p10"]),
                "real_time_p50": day_ahead + float(row["spread_p50"]),
                "real_time_p90": day_ahead + float(row["spread_p90"]),
            }
        )
        rows.append(row)
    conditioned["rows"] = rows
    conditioned["information_state"] = "OFFICIAL_DAY_AHEAD_REVEALED"
    visible_rt_count = (
        SPOT_PERIODS
        if event == "MONTH_END"
        else max(0, rt_period - 1)
        if event == "REAL_TIME"
        else 0
    )
    for index in range(visible_rt_count):
        row = rows[index]
        real_time = float(official_prices["real_time"][index])
        day_ahead = float(official_prices["day_ahead"][index])
        spread = real_time - day_ahead
        row.update(
            {
                "spread_p10": spread,
                "spread_p50": spread,
                "spread_p90": spread,
                "real_time_p10": real_time,
                "real_time_p50": real_time,
                "real_time_p90": real_time,
                "real_time_observed": True,
            }
        )
    conditioned["real_time_observed_through"] = visible_rt_count
    return conditioned


def _fully_realized_snapshot(
    snapshot: Mapping[str, object], actual: Sequence[float]
) -> dict:
    """Condition every load period to settlement truth at month end."""

    realized = dict(snapshot)
    rows = []
    for source, value in zip(snapshot["rows"], actual):
        row = dict(source)
        row.update(
            {
                "p10_mwh": float(value),
                "p50_mwh": float(value),
                "p90_mwh": float(value),
                "actual_mwh": float(value),
                "status": "ACTUAL",
            }
        )
        rows.append(row)
    realized["rows"] = rows
    realized["snapshot_id"] = str(snapshot["snapshot_id"]) + "-SETTLED"
    return realized


def _visible_portfolio(portfolio: Mapping[str, object], event: str) -> dict:
    selected_index = _event_index(event)
    actions = [
        action
        for action in portfolio["actions"]
        if _event_index(str(action["event"])) <= selected_index
    ]
    visible_events = {str(action["event"]) for action in actions}
    fills = [fill for fill in portfolio["fills"] if fill.product_class in visible_events]
    position = float(actions[-1]["resulting_daily_position_mwh"]) if actions else 0.0
    annual = sum(
        fill.signed_quantity for fill in fills if fill.product_class == "ANNUAL"
    )
    overall = sum(
        fill.signed_quantity for fill in fills if fill.assessment_base_eligible
    )
    rolling_sold = sum(
        float(action.get("sell_quantity_mwh", action["quantity_mwh"]))
        for action in actions
        if action["event"] in {"D-3", "D-2"}
        and float(action.get("sell_quantity_mwh", 0.0)) > 1e-9
    )
    if not rolling_sold:
        rolling_sold = sum(
            float(action["quantity_mwh"])
            for action in actions
            if action["event"] in {"D-3", "D-2"} and action["side"] == "SELL"
        )
    rolling_cap = next(
        (
            float(action.get("rolling_sell_cap_mwh", 0.0))
            for action in reversed(actions)
            if action["event"] in {"D-3", "D-2"}
        ),
        0.0,
    )
    return {
        "strategy": portfolio["strategy"],
        "actions": actions,
        "fills": fills,
        "position_mwh": position,
        "position_unit": "MWh/delivery_day",
        "annual_position_mwh": annual,
        "overall_position_mwh": overall,
        "rolling_sell_used_mwh": rolling_sold,
        "rolling_sell_cap_mwh": rolling_cap,
        "optimization": portfolio.get("optimization", {}),
    }


def _assessment_curves(
    fills: Sequence[ContractFill],
) -> tuple:
    """Build raw 48-point annual and overall contract curves."""

    annual = [0.0] * 48
    overall = [0.0] * 48
    for fill in fills:
        is_annual = fill.product_class == "ANNUAL"
        is_overall = fill.assessment_base_eligible
        if not is_annual and not is_overall:
            continue
        for index in range(48):
            value = float(fill.delivery_curve.get("P%02d" % (index + 1), 0.0))
            if is_annual:
                annual[index] += value
            if is_overall:
                overall[index] += value
    return annual, overall


def _diagnostic_declaration(
    snapshot: Mapping[str, object],
    risk: float,
    physical_peak_mw: float,
    locked: bool,
    day_ahead_prices: Sequence[float],
    real_time_price_forecast: Sequence[float],
    locked_values: Sequence[float] = (),
) -> dict:
    """Build a non-optimizing declaration for explicit diagnostic mode.

    This is the dependency-free reference equivalent of the L3 declaration
    subproblem.  A production backend should replace the pointwise search with
    one stochastic MILP so declarations, storage and non-anticipativity are
    solved together.
    """

    if len(day_ahead_prices) != SPOT_PERIODS or len(real_time_price_forecast) != SPOT_PERIODS:
        raise ValueError("日前和实时价格预测必须各有96个点")
    if locked_values and len(locked_values) != SPOT_PERIODS:
        raise ValueError("锁定日前申报必须有96个点")

    def scenario_cost(load: float, declared: float, day_ahead: float, real_time: float) -> float:
        deviation = load - declared
        excess = max(abs(deviation) - 0.10 * load, 0.0)
        if deviation > 0:
            spread = max(day_ahead - real_time, 0.0)
        else:
            spread = max(real_time - day_ahead, 0.0)
        recovery = 1.05 * spread * excess
        return day_ahead * declared + real_time * deviation + recovery

    rows: List[dict] = []
    scenarios = discrete_load_scenarios()
    probabilities = tuple(item.probability for item in scenarios)
    for index, row in enumerate(snapshot["rows"]):
        p10 = float(row["p10_mwh"])
        p50 = float(row["p50_mwh"])
        p90 = float(row["p90_mwh"])
        physical_upper = physical_peak_mw * SPOT_HOURS
        lower, upper = robust_declaration_band(p10, p90, physical_upper)
        target = p50 + risk * 0.50 * (p90 - p50)
        day_ahead = float(day_ahead_prices[index])
        real_time = float(real_time_price_forecast[index])
        if real_time - day_ahead >= -20.0:
            reserve_weight = 0.50 + 0.50 * risk
            lower = max(lower, p50 + reserve_weight * (p90 - p50))
        if locked_values:
            declared = float(locked_values[index])
            lower = upper = declared
        elif lower <= upper:
            candidates = [lower, upper, p10, p50, p90, target]
            step = (upper - lower) / 20.0
            candidates.extend(lower + step * candidate_index for candidate_index in range(21))
            candidates = sorted({round(max(lower, min(upper, value)), 6) for value in candidates})
            scored = []
            for candidate in candidates:
                costs = [
                    scenario_cost(
                        discrete_load_value(row, scenario),
                        candidate,
                        day_ahead,
                        real_time * scenario.real_time_price_factor,
                    )
                    for scenario in scenarios
                ]
                mean_cost = sum(cost * probability for cost, probability in zip(costs, probabilities))
                score = (1.0 - risk) * mean_cost + risk * max(costs)
                scored.append((score, candidate))
            _, declared = min(scored, key=lambda item: item[0])
        else:
            declared = p50
        slack_lower = max(lower - declared, 0.0)
        slack_upper = max(declared - upper, 0.0)
        if real_time > day_ahead + 1e-9:
            price_reason = "实时价格预测高于日前价，申报向上界靠拢以减少高价实时补购"
        elif real_time < day_ahead - 1e-9:
            price_reason = "实时价格预测低于日前价，申报向下界靠拢但受偏差回收约束"
        else:
            price_reason = "日前与实时价格预测接近，主要按负荷风险选择申报"
        rows.append(
            {
                "period": row["period"],
                "time": row["time"],
                "p10_mwh": round(p10, 3),
                "p50_mwh": round(p50, 3),
                "p90_mwh": round(p90, 3),
                "lower_mwh": round(lower, 3),
                "upper_mwh": round(upper, 3),
                "band_slack_lower_mwh": round(slack_lower, 6),
                "band_slack_upper_mwh": round(slack_upper, 6),
                "declared_mwh": round(declared, 3),
                "day_ahead_price_yuan_per_mwh": round(day_ahead, 3),
                "real_time_price_forecast_yuan_per_mwh": round(real_time, 3),
                "price_spread_yuan_per_mwh": round(real_time - day_ahead, 3),
                "price_reason": price_reason,
            }
        )
    return {
        "status": "LOCKED" if locked else "PROJECTED",
        "deadline": "D-1 日前市场关闸（17:30真实日前价格公布前）",
        "period_count": SPOT_PERIODS,
        "interval_minutes": 15,
        "total_mwh": round(sum(row["declared_mwh"] for row in rows), 3),
        "total_band_slack_mwh": round(
            sum(
                row["band_slack_lower_mwh"] + row["band_slack_upper_mwh"]
                for row in rows
            ),
            6,
        ),
        "price_aware": True,
        "layer": "L3",
        "objective_note": "P10/P50/P90负荷与日前/实时价格场景成本，含10%偏差回收代理",
        "rows": rows,
    }


def _diagnostic_storage_dispatch(
    actual: Sequence[float],
    declaration: Sequence[float],
    day_ahead: Sequence[float],
    real_time: Sequence[float],
    rt_period: int,
    fixed_rows: Sequence[Mapping[str, object]] = (),
    fixed_until: int = 0,
) -> dict:
    capacity = 20.0
    minimum_soc = 2.0
    efficiency = 0.92
    max_grid_energy = 1.25
    sorted_day_ahead = sorted(day_ahead)
    charge_threshold = sorted_day_ahead[max(0, int(len(sorted_day_ahead) * 0.25) - 1)]
    # Reserve stored energy for the day's highest-price window instead of
    # discharging at every merely-above-average interval.
    discharge_threshold = sorted_day_ahead[min(len(sorted_day_ahead) - 1, int(len(sorted_day_ahead) * 0.85))]
    soc = 10.0
    rows: List[dict] = []
    for index, (load, declared, da, rt) in enumerate(zip(actual, declaration, day_ahead, real_time)):
        soc_start = soc
        grid_charge = 0.0
        delivered = 0.0
        if index < fixed_until:
            fixed = fixed_rows[index]
            grid_charge = float(fixed["charge_from_grid_mwh"])
            delivered = float(fixed["discharge_to_load_mwh"])
            action = delivered - grid_charge
            soc += grid_charge * efficiency - delivered / efficiency
            mode = "CHARGE" if grid_charge > 1e-6 else "DISCHARGE" if delivered > 1e-6 else "IDLE"
        elif rt <= charge_threshold and soc < capacity - 0.01:
            grid_charge = min(max_grid_energy, (capacity - soc) / efficiency)
            action = -grid_charge
            soc += grid_charge * efficiency
            mode = "CHARGE"
        elif rt >= discharge_threshold and soc > minimum_soc + 0.01:
            delivered = min(max_grid_energy, (soc - minimum_soc) * efficiency)
            action = delivered
            soc -= delivered / efficiency
            mode = "DISCHARGE"
        else:
            action = 0.0
            mode = "IDLE"
        if index >= fixed_until:
            # Apply the same pointwise spot-deviation boundary as the MILP:
            # abs(load - storage_action - declaration) <= 10% * load.
            action_lower = 0.90 * load - declared
            action_upper = 1.10 * load - declared
            action = max(action_lower, min(action_upper, action))
            if action >= 0.0:
                delivered = min(action, max(0.0, (soc_start - minimum_soc) * efficiency))
                grid_charge = 0.0
                action = delivered
                soc = soc_start - delivered / efficiency
            else:
                grid_charge = min(-action, max(0.0, (capacity - soc_start) / efficiency))
                delivered = 0.0
                action = -grid_charge
                soc = soc_start + grid_charge * efficiency
            mode = "CHARGE" if grid_charge > 1e-6 else "DISCHARGE" if delivered > 1e-6 else "IDLE"
        grid_load = load - action
        real_time_net = grid_load - declared
        deviation_limit = 0.10 * load
        deviation_ratio = abs(real_time_net) / max(load, 1e-9)
        rows.append(
            {
                "period": index + 1,
                "time": spot_time(index + 1),
                "actual_load_mwh": round(load, 3),
                "declared_mwh": round(declared, 3),
                "day_ahead_price": round(da, 3),
                "real_time_price": round(rt, 3),
                "mode": mode,
                "storage_action_mwh": round(action, 3),
                "charge_from_grid_mwh": round(grid_charge, 3),
                "charge_stored_mwh": round(grid_charge * efficiency, 3),
                "discharge_to_load_mwh": round(delivered, 3),
                "discharge_from_soc_mwh": round(delivered / efficiency, 3),
                "grid_load_after_storage_mwh": round(grid_load, 3),
                "real_time_buy_mwh": round(max(real_time_net, 0.0), 6),
                "real_time_sell_mwh": round(max(-real_time_net, 0.0), 6),
                "real_time_net_mwh": round(real_time_net, 6),
                "spot_deviation_abs_mwh": round(abs(real_time_net), 6),
                "spot_deviation_limit_mwh": round(deviation_limit, 6),
                "spot_deviation_ratio": round(deviation_ratio, 6),
                "spot_deviation_compliant": abs(real_time_net) <= deviation_limit + 1e-7,
                "settled_supply_mwh": round(
                    declared + max(real_time_net, 0.0) - max(-real_time_net, 0.0),
                    6,
                ),
                "balance_residual_mwh": 0.0,
                "soc_start_mwh": round(soc_start, 3),
                "soc_mwh": round(soc, 3),
            }
        )
    current_index = max(1, min(SPOT_PERIODS, rt_period)) - 1
    charged = sum(max(-row["storage_action_mwh"], 0.0) for row in rows)
    discharged = sum(max(row["storage_action_mwh"], 0.0) for row in rows)
    charge_cost = sum(
        max(-row["storage_action_mwh"], 0.0) * row["real_time_price"] for row in rows
    )
    average_charge_price = charge_cost / max(charged, 1e-9)
    discharge_value = sum(max(row["storage_action_mwh"], 0.0) * row["real_time_price"] for row in rows)
    average_discharge_price = discharge_value / max(discharged, 1e-9)
    matched_discharge = min(discharged, charged * efficiency * efficiency)
    matched_charge = matched_discharge / (efficiency * efficiency) if matched_discharge else 0.0
    arbitrage_value = matched_discharge * average_discharge_price - matched_charge * average_charge_price
    return {
        "capacity_mwh": capacity,
        "power_mw": max_grid_energy / SPOT_HOURS,
        "interval_minutes": 15,
        "efficiency": efficiency,
        "charge_price_threshold": round(charge_threshold, 2),
        "discharge_price_threshold": round(discharge_threshold, 2),
        "charged_mwh": round(charged, 3),
        "discharged_mwh": round(discharged, 3),
        "gross_arbitrage_value_yuan": round(arbitrage_value, 2),
        "average_charge_price_yuan_per_mwh": round(average_charge_price, 2),
        "average_discharge_price_yuan_per_mwh": round(average_discharge_price, 2),
        "charge_periods": sum(row["mode"] == "CHARGE" for row in rows),
        "discharge_periods": sum(row["mode"] == "DISCHARGE" for row in rows),
        "current": rows[current_index],
        "rows": rows,
    }


def _solve_l3_path(
    snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    execution_load: Sequence[float],
    risk: float,
    physical_peak_mw: float,
    day_ahead_prices: Sequence[float],
    real_time_prices: Sequence[float],
    rt_period: int,
    locked: bool,
    locked_contract_curve: Sequence[float] = (),
    locked_contract_cost: float = 0.0,
    locked_declaration: Sequence[float] = (),
    fixed_storage_rows: Sequence[Mapping[str, object]] = (),
    fixed_until: int = 0,
    allow_diagnostic_policy: bool = False,
) -> Mapping[str, object]:
    try:
        if allow_diagnostic_policy:
            raise MilpBackendUnavailable("显式 diagnostic_mode：未调用 MILP 求解器")
        run = solve_l3_milp(
            snapshot,
            price_snapshot,
            execution_load,
            risk,
            physical_peak_mw,
            day_ahead_prices,
            real_time_prices,
            rt_period,
            locked,
            locked_contract_curve,
            locked_contract_cost,
            locked_declaration=locked_declaration,
            fixed_storage_rows=fixed_storage_rows,
            fixed_until=fixed_until,
        )
        return {"milp": run, "error": None, **run}
    except (MilpBackendUnavailable, MilpSolveError) as exc:
        if not allow_diagnostic_policy:
            raise
        declaration = _diagnostic_declaration(
            snapshot,
            risk,
            physical_peak_mw,
            locked,
            day_ahead_prices,
            real_time_prices,
            locked_declaration,
        )
        storage = _diagnostic_storage_dispatch(
            execution_load,
            [row["declared_mwh"] for row in declaration["rows"]],
            day_ahead_prices,
            real_time_prices,
            rt_period,
            fixed_storage_rows,
            fixed_until,
        )
        declaration.update(
            {
                "solver_backend": "DIAGNOSTIC_POLICY_NOT_A_SOLVER",
                "solver_status": "DIAGNOSTIC_POLICY_ONLY",
                "mip_gap": None,
            }
        )
        storage.update(
            {
                "solver_backend": "DIAGNOSTIC_POLICY_NOT_A_SOLVER",
                "solver_status": "DIAGNOSTIC_POLICY_ONLY",
                "mip_gap": None,
            }
        )
        return {
            "milp": None,
            "error": str(exc),
            "declaration": declaration,
            "storage": storage,
            "execution_ledger": {
                "scenario": "DIAGNOSTIC_MEDIAN_PATH",
                "fixed_until": fixed_until,
                "rows": storage["rows"],
            },
        }


def _replay_state(
    declaration: Mapping[str, object],
    execution_rows: Sequence[Mapping[str, object]],
    fixed_until: int,
    event: str,
    posted_contracts: Sequence[ContractFill] = (),
) -> MpcState:
    periods = ["P%02d" % (index + 1) for index in range(SPOT_PERIODS)]
    accepted = {
        period: float(row["declared_mwh"])
        for period, row in zip(periods, declaration["rows"])
    }
    executed = list(execution_rows[:fixed_until])
    return MpcState(
        state_version=fixed_until,
        event_id="DEMO-" + event,
        current_interval=(None if fixed_until == 0 else periods[fixed_until - 1]),
        posted_contracts=list(posted_contracts),
        declaration_curve=dict(accepted),
        accepted_curve=accepted,
        real_time_buy_curve={
            periods[index]: float(row["real_time_buy_mwh"])
            for index, row in enumerate(executed)
        },
        real_time_sell_curve={
            periods[index]: float(row["real_time_sell_mwh"])
            for index, row in enumerate(executed)
        },
        charge_curve={
            periods[index]: float(row["charge_from_grid_mwh"])
            for index, row in enumerate(executed)
        },
        discharge_curve={
            periods[index]: float(row["discharge_to_load_mwh"])
            for index, row in enumerate(executed)
        },
        soc_mwh=(10.0 if not executed else float(executed[-1]["soc_mwh"])),
        locked_plan_ids=[fill.fill_id for fill in posted_contracts],
        applied_source_ids=[fill.fill_id for fill in posted_contracts],
    )


def _declaration_breakdown(
    fills: Sequence[ContractFill],
    declaration: Mapping[str, object],
    actual_day: Sequence[float],
    month_factor: float,
    storage: Mapping[str, object],
) -> dict:
    """Build the per-period energy stack used by the D-1 chart.

    Annual/monthly/ten-day fills are stored as monthly energy in the trading
    ledger, while rolling fills are already delivery-day energy.  Convert only
    the former to a daily curve before reconciling against the 48-point plan.
    """

    products = ("ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2")
    curves = {product: [0.0] * SPOT_PERIODS for product in products}
    for fill in fills:
        product = str(fill.product_class)
        if product not in curves:
            continue
        scale = 1.0 / float(fill.equivalent_delivery_days)
        half_hour_curve = [
            float(fill.delivery_curve.get("P%02d" % (index + 1), 0.0)) * scale
            for index in range(48)
        ]
        for index, value in enumerate(expand_half_hour_energy(half_hour_curve)):
            curves[product][index] += value

    rows: List[dict] = []
    declaration_rows = declaration["rows"]
    storage_rows = storage["rows"]
    for index, declaration_row in enumerate(declaration_rows):
        long_term = sum(curves[product][index] for product in products)
        declared = float(declaration_row["declared_mwh"])
        actual = float(actual_day[index])
        storage_row = storage_rows[index]
        net_load = float(storage_row["grid_load_after_storage_mwh"])
        day_ahead_net = float(
            declaration_row.get("day_ahead_net_mwh", declared - long_term)
        )
        real_time_buy = float(storage_row["real_time_buy_mwh"])
        real_time_sell = float(storage_row["real_time_sell_mwh"])
        real_time_net = real_time_buy - real_time_sell
        forecast_load = float(declaration_row["p50_mwh"])
        spot_exposure = forecast_load - long_term
        deviation_abs = abs(real_time_net)
        deviation_limit = 0.10 * max(actual, 0.0)
        deviation_ratio = deviation_abs / max(actual, 1e-9)
        rows.append(
            {
                "period": index + 1,
                "time": declaration_row["time"],
                "annual_mwh": round(curves["ANNUAL"][index], 6),
                "monthly_mwh": round(curves["MONTHLY"][index], 6),
                "ten_day_mwh": round(curves["TEN_DAY"][index], 6),
                "d3_mwh": round(curves["D-3"][index], 6),
                "d2_mwh": round(curves["D-2"][index], 6),
                "long_term_mwh": round(long_term, 6),
                "day_ahead_buy_mwh": round(max(day_ahead_net, 0.0), 6),
                "day_ahead_sell_mwh": round(max(-day_ahead_net, 0.0), 6),
                "day_ahead_spot_mwh": round(day_ahead_net, 6),
                "declaration_mwh": round(declared, 6),
                "real_time_buy_mwh": round(real_time_buy, 6),
                "real_time_sell_mwh": round(real_time_sell, 6),
                "real_time_spot_mwh": round(real_time_net, 6),
                "spot_exposure_mwh": round(spot_exposure, 6),
                "spot_exposure_buy_mwh": round(max(spot_exposure, 0.0), 6),
                "spot_exposure_sell_mwh": round(max(-spot_exposure, 0.0), 6),
                "spot_deviation_abs_mwh": round(deviation_abs, 6),
                "spot_deviation_limit_mwh": round(deviation_limit, 6),
                "spot_deviation_ratio": round(deviation_ratio, 6),
                "spot_deviation_compliant": deviation_abs <= deviation_limit + 1e-6,
                "p10_mwh": round(float(declaration_row["p10_mwh"]), 3),
                "forecast_load_mwh": round(forecast_load, 3),
                "p90_mwh": round(float(declaration_row["p90_mwh"]), 3),
                "band_slack_mwh": round(
                    float(declaration_row.get("band_slack_lower_mwh", 0.0))
                    + float(declaration_row.get("band_slack_upper_mwh", 0.0)),
                    6,
                ),
                "actual_load_mwh": round(actual, 6),
                "net_grid_load_after_storage_mwh": round(net_load, 6),
                "real_time_after_storage_mwh": round(net_load - declared, 6),
                "reconciliation_mwh": round(
                    long_term
                    + max(day_ahead_net, 0.0)
                    - max(-day_ahead_net, 0.0)
                    + real_time_buy
                    - real_time_sell,
                    6,
                ),
            }
        )
    return {
        "unit": "MWh/15分钟",
        "monthly_contract_scale": round(month_factor, 6),
        "long_term_products": ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
        "long_term_assessment_granularity": "48_HALF_HOUR_PERIODS",
        "long_term_assessment_products": ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
        "long_term_delivery_allocation_grid": "48_HALF_HOUR_PRODUCTS",
        "spot_deviation_assessment_granularity": "96_QUARTER_HOUR_PERIODS",
        "spot_deviation_limit_ratio": 0.10,
        "spot_deviation_basis": "ACTUAL_LOAD_PER_QUARTER_HOUR",
        "planning_spot_exposure_definition": "P50预测负荷 - 已锁定中长期净合约量",
        "reconciliation_definition": "中长期 + 日前买入 - 日前卖出 + 实时买入 - 实时卖出 = 储能后电网负荷",
        "rows": rows,
        "totals": {
            "long_term_mwh": round(sum(row["long_term_mwh"] for row in rows), 3),
            "day_ahead_buy_mwh": round(sum(row["day_ahead_buy_mwh"] for row in rows), 3),
            "day_ahead_sell_mwh": round(sum(row["day_ahead_sell_mwh"] for row in rows), 3),
            "day_ahead_spot_mwh": round(sum(row["day_ahead_spot_mwh"] for row in rows), 3),
            "declaration_mwh": round(sum(row["declaration_mwh"] for row in rows), 3),
            "real_time_buy_mwh": round(sum(row["real_time_buy_mwh"] for row in rows), 3),
            "real_time_sell_mwh": round(sum(row["real_time_sell_mwh"] for row in rows), 3),
            "real_time_spot_mwh": round(sum(row["real_time_spot_mwh"] for row in rows), 3),
            "spot_exposure_mwh": round(sum(row["spot_exposure_mwh"] for row in rows), 3),
            "spot_exposure_buy_mwh": round(sum(row["spot_exposure_buy_mwh"] for row in rows), 3),
            "spot_exposure_sell_mwh": round(sum(row["spot_exposure_sell_mwh"] for row in rows), 3),
            "forecast_load_mwh": round(sum(row["forecast_load_mwh"] for row in rows), 3),
            "actual_load_mwh": round(sum(row["actual_load_mwh"] for row in rows), 3),
            "net_grid_load_after_storage_mwh": round(
                sum(row["net_grid_load_after_storage_mwh"] for row in rows), 3
            ),
            "spot_deviation_breach_periods": sum(
                not row["spot_deviation_compliant"] for row in rows
            ),
            "max_spot_deviation_ratio": round(
                max((row["spot_deviation_ratio"] for row in rows), default=0.0),
                6,
            ),
        },
    }


def _daily_contract_cfd(
    fills: Sequence[ContractFill],
    day_ahead_prices: Sequence[float],
    month_factor: float,
    product_filter: Sequence[str] = (),
) -> float:
    selected = set(product_filter)
    total = 0.0
    for fill in fills:
        if selected and fill.product_class not in selected:
            continue
        scale = 1.0 / float(fill.equivalent_delivery_days)
        for index in range(48):
            key = "P%02d" % (index + 1)
            da = 0.5 * (
                float(day_ahead_prices[2 * index])
                + float(day_ahead_prices[2 * index + 1])
            )
            total += scale * float(fill.delivery_curve.get(key, 0.0)) * (
                float(fill.price) - da
            )
    return total


def _daily_locked_contract_curve(
    fills: Sequence[ContractFill],
    product_filter: Sequence[str] = (),
) -> List[float]:
    """Convert immutable 48-point fills into the delivery day's 96-point curve."""

    selected = set(product_filter)
    half_hour = [0.0] * 48
    for fill in fills:
        if selected and fill.product_class not in selected:
            continue
        scale = 1.0 / float(fill.equivalent_delivery_days)
        for index in range(48):
            half_hour[index] += scale * float(
                fill.delivery_curve.get("P%02d" % (index + 1), 0.0)
            )
    return expand_half_hour_energy(half_hour)


def _daily_contract_fixed_cost(
    fills: Sequence[ContractFill], product_filter: Sequence[str] = ()
) -> float:
    selected = set(product_filter)
    return sum(
        float(fill.signed_quantity)
        / float(fill.equivalent_delivery_days)
        * float(fill.price)
        + float(fill.fee) / float(fill.equivalent_delivery_days)
        for fill in fills
        if not selected or fill.product_class in selected
    )


def _evaluate_l3_objective(
    fills: Sequence[ContractFill],
    declaration_breakdown: Mapping[str, object],
    storage: Mapping[str, object],
    day_ahead_prices: Sequence[float],
    real_time_prices: Sequence[float],
    risk: float,
    month_factor: float,
    load_snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
) -> dict:
    """Evaluate the integrated L3 economic objective for reporting.

    The same terms are used by the stochastic MILP. Diagnostic mode evaluates
    the selected policy on the same reduced joint scenario catalog, but does
    not claim to optimize it.
    """

    rows = declaration_breakdown["rows"]
    scenarios = reduced_joint_trajectories(
        load_snapshot, price_snapshot, L3_OPTIMIZATION_SCENARIO_COUNT
    )
    probabilities = tuple(item.probability for item in scenarios)
    scenario_costs: List[float] = []
    slack_penalty = 10000.0 * sum(float(row.get("band_slack_mwh", 0.0)) for row in rows)
    degradation = 2.0 * sum(
        float(row["charge_from_grid_mwh"])
        + float(row["discharge_to_load_mwh"])
        for row in storage["rows"]
    )
    for scenario in scenarios:
        physical = 0.0
        recovery = 0.0
        for index, row in enumerate(rows):
            load = float(scenario.load_mwh[index])
            charge = float(storage["rows"][index]["charge_from_grid_mwh"])
            discharge = float(storage["rows"][index]["discharge_to_load_mwh"])
            net_load = load + charge - discharge
            declared = float(row["declaration_mwh"])
            da = float(scenario.day_ahead_price[index])
            rt = float(scenario.real_time_price[index])
            physical += da * declared + rt * (net_load - declared)
            deviation = net_load - declared
            excess = max(abs(deviation) - 0.10 * net_load, 0.0)
            spread = max(da - rt, 0.0) if deviation > 0 else max(rt - da, 0.0)
            recovery += 1.05 * spread * excess
        contract_cfd = _daily_contract_cfd(fills, day_ahead_prices, month_factor)
        trade_fee = sum(
            float(fill.fee) / float(fill.equivalent_delivery_days)
            for fill in fills
        )
        scenario_costs.append(
            physical
            + contract_cfd
            + recovery
            + trade_fee
            + degradation
            + slack_penalty
        )
    expected = sum(cost * probability for cost, probability in zip(scenario_costs, probabilities))
    tail = cvar(scenario_costs, probabilities, 0.95)
    objective = (1.0 - risk) * expected + risk * tail
    rolling_cfd = _daily_contract_cfd(fills, day_ahead_prices, month_factor, ("D-3", "D-2"))
    return {
        "backend": "DIAGNOSTIC_POLICY_NOT_A_SOLVER",
        "model_structure": "JOINT_SCENARIO_DIAGNOSTIC_EVALUATION",
        "candidate_scenario_count": JOINT_CANDIDATE_COUNT,
        "optimization_scenario_count": len(scenarios),
        "scenario_generation": joint_scenario_diagnostics(
            load_snapshot, price_snapshot, scenarios
        ),
        "scenario_catalog": [
            {
                "scenario_id": item.scenario_id,
                "probability": item.probability,
                "load_state": item.load_quantile,
                "day_ahead_state": item.day_ahead_quantile,
                "spread_state": item.spread_quantile,
            }
            for item in scenarios
        ],
        "layer": "L3",
        "objective_definition": "L3 = 锁定合同成本 + 日前净买卖 + 实时补救 + 偏差回收 + 交易费 + 储能净负荷影响",
        "scenario_probabilities": list(probabilities),
        "scenario_costs_yuan": [round(value, 2) for value in scenario_costs],
        "expected_cost_yuan": round(expected, 2),
        "tail_cost_yuan": round(tail, 2),
        "cvar95_yuan": round(tail, 2),
        "objective_yuan": round(objective, 2),
        "rolling_cfd_yuan": round(rolling_cfd, 2),
        "storage_degradation_yuan": round(degradation, 2),
        "declaration_slack_penalty_yuan": round(slack_penalty, 2),
        "l1_contract_cfd_yuan": round(
            _daily_contract_cfd(fills, day_ahead_prices, month_factor, ("ANNUAL", "MONTHLY", "TEN_DAY")),
            2,
        ),
        "storage_charge_mwh": round(sum(float(row["charge_from_grid_mwh"]) for row in storage["rows"]), 3),
        "storage_discharge_mwh": round(sum(float(row["discharge_to_load_mwh"]) for row in storage["rows"]), 3),
        "assessment_guard": declaration_breakdown.get("assessment_guard", {}),
    }


def _wholesale_settlement(
    portfolio: Mapping[str, object],
    actual_month: Sequence[float],
    declaration_month: Sequence[float],
    day_ahead_prices: Sequence[float],
    real_time_prices: Sequence[float],
    execution_rows: Sequence[Mapping[str, object]],
    month_factor: float,
) -> dict:
    fills: Sequence[ContractFill] = portfolio["fills"]
    physical_energy_cost = sum(
        declared * da
        + month_factor
        * (float(row["real_time_buy_mwh"]) - float(row["real_time_sell_mwh"]))
        * rt
        for declared, da, rt, row in zip(
            declaration_month, day_ahead_prices, real_time_prices, execution_rows
        )
    )
    day_ahead_map = {
        "P%02d" % (index + 1): 0.5
        * (float(day_ahead_prices[2 * index]) + float(day_ahead_prices[2 * index + 1]))
        for index in range(48)
    }
    contract_cfd = contract_difference_cost(fills, day_ahead_map)
    trade_fee = sum(fill.fee for fill in fills)
    actual_total = sum(actual_month)
    declared_total = sum(declaration_month)
    da_reference = load_weighted_price(actual_month, day_ahead_prices)
    rt_reference = load_weighted_price(actual_month, real_time_prices)
    da_recovery = month_factor * sum(
        day_ahead_recovery(
            float(row["grid_load_after_storage_mwh"]),
            float(row["declared_mwh"]),
            float(da),
            float(rt),
        )
        for row, da, rt in zip(execution_rows, day_ahead_prices, real_time_prices)
    )
    actual_assessment_curve = aggregate_quarter_hour_energy(actual_month)
    annual_assessment_curve, overall_assessment_curve = _assessment_curves(fills)
    annual_point_ratios = [
        contract / max(actual, 1e-9)
        for contract, actual in zip(annual_assessment_curve, actual_assessment_curve)
    ]
    overall_point_ratios = [
        contract / max(actual, 1e-9)
        for contract, actual in zip(overall_assessment_curve, actual_assessment_curve)
    ]
    annual_recovery = sum(
        annual_ratio_recovery(actual, contract, 405.0, 418.0)
        for actual, contract in zip(
            actual_assessment_curve, annual_assessment_curve
        )
    )
    ratio_recovery = sum(
        over_profit_recovery(actual, contract, 418.0, rt_reference)
        for actual, contract in zip(
            actual_assessment_curve, overall_assessment_curve
        )
    )
    bill = settle_month(
        SettlementInputs(
            energy_cost=physical_energy_cost + contract_cfd,
            trade_fee=trade_fee,
            day_ahead_recovery=da_recovery,
            annual_recovery=annual_recovery,
            over_profit_recovery=ratio_recovery,
        )
    )
    return {
        "physical_energy_cost_yuan": _money(physical_energy_cost),
        "contract_difference_yuan": _money(contract_cfd),
        "trade_fee_yuan": _money(trade_fee),
        "day_ahead_recovery_yuan": _money(da_recovery),
        "annual_recovery_yuan": _money(annual_recovery),
        "contract_ratio_recovery_yuan": _money(ratio_recovery),
        "annual_aggregate_ratio": round(float(portfolio["annual_position_mwh"]) / max(actual_total, 1e-9), 4),
        "annual_assessment_rule": "LOWER_ONLY_60_PERCENT",
        "annual_point_ratios": [round(value, 6) for value in annual_point_ratios],
        "annual_compliant_periods": sum(value >= 0.60 - 1e-9 for value in annual_point_ratios),
        "overall_aggregate_ratio": round(
            float(portfolio["overall_position_mwh"]) / max(actual_total, 1e-9), 4
        ),
        "overall_assessment_band": [0.90, 1.10],
        "overall_point_ratios": [round(value, 6) for value in overall_point_ratios],
        "overall_compliant_periods": sum(
            0.90 - 1e-9 <= value <= 1.10 + 1e-9
            for value in overall_point_ratios
        ),
        "long_term_assessment_granularity": "48_HALF_HOUR_PERIODS",
        "aggregate_ratio_role": "DIAGNOSTIC_ONLY_NOT_ASSESSMENT",
        "wholesale_total_yuan": _money(bill.wholesale_total),
        "cost_breakdown": {key: _money(value) for key, value in bill.to_dict().items()},
    }


def _forecast_history(snapshots: Sequence[dict], selected_event: str, rt_period: int) -> List[dict]:
    if selected_event == "MONTH_END":
        available_through = len(snapshots) - 1
    elif selected_event == "REAL_TIME":
        available_through = 5 + rt_period
    elif selected_event == "D-1_PRICE":
        available_through = 5
    else:
        available_through = EVENT_ORDER.index(selected_event)
    result: List[dict] = []
    previous_p50 = None
    previous_mape = None
    for index, snapshot in enumerate(snapshots):
        available = index <= available_through
        if not available:
            result.append(
                {
                    "sequence": index + 1,
                    "snapshot_id": snapshot["snapshot_id"],
                    "event": snapshot["event"],
                    "label": snapshot["label"],
                    "published_at": snapshot["published_at"],
                    "available": False,
                    "p10_total_mwh": None,
                    "p50_total_mwh": None,
                    "p90_total_mwh": None,
                    "actual_total_mwh": None,
                    "p50_change_mwh": None,
                    "p50_error_mwh": None,
                    "mae_mwh": None,
                    "wape": None,
                    "mape": None,
                    "percentage_error_metric": "WAPE",
                    "accuracy_percent": None,
                    "accuracy_gain_points": None,
                    "interval_width_mwh": None,
                    "realized_periods": 0,
                }
            )
            continue
        public = public_snapshot(
            snapshot,
            reveal_all_actual=selected_event == "MONTH_END",
            realized_limit=(
                rt_period
                if selected_event == "REAL_TIME" and available
                else 0
            ),
        )
        quality = public["quality"]
        p50 = float(quality["p50_total_mwh"])
        wape = None if quality["wape"] is None else float(quality["wape"])
        actual_total = quality["actual_total_mwh"]
        result.append(
            {
                "sequence": index + 1,
                "snapshot_id": snapshot["snapshot_id"],
                "event": snapshot["event"],
                "label": snapshot["label"],
                "published_at": snapshot["published_at"],
                "available": available,
                "p10_total_mwh": quality["p10_total_mwh"],
                "p50_total_mwh": quality["p50_total_mwh"],
                "p90_total_mwh": quality["p90_total_mwh"],
                "actual_total_mwh": quality["actual_total_mwh"],
                "p50_change_mwh": None if previous_p50 is None else round(p50 - previous_p50, 3),
                "p50_error_mwh": (
                    None
                    if actual_total is None
                    else round(p50 - float(actual_total), 3)
                ),
                "mae_mwh": quality["mae_mwh"],
                "wape": wape,
                "mape": wape,
                "percentage_error_metric": "WAPE",
                "accuracy_percent": (
                    None if wape is None else round((1.0 - wape) * 100.0, 2)
                ),
                "accuracy_gain_points": (
                    None
                    if previous_mape is None or wape is None
                    else round((previous_mape - wape) * 100.0, 2)
                ),
                "interval_width_mwh": quality["interval_width_mwh"],
                "realized_periods": quality["realized_periods"],
            }
        )
        previous_p50 = p50
        if wape is not None:
            previous_mape = wape
    return result


def _timeline(
    event: str,
    optimized_actions: Sequence[Mapping[str, object]],
    forecast_history: Sequence[Mapping[str, object]],
    rt_period: int,
) -> List[dict]:
    selected_index = _event_index(event)
    action_map = {str(action["event"]): action for action in optimized_actions}
    history_map = {
        str(item["event"]): item
        for item in forecast_history
        if item["event"] != "REAL_TIME"
    }
    history_map["D-1_PRICE"] = history_map["D-1"]
    history_map["REAL_TIME"] = next(
        item for item in forecast_history if item["label"] == "RT-%02d" % rt_period
    )
    result: List[dict] = []
    for index, item in enumerate(EVENT_ORDER):
        if index > selected_index:
            summary = "待到达后重算"
        elif item in action_map:
            action = action_map[item]
            if action["side"] == "HOLD":
                summary = "不交易"
            else:
                summary = "%s %.0f MWh" % ("买入" if action["side"] == "BUY" else "卖出", action["quantity_mwh"])
        elif item == "D-1":
            summary = "96 点申报锁定"
        elif item == "D-1_PRICE":
            summary = "17:30日前价揭示"
        elif item == "REAL_TIME":
            summary = "15 分钟滚动执行"
        else:
            summary = "正式复算"
        result.append(
            {
                "event": item,
                "label": EVENT_LABELS[item],
                "layer": (
                    "L1" if item in L1_EVENTS
                    else "L2" if item in L2_NEAR_TERM_EVENTS
                    else "L3" if item in {"D-1", "D-1_PRICE", "REAL_TIME"}
                    else "结算"
                ),
                "action": summary,
                "state": "completed" if index < selected_index else "active" if index == selected_index else "future",
                "published_at": (
                    "2026-10-05T12:00:00+08:00"
                    if item == "MONTH_END"
                    else history_map[item]["published_at"]
                ),
                "p50_total_mwh": (
                    None
                    if item == "MONTH_END" or not history_map[item]["available"]
                    else history_map[item]["p50_total_mwh"]
                ),
                "p50_change_mwh": (
                    None
                    if item == "MONTH_END" or not history_map[item]["available"]
                    else history_map[item]["p50_change_mwh"]
                ),
                "accuracy_percent": (
                    None
                    if item == "MONTH_END" or not history_map[item]["available"]
                    else history_map[item]["accuracy_percent"]
                ),
                "monthly_equivalent_p50_mwh": (
                    None
                    if item not in {"ANNUAL", "MONTHLY", "TEN_DAY"}
                    or not history_map[item]["available"]
                    else history_map[item]["monthly_equivalent_p50_mwh"]
                ),
            }
        )
    return result


def _display_actions(actions: Sequence[Mapping[str, object]], selected_event: str) -> List[dict]:
    selected_index = _event_index(selected_event)
    result: List[dict] = []
    for action in actions:
        if _event_index(str(action["event"])) <= selected_index:
            result.append({**action, "state": "POSTED"})
        else:
            result.append(
                {
                    "event": action["event"],
                    "state": "FUTURE",
                    "side": None,
                    "quantity_mwh": None,
                    "target_position_mwh": None,
                    "resulting_position_mwh": None,
                    "execution_price_yuan_per_mwh": None,
                    "reason": "该节点尚未到达，不能使用未来预测生成动作",
                }
            )
    return result


def _current_action(
    event: str,
    optimized_actions: Sequence[Mapping[str, object]],
    declaration: Mapping[str, object],
    storage: Mapping[str, object],
    margin: float,
) -> dict:
    if event in TRADE_EVENTS:
        action = next(action for action in optimized_actions if action["event"] == event)
        label = (
            "不交易" if action["side"] == "HOLD"
            else "买卖双向滚撮" if action["side"] == "MIXED"
            else "买入合同" if action["side"] == "BUY" else "卖出合同"
        )
        return {
            "action_type": "TRADE",
            "label": label,
            "side": action["side"],
            "quantity_mwh": (
                float(action.get("buy_quantity_mwh", 0.0))
                + float(action.get("sell_quantity_mwh", 0.0))
                if action["side"] == "MIXED"
                else action["quantity_mwh"]
            ),
            "buy_quantity_mwh": action.get("buy_quantity_mwh", 0.0),
            "sell_quantity_mwh": action.get("sell_quantity_mwh", 0.0),
            "net_quantity_mwh": action.get("net_quantity_mwh", action["quantity_mwh"]),
            "reason": action["reason"],
        }
    if event == "D-1":
        return {
            "action_type": "DAY_AHEAD",
            "label": "提交并锁定96点日前电量曲线",
            "side": "SUBMIT",
            "quantity_mwh": declaration["total_mwh"],
            "reason": "逐时段在 P10-P90 与有利偏差 ±10% 区间内选择风险调整申报量",
        }
    if event == "D-1_PRICE":
        return {
            "action_type": "PRICE_REVEAL",
            "label": "接收17:30真实日前价格并重算储能计划",
            "side": "REOPTIMIZE",
            "quantity_mwh": 0.0,
            "reason": "日前报量已锁定；真实日前价格只更新结算基准和后续储能条件计划",
        }
    if event == "REAL_TIME":
        current = storage["current"]
        if storage.get("solver_status") in {"OPTIMAL", "LIMIT_REACHED"}:
            if current["mode"] == "CHARGE":
                reason = "L3 MILP联合未来价格、SOC、效率和终端电量约束，当前执行充电"
            elif current["mode"] == "DISCHARGE":
                reason = "L3 MILP联合未来价格、SOC、效率和终端电量约束，当前执行放电"
            else:
                reason = "L3 MILP判断当前充放电收益不足，保留SOC等待后续时段"
        elif current["mode"] == "CHARGE":
            reason = "当前实时价 %.0f 元/MWh 位于低价充电区间（≤%.0f）" % (
                current["real_time_price"],
                storage["charge_price_threshold"],
            )
        elif current["mode"] == "DISCHARGE":
            reason = "当前实时价 %.0f 元/MWh 位于高价放电区间（≥%.0f）" % (
                current["real_time_price"],
                storage["discharge_price_threshold"],
            )
        else:
            if current["real_time_price"] >= storage["discharge_price_threshold"]:
                reason = "当前处于高价窗口，但SOC已到安全下限；此前高价时段已经完成放电"
            elif current["real_time_price"] <= storage["charge_price_threshold"]:
                reason = "当前处于低价窗口，但SOC已接近容量上限；本时段不再继续充电"
            else:
                reason = "当前价格位于充放电阈值之间，保留SOC等待更大的日内价差"
        return {
            "action_type": "RESOURCE",
            "label": {"CHARGE": "储能充电", "DISCHARGE": "储能放电", "IDLE": "储能保持空闲"}[current["mode"]],
            "side": current["mode"],
            "quantity_mwh": abs(current["storage_action_mwh"]),
            "reason": reason,
        }
    return {
        "action_type": "SETTLEMENT",
        "label": "月末正式口径复算",
        "side": "SETTLE",
        "quantity_mwh": 0.0,
        "reason": "零售收入减批发购电、回收费用和交易费，当前净毛利 %.0f 元" % margin,
    }


def simulate(
    event: str = "D-1",
    risk: float = 0.25,
    volatility: str = "high",
    annual_coverage: float = 0.80,
    monthly_coverage: float = 0.95,
    ten_day_coverage: float = 0.98,
    d3_coverage: float = 1.00,
    d2_coverage: float = 1.00,
    target_quantile: str = "P50",
    minimum_edge: float = 2.0,
    deadband_ratio: float = 0.005,
    maximum_adjustment: float = 8000.0,
    allow_sell: bool = True,
    rolling_interval_limit_ratio: float = 0.20,
    rolling_daily_limit_ratio: float = 0.05,
    rolling_sell_basis: str = "P50",
    rolling_user_buy_ceiling: float = 1000.0,
    rolling_user_sell_floor: float = 0.0,
    rolling_price_edge: float = 100.0,
    rolling_min_fill_ratio: float = 0.10,
    rolling_max_fill_ratio: float = 0.20,
    retail_pricing_source: str = "MOCK_ASSUMPTION",
    package_type: str = "LINKED",
    cap_price: float = 490.0,
    service_fee: float = 18.0,
    fixed_price: float = 455.0,
    share_ratio: float = 0.50,
    retail_annual_weight: float = 0.70,
    retail_monthly_weight: float = 0.20,
    retail_spot_weight: float = 0.10,
    rt_period: int = 76,
    require_milp: bool = True,
    diagnostic_mode: bool = False,
    load_scenario_seed: int = DEFAULT_LOAD_SCENARIO_SEED,
    price_scenario_seed: int = DEFAULT_PRICE_SCENARIO_SEED,
    actual_load_overrides: Sequence[object] = (),
    actual_real_time_price_overrides: Sequence[object] = (),
    progress_logger: Optional[Callable[[str], None]] = None,
    cvar_enabled: bool = True,
    annual_price: float = 405.0,
    monthly_price: float = 416.0,
    ten_day_price: float = 424.0,
    rolling_price_edge_lower: float = 100.0,
    rolling_price_edge_upper: float = 100.0,
) -> dict:
    trace = _SimulationTrace(progress_logger, event)
    _event_index(event)
    if not 0.0 <= risk <= 1.0:
        raise ValueError("risk 必须位于 [0, 1]")
    if not cvar_enabled:
        risk = 0.0
    if not 1 <= rt_period <= SPOT_PERIODS:
        raise ValueError("rt_period 必须位于 [1, 96]")
    if require_milp and diagnostic_mode:
        raise ValueError("require_milp=true 与 diagnostic_mode=true 不能同时使用")
    allow_diagnostic_policy = diagnostic_mode and not require_milp
    if (
        rolling_price_edge != 100.0
        and rolling_price_edge_lower == 100.0
        and rolling_price_edge_upper == 100.0
    ):
        # Preserve the former symmetric API for existing callers.
        rolling_price_edge_lower = rolling_price_edge
        rolling_price_edge_upper = rolling_price_edge
    park_config = IndustrialParkConfig()
    trader_config = TraderConfig(
        annual_price_yuan_per_mwh=annual_price,
        monthly_price_yuan_per_mwh=monthly_price,
        ten_day_price_yuan_per_mwh=ten_day_price,
        annual_coverage=annual_coverage,
        monthly_coverage=monthly_coverage,
        ten_day_coverage=ten_day_coverage,
        d3_coverage=d3_coverage,
        d2_coverage=d2_coverage,
        target_quantile=target_quantile,
        minimum_edge_yuan_per_mwh=minimum_edge,
        deadband_ratio=deadband_ratio,
        maximum_node_adjustment_mwh=maximum_adjustment,
        allow_sell=allow_sell,
        rolling_interval_limit_ratio=rolling_interval_limit_ratio,
        rolling_daily_limit_ratio=rolling_daily_limit_ratio,
        rolling_sell_basis=rolling_sell_basis,
        rolling_user_buy_ceiling_yuan_per_mwh=rolling_user_buy_ceiling,
        rolling_user_sell_floor_yuan_per_mwh=rolling_user_sell_floor,
        rolling_price_edge_lower_yuan_per_mwh=rolling_price_edge_lower,
        rolling_price_edge_upper_yuan_per_mwh=rolling_price_edge_upper,
        rolling_min_fill_ratio=rolling_min_fill_ratio,
        rolling_max_fill_ratio=rolling_max_fill_ratio,
    )
    retail_config = RetailMarketConfig(
        pricing_source=retail_pricing_source,
        package_type=package_type,
        cap_price_yuan_per_mwh=cap_price,
        service_fee_yuan_per_mwh=service_fee,
        fixed_price_yuan_per_mwh=fixed_price,
        share_ratio=share_ratio,
        annual_weight=retail_annual_weight,
        monthly_weight=retail_monthly_weight,
        spot_weight=retail_spot_weight,
    )
    trace.mark("参数校验和配置构建")
    snapshots = build_forecast_snapshots(
        park_config,
        volatility,
        load_scenario_seed,
        actual_load_overrides,
    )
    spot_forecasts = snapshot_index(snapshots)
    forecasts = {
        key: aggregate_spot_snapshot(value)
        for key, value in spot_forecasts.items()
        if key in TRADE_EVENTS
    }
    price_forecasts = build_price_forecasts()
    rolling_order_books = {
        event_key: build_rolling_order_book(event_key, price_forecasts[event_key])
        for event_key in L2_NEAR_TERM_EVENTS
    }
    prices = price_curves(price_scenario_seed, actual_real_time_price_overrides)
    prices["annual"] = [float(annual_price)] * SPOT_PERIODS
    prices["monthly"] = [float(monthly_price)] * SPOT_PERIODS
    prices["ten_day"] = [float(ten_day_price)] * SPOT_PERIODS
    actual_day = [
        float(row["actual_mwh"])
        for row in spot_forecasts["D-1"]["rows"]
    ]
    trace.mark("负荷、价格和滚撮订单数据准备")
    selected_event = (
        "D-1" if event == "D-1_PRICE" else "REAL_TIME" if event == "MONTH_END" else event
    )
    selected_snapshot = select_snapshot(
        snapshots,
        selected_event,
        SPOT_PERIODS if event == "MONTH_END" else rt_period,
    )
    if event == "MONTH_END":
        selected_snapshot = _fully_realized_snapshot(selected_snapshot, actual_day)
    annual_forecast_curve = [float(row["p50_mwh"]) for row in forecasts["ANNUAL"]["rows"]]
    expected_spot = {}
    for trade_event in TRADE_EVENTS:
        if trade_event in {"D-3", "D-2"}:
            price_key = trade_event
            da_curve = [row["day_ahead_p50"] for row in price_forecasts[price_key]["rows"]]
            expected_spot[trade_event] = load_weighted_price(
                expand_half_hour_energy(annual_forecast_curve), da_curve
            )
        else:
            expected_spot[trade_event] = 430.0
    near_term_joint_scenarios = {
        trade_event: reduced_joint_trajectories(
            spot_forecasts[trade_event],
            price_forecasts[trade_event],
            L3_OPTIMIZATION_SCENARIO_COUNT,
        )
        for trade_event in L2_NEAR_TERM_EVENTS
    }
    trace.mark("近端100条联合轨迹生成")

    def near_term_valuation_builder(
        trade_event: str, locked_fills: Sequence[ContractFill]
    ) -> Mapping[str, object]:
        load_snapshot = spot_forecasts[trade_event]
        price_snapshot = price_forecasts[trade_event]
        execution = [float(row["p50_mwh"]) for row in load_snapshot["rows"]]
        contract_curve = _daily_locked_contract_curve(locked_fills)
        contract_cost = _daily_contract_fixed_cost(locked_fills)
        valuation_path = _solve_l3_path(
            load_snapshot,
            price_snapshot,
            execution,
            risk,
            park_config.physical_peak_mw,
            [float(row["day_ahead_p50"]) for row in price_snapshot["rows"]],
            [float(row["real_time_p50"]) for row in price_snapshot["rows"]],
            1,
            False,
            contract_curve,
            contract_cost,
            allow_diagnostic_policy=allow_diagnostic_policy,
        )
        trace.l3("%s滚撮后L3估值" % trade_event, valuation_path)
        optimization = dict(
            valuation_path.get("optimization", {})
            if valuation_path.get("milp") is not None
            else {}
        )
        optimization["locked_contract_total_mwh"] = round(sum(contract_curve), 6)
        return optimization
    baseline_all = build_portfolio(
        forecasts,
        trader_config,
        park_config,
        expected_spot,
        risk,
        "BASELINE",
        order_books=rolling_order_books,
    )
    trace.mark("交易员基线组合构建")
    optimized_all = build_portfolio(
        forecasts,
        trader_config,
        park_config,
        expected_spot,
        risk,
        "OPTIMIZED",
        near_term_joint_scenarios,
        allow_diagnostic_policy,
        rolling_order_books,
        near_term_valuation_builder=near_term_valuation_builder,
    )
    trace.mark("优化组合、L1和滚撮估值完成")
    portfolio_event = "D-1" if event == "D-1_PRICE" else event
    baseline = _visible_portfolio(baseline_all, portfolio_event)
    optimized = _visible_portfolio(optimized_all, portfolio_event)
    d1_snapshot = spot_forecasts["D-1"]
    declaration_snapshot = selected_snapshot if _event_index(event) < _event_index("D-1") else d1_snapshot
    active_price_snapshot = _price_forecast_for_event(
        event, price_forecasts, prices, rt_period
    )
    decision_day_ahead = [
        float(row["day_ahead_p50"]) for row in active_price_snapshot["rows"]
    ]
    decision_real_time = [
        float(row["real_time_p50"]) for row in active_price_snapshot["rows"]
    ]
    if event == "MONTH_END":
        decision_day_ahead = list(prices["day_ahead"])
        decision_real_time = list(prices["real_time"])
    factor = month_equivalent_days(park_config)
    locked_contract_curve = _daily_locked_contract_curve(optimized["fills"])
    locked_contract_cost = _daily_contract_fixed_cost(optimized["fills"])
    execution_load = [
        float(row["p50_mwh"]) for row in declaration_snapshot["rows"]
    ]
    base_d1_path = None
    if _event_index(event) > _event_index("D-1"):
        base_d1_load = [float(row["p50_mwh"]) for row in d1_snapshot["rows"]]
        base_d1_path = _solve_l3_path(
            d1_snapshot,
            price_forecasts["D-1"],
            base_d1_load,
            risk,
            park_config.physical_peak_mw,
            [row["day_ahead_p50"] for row in price_forecasts["D-1"]["rows"]],
            [row["real_time_p50"] for row in price_forecasts["D-1"]["rows"]],
            1,
            True,
            locked_contract_curve,
            locked_contract_cost,
            allow_diagnostic_policy=allow_diagnostic_policy,
        )
        trace.l3("D-1锁定申报基准", base_d1_path)
        declaration_snapshot = selected_snapshot
        execution_load = [
            float(row["p50_mwh"]) for row in selected_snapshot["rows"]
        ]
        locked_values = [
            float(row["declared_mwh"])
            for row in base_d1_path["declaration"]["rows"]
        ]
        fixed_until = (
            SPOT_PERIODS
            if event == "MONTH_END"
            else max(0, rt_period - 1)
            if event == "REAL_TIME"
            else 0
        )
        l3_path = _solve_l3_path(
            declaration_snapshot,
            active_price_snapshot,
            execution_load,
            risk,
            park_config.physical_peak_mw,
            decision_day_ahead,
            decision_real_time,
            SPOT_PERIODS if event == "MONTH_END" else rt_period,
            True,
            locked_contract_curve,
            locked_contract_cost,
            locked_values,
            base_d1_path["storage"]["rows"],
            fixed_until,
            allow_diagnostic_policy,
        )
        trace.l3("当前事件滚动路径", l3_path)
    else:
        l3_path = _solve_l3_path(
            declaration_snapshot,
            active_price_snapshot,
            execution_load,
            risk,
            park_config.physical_peak_mw,
            decision_day_ahead,
            decision_real_time,
            rt_period,
            _event_index(event) >= _event_index("D-1"),
            locked_contract_curve,
            locked_contract_cost,
            allow_diagnostic_policy=allow_diagnostic_policy,
        )
        trace.l3("当前事件L3路径", l3_path)
    trace.mark("L3日前申报、实时偏差和储能路径求解")
    l3_milp_run = l3_path["milp"]
    l3_milp_error = l3_path["error"]
    declaration = l3_path["declaration"]
    storage = l3_path["storage"]
    declaration_breakdown = _declaration_breakdown(
        optimized["fills"], declaration, execution_load, factor, storage
    )
    trace.mark("申报拆解和能量闭合")
    actual_month = monthly_curve(actual_day, park_config)
    planning_day = (
        actual_day
        if event == "MONTH_END"
        else [float(row["p50_mwh"]) for row in selected_snapshot["rows"]]
    )
    planning_month = monthly_curve(planning_day, park_config)
    declaration_month = monthly_curve([row["declared_mwh"] for row in declaration["rows"]], park_config)
    retail = retail_settlement(
        planning_month,
        prices["annual"],
        prices["monthly"],
        decision_real_time,
        retail_config,
    )
    baseline_bill = _wholesale_settlement(
        baseline,
        planning_month,
        declaration_month,
        decision_day_ahead,
        decision_real_time,
        storage["rows"],
        factor,
    )
    optimized_bill = _wholesale_settlement(
        optimized,
        planning_month,
        declaration_month,
        decision_day_ahead,
        decision_real_time,
        storage["rows"],
        factor,
    )
    trace.mark("零售和批发结算")
    baseline_margin = float(retail["revenue_yuan"]) - baseline_bill["wholesale_total_yuan"]
    optimized_margin = float(retail["revenue_yuan"]) - optimized_bill["wholesale_total_yuan"]
    margin_improvement = optimized_margin - baseline_margin
    forecast_history = _forecast_history(snapshots, event, rt_period)
    convergence = [
        {
            "event": item["label"],
            "p10_total_mwh": item["p10_total_mwh"],
            "p50_total_mwh": item["p50_total_mwh"],
            "p90_total_mwh": item["p90_total_mwh"],
            "actual_total_mwh": item["actual_total_mwh"],
            "mae_mwh": item["mae_mwh"],
            "wape": item["wape"],
            "mape": item["mape"],
            "interval_width_mwh": item["interval_width_mwh"],
        }
        for item in forecast_history
        if item["available"]
        and (item["event"] != "REAL_TIME" or item["label"] == "RT-%02d" % rt_period)
    ]
    for item in forecast_history:
        item["monthly_equivalent_p50_mwh"] = (
            None
            if item["p50_total_mwh"] is None
            else round(float(item["p50_total_mwh"]) * factor, 3)
        )
    forecast_phases = []
    for index, snapshot in enumerate(snapshots):
        public = public_snapshot(
            snapshot,
            reveal_all_actual=event == "MONTH_END",
            realized_limit=(
                rt_period
                if event == "REAL_TIME" and forecast_history[index]["available"]
                else 0
            ),
        )
        if not forecast_history[index]["available"]:
            public["rows"] = []
            public["quality"] = {
                "p10_total_mwh": None,
                "p50_total_mwh": None,
                "p90_total_mwh": None,
                "actual_total_mwh": None,
                "mae_mwh": None,
                "wape": None,
                "mape": None,
                "percentage_error_metric": "WAPE",
                "interval_width_mwh": None,
                "quality_scope": "NOT_AVAILABLE",
                "realized_periods": 0,
            }
        forecast_phases.append(
            {
            "snapshot_id": snapshot["snapshot_id"],
            "event": snapshot["event"],
            "label": snapshot["label"],
            "published_at": snapshot["published_at"],
            "rt_period": snapshot.get("rt_period", 0),
            "available": forecast_history[index]["available"],
            "quality": public["quality"],
            "rows": public["rows"],
            }
        )
    baseline_action_map = {action["event"]: action for action in baseline_all["actions"]}
    optimized_action_map = {action["event"]: action for action in optimized_all["actions"]}
    for item in forecast_history:
        if not item["available"]:
            item["decision"] = None
            continue
        if item["event"] in TRADE_EVENTS:
            baseline_action = baseline_action_map[item["event"]]
            optimized_action = optimized_action_map[item["event"]]
            item["baseline_decision"] = {
                "side": baseline_action["side"],
                "quantity_mwh": baseline_action["quantity_mwh"],
                "position_mwh": baseline_action["resulting_position_mwh"],
            }
            item["optimized_decision"] = {
                "side": optimized_action["side"],
                "quantity_mwh": optimized_action["quantity_mwh"],
                "position_mwh": optimized_action["resulting_position_mwh"],
            }
            item["decision"] = "%s %.1f MWh" % (
                "买卖双向" if optimized_action["side"] == "MIXED" else "买入" if optimized_action["side"] == "BUY" else "卖出" if optimized_action["side"] == "SELL" else "不交易",
                optimized_action["quantity_mwh"],
            )
        elif item["event"] == "D-1":
            item["decision"] = "申报 %.1f MWh" % declaration["total_mwh"]
        else:
            resource = storage["rows"][int(item["realized_periods"]) - 1]
            item["decision"] = "%s %.1f MWh" % (
                {"CHARGE": "充电", "DISCHARGE": "放电", "IDLE": "空闲"}[resource["mode"]],
                abs(resource["storage_action_mwh"]),
            )
    trace.mark("预测历史和前端曲线数据")
    portfolio_payload = {
        "config": trader_config.to_dict(),
        "baseline": {
            "position_mwh": round(baseline["position_mwh"], 3),
            "position_unit": baseline["position_unit"],
            "annual_position_mwh": round(baseline["annual_position_mwh"], 3),
            "overall_position_mwh": round(baseline["overall_position_mwh"], 3),
            "rolling_sell_used_mwh": round(baseline["rolling_sell_used_mwh"], 3),
            "rolling_sell_cap_mwh": round(baseline["rolling_sell_cap_mwh"], 3),
            "actions": _display_actions(baseline_all["actions"], event),
            "fills": [asdict(fill) for fill in baseline["fills"]],
            "optimization": baseline.get("optimization", {}),
        },
        "optimized": {
            "position_mwh": round(optimized["position_mwh"], 3),
            "position_unit": optimized["position_unit"],
            "annual_position_mwh": round(optimized["annual_position_mwh"], 3),
            "overall_position_mwh": round(optimized["overall_position_mwh"], 3),
            "rolling_sell_used_mwh": round(optimized["rolling_sell_used_mwh"], 3),
            "rolling_sell_cap_mwh": round(optimized["rolling_sell_cap_mwh"], 3),
            "actions": _display_actions(optimized_all["actions"], event),
            "fills": [asdict(fill) for fill in optimized["fills"]],
            "optimization": optimized.get("optimization", {}),
        },
    }
    selected_order_book = rolling_order_books.get(event, {}) if event in ROLLING_EVENTS else {}
    selected_optimized_action = next(
        (item for item in optimized_all["actions"] if item["event"] == event), None
    )
    selected_decisions = (
        [dict(item) for item in selected_optimized_action.get("order_decisions", [])]
        if selected_optimized_action is not None
        else []
    )
    decisions_by_id = {
        str(item.get("order_id")): item for item in selected_decisions
    }
    portfolio_payload["rolling_orders"] = {
        "selected_event": event if event in ROLLING_EVENTS else None,
        "orders": [
            {
                **dict(order),
                **dict(decisions_by_id.get(str(order["order_id"]), {})),
                "accepted_quantity_mwh": round(
                    float(decisions_by_id.get(str(order["order_id"]), {}).get("accepted_quantity_mwh", 0.0)),
                    6,
                ),
                "accepted": bool(
                    decisions_by_id.get(str(order["order_id"]), {}).get("accepted", False)
                ),
            }
            for order in selected_order_book.get("orders", [])
        ],
        "order_count": int(selected_order_book.get("order_count", 0)),
        "note": "订单价相对实时现货P50预测价达到买入下限%.0f、卖出上限%.0f元/MWh后被捕捉，实际成交订单量的%.0f%%-%.0f%%；成交后写入不可撤回ContractFill并重算L3。"
        % (
            trader_config.price_edge_lower_yuan_per_mwh,
            trader_config.price_edge_upper_yuan_per_mwh,
            100.0 * trader_config.rolling_min_fill_ratio,
            100.0 * trader_config.rolling_max_fill_ratio,
        ),
    }
    all_rolling_decisions = [
        decision
        for action in optimized_all["actions"]
        if action["event"] in ROLLING_EVENTS
        for decision in action.get("order_decisions", [])
    ]
    portfolio_payload["rolling_rule"] = {
        "sell_allowed_events": list(ROLLING_EVENTS),
        "sell_forbidden_events": ["ANNUAL", "MONTHLY", "TEN_DAY"],
        "price_reference": "REAL_TIME_P50_FORECAST",
        # Retain the legacy symmetric field for API consumers; new clients
        # should use the explicit lower/upper values below.
        "price_edge_yuan_per_mwh": trader_config.price_edge_lower_yuan_per_mwh,
        "price_edge_lower_yuan_per_mwh": trader_config.price_edge_lower_yuan_per_mwh,
        "price_edge_upper_yuan_per_mwh": trader_config.price_edge_upper_yuan_per_mwh,
        "min_fill_ratio": trader_config.rolling_min_fill_ratio,
        "max_fill_ratio": trader_config.rolling_max_fill_ratio,
        "position_lower_ratio": trader_config.rolling_position_lower_ratio,
        "position_upper_ratio": trader_config.rolling_position_upper_ratio,
        "l3_marginal_gate_enabled": trader_config.rolling_l3_marginal_gate,
        "l3_min_net_benefit_yuan_per_mwh": trader_config.rolling_l3_min_net_benefit_yuan_per_mwh,
        "l3_gate_available": any(
            bool(item.get("l3_gate_available")) for item in all_rolling_decisions
        ),
        "l3_gate_rejected_order_count": sum(
            item.get("rejection_code") == "L3_MARGINAL_VALUE_INSUFFICIENT"
            for item in all_rolling_decisions
        ),
        "captured_order_count": sum(
            bool(item.get("price_triggered")) for item in all_rolling_decisions
        ),
        "executed_order_count": sum(
            bool(item.get("accepted")) for item in all_rolling_decisions
        ),
        "executed_buy_mwh": round(
            sum(
                float(item.get("accepted_quantity_mwh", 0.0))
                for item in all_rolling_decisions
                if item.get("our_side") == "BUY"
            ),
            6,
        ),
        "executed_sell_mwh": round(
            sum(
                float(item.get("accepted_quantity_mwh", 0.0))
                for item in all_rolling_decisions
                if item.get("our_side") == "SELL"
            ),
            6,
        ),
    }
    assessment_curve = aggregate_quarter_hour_energy(planning_month)
    assessment_energy = max(sum(assessment_curve), 1e-9)
    annual_curve, overall_curve = _assessment_curves(optimized["fills"])
    annual_point_ratios = [
        contract / max(demand, 1e-9)
        for contract, demand in zip(annual_curve, assessment_curve)
    ]
    overall_point_ratios = [
        contract / max(demand, 1e-9)
        for contract, demand in zip(overall_curve, assessment_curve)
    ]
    annual_compliant = [ratio >= 0.60 - 1e-9 for ratio in annual_point_ratios]
    overall_compliant = [
        0.90 - 1e-9 <= ratio <= 1.10 + 1e-9
        for ratio in overall_point_ratios
    ]
    annual_ratio_value = sum(annual_curve) / assessment_energy
    overall_ratio_value = sum(overall_curve) / assessment_energy
    period_rows = [
        {
            "period": index + 1,
            "time": spot_time(index * 2 + 1),
            "assessment_demand_mwh": round(assessment_curve[index], 6),
            "annual_contract_mwh": round(annual_curve[index], 6),
            "annual_ratio": round(annual_point_ratios[index], 6),
            "annual_compliant": annual_compliant[index],
            "overall_contract_mwh": round(overall_curve[index], 6),
            "overall_ratio": round(overall_point_ratios[index], 6),
            "overall_compliant": overall_compliant[index],
        }
        for index in range(48)
    ]
    annual_status = (
        "PASS" if all(annual_compliant) else "BELOW_LOWER"
    )
    has_overall_under = any(ratio < 0.90 for ratio in overall_point_ratios)
    has_overall_over = any(ratio > 1.10 for ratio in overall_point_ratios)
    overall_status = (
        "PASS"
        if all(overall_compliant)
        else "OUTSIDE_BAND"
        if has_overall_under and has_overall_over
        else "BELOW_LOWER"
        if has_overall_under
        else "ABOVE_UPPER"
    )
    portfolio_payload["assessment"] = {
        "forecast_energy_basis_mwh": round(assessment_energy, 3),
        "basis_label": "当前预测48点月累计分时电量",
        "granularity": "48_HALF_HOUR_PERIODS",
        "periods": period_rows,
        "annual": {
            "position_mwh": round(optimized["annual_position_mwh"], 3),
            "aggregate_ratio": round(annual_ratio_value, 4),
            "min_ratio": round(min(annual_point_ratios), 4),
            "max_ratio": round(max(annual_point_ratios), 4),
            "compliant_periods": sum(annual_compliant),
            "period_count": len(annual_compliant),
            "lower_limit": 0.60,
            "upper_limit": None,
            "status": annual_status,
        },
        "overall": {
            "position_mwh": round(optimized["overall_position_mwh"], 3),
            "aggregate_ratio": round(overall_ratio_value, 4),
            "min_ratio": round(min(overall_point_ratios), 4),
            "max_ratio": round(max(overall_point_ratios), 4),
            "compliant_periods": sum(overall_compliant),
            "period_count": len(overall_compliant),
            "lower_limit": 0.90,
            "upper_limit": 1.10,
            "included_products": ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
            "status": (
                "PENDING"
                if _event_index(event) < _event_index("D-2")
                else overall_status
            ),
        },
        "unit_note": "年度和总体中长期均按48个半小时点逐点计算考核比例；D-3/D-2按实际成交点计入总体曲线",
    }
    assessment_ready = (
        _event_index(event) >= _event_index("D-2")
        and all(annual_compliant)
        and all(overall_compliant)
    )
    for index, row in enumerate(declaration_breakdown["rows"]):
        point = period_rows[index // 2]
        row.update(
            {
                "annual_assessment_ratio": point["annual_ratio"],
                "annual_assessment_compliant": point["annual_compliant"],
                "overall_assessment_ratio": point["overall_ratio"],
                "overall_assessment_compliant": point["overall_compliant"],
            }
        )
    declaration_breakdown["assessment_guard"] = {
        "status": "PASS" if assessment_ready else "PENDING" if _event_index(event) < _event_index("D-2") else "BLOCKED",
        "long_term_assessment_granularity": "48_HALF_HOUR_PERIODS",
        "long_term_assessment_products": ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
        "long_term_delivery_allocation_grid": "48_HALF_HOUR_PRODUCTS",
        "spot_deviation_assessment_granularity": "96_QUARTER_HOUR_PERIODS",
        "spot_deviation_limit_ratio": 0.10,
        "spot_deviation_basis": "ACTUAL_LOAD_PER_QUARTER_HOUR",
        "annual_lower_ratio": 0.60,
        "overall_band": [0.90, 1.10],
        "annual_aggregate_ratio": round(annual_ratio_value, 4),
        "overall_aggregate_ratio": round(overall_ratio_value, 4),
        "annual_min_ratio": round(min(annual_point_ratios), 4),
        "overall_min_ratio": round(min(overall_point_ratios), 4),
        "overall_max_ratio": round(max(overall_point_ratios), 4),
        "assessment_period_count": 48,
        "aggregate_ratio_role": "DIAGNOSTIC_ONLY_NOT_ASSESSMENT",
        "spot_counts_toward_long_term_assessment": False,
        "note": "年度和总体中长期均在48个半小时产品点逐点计算比例。年度净合约量/对应分时月累计电量不低于60%；总体中长期净合约量/对应分时月累计电量位于90%-110%。日前和实时现货不计入中长期考核。",
    }
    l3_objective = _evaluate_l3_objective(
        optimized["fills"],
        declaration_breakdown,
        storage,
        prices["day_ahead"],
        prices["real_time"],
        risk,
        factor,
        declaration_snapshot,
        active_price_snapshot,
    )
    if l3_milp_run is not None:
        l3_objective.update(l3_milp_run["optimization"])
        l3_objective["objective_definition"] = (
            "L3随机MILP = 锁定合同成本 + 日前净买卖 + 实时补救 + 偏差回收 + 储能退化，"
            "含充放电互斥、SOC、非预见性及CVaR；现货双边量用微小摩擦消除等价解"
        )
    else:
        l3_objective.update(
            {
                "milp_executed": False,
                "status": "MILP_BACKEND_UNAVAILABLE",
                "mip_gap": None,
                "message": l3_milp_error,
            }
        )
    cvar95 = float(l3_objective["cvar95_yuan"])
    risk_objective = float(l3_objective["objective_yuan"])
    current_action = _current_action(
        event,
        optimized_all["actions"],
        declaration,
        storage,
        optimized_margin,
    )
    day_ahead_is_visible = _event_index(event) >= _event_index("D-1_PRICE")
    realized_rt_limit = (
        SPOT_PERIODS if event == "MONTH_END" else max(0, rt_period - 1) if event == "REAL_TIME" else 0
    )
    price_forecast_rows = []
    for index, row in enumerate(active_price_snapshot["rows"]):
        price_forecast_rows.append(
            {
                "period": index + 1,
                "time": spot_time(index + 1),
                "day_ahead_p10": round(float(row["day_ahead_p10"]), 3),
                "day_ahead_p50": round(float(row["day_ahead_p50"]), 3),
                "day_ahead_p90": round(float(row["day_ahead_p90"]), 3),
                "real_time_p10": round(float(row["real_time_p10"]), 3),
                "real_time_p50": round(float(row["real_time_p50"]), 3),
                "real_time_p90": round(float(row["real_time_p90"]), 3),
                "spread_p50": round(float(row["spread_p50"]), 3),
                "official_day_ahead": (
                    round(float(prices["day_ahead"][index]), 3) if day_ahead_is_visible else None
                ),
                "official_real_time": (
                    round(float(prices["real_time"][index]), 3)
                    if index < realized_rt_limit
                    else None
                ),
                "actual_real_time_source": (
                    "MANUAL_OVERRIDE"
                    if actual_real_time_price_overrides
                    and actual_real_time_price_overrides[index] is not None
                    else "SEEDED_RANDOM"
                ),
                "scenario": (
                    "MIDDAY_PV_SURPLUS"
                    if MIDDAY_PV_WINDOW[0] <= index / 4.0 < MIDDAY_PV_WINDOW[1]
                    else "HIGH_PRICE_WINDOW"
                    if any(start <= index / 4.0 < end for start, end in HIGH_PRICE_WINDOWS)
                    else "NORMAL_OPERATION"
                ),
            }
        )
    l1_milp_executed = bool(optimized_all["optimization"]["milp_executed"])
    l3_milp_executed = l3_milp_run is not None
    full_milp_executed = l1_milp_executed and l3_milp_executed
    engine = (
        "scipy.optimize.milp / HiGHS"
        if full_milp_executed
        else "Explicit diagnostic policy only; no optimizer result"
    )
    fixed_until = int(l3_path["execution_ledger"]["fixed_until"])
    replayed_state = _replay_state(
        declaration, storage["rows"], fixed_until, event, optimized["fills"]
    )
    selected_public = public_snapshot(
        selected_snapshot,
        reveal_all_actual=event == "MONTH_END",
        realized_limit=rt_period if event == "REAL_TIME" else 0,
    )
    trace.mark("MPC状态回放")
    payload = {
        "meta": {
            "target_month": "2026-09",
            "target_date": park_config.target_date,
            "selected_event": event,
            "storage_visibility": {
                "visible": event in {"D-1", "D-1_PRICE", "REAL_TIME", "MONTH_END"},
                "mode": (
                    "DAY_AHEAD_CONDITIONAL_PLAN"
                    if event in {"D-1", "D-1_PRICE"}
                    else "REAL_TIME_MPC"
                    if event == "REAL_TIME"
                    else "EXECUTED_HISTORY"
                    if event == "MONTH_END"
                    else "CONDITIONAL_PLAN_HIDDEN"
                ),
                "note": (
                    "D-1展示L3储能条件计划，但不作为已执行动作；只有进入实时滚动计算后，"
                    "当前点储能动作才锁定执行。"
                ),
            },
            "rt_period": rt_period,
            "risk_lambda": risk,
            "risk_alpha": 0.95,
            "cvar_enabled": bool(cvar_enabled),
            "long_term_prices_yuan_per_mwh": {
                "annual": float(annual_price),
                "monthly": float(monthly_price),
                "ten_day": float(ten_day_price),
            },
            "volatility": volatility,
            "engine": engine,
            "model_status": (
                "MILP_OPTIMAL"
                if full_milp_executed and l3_objective.get("status") == "OPTIMAL"
                else "MILP_LIMIT_REACHED"
                if full_milp_executed
                else "DIAGNOSTIC_ONLY"
            ),
            "model_structure": "L1_CONTRACT_MILP_PLUS_L2_ROLLING_TRIGGER_PLUS_L3_96_POINT_JOINT_SCENARIO_MPC",
            "time_grids": {
                "long_term": {"periods": 48, "minutes": 30},
                "spot_and_storage": {"periods": 96, "minutes": 15},
            },
            "milp": {
                "configured": True,
                "executed": full_milp_executed,
                "l1_executed": l1_milp_executed,
                "l3_executed": l3_milp_executed,
                "l1_backend": optimized_all["optimization"]["backend"],
                "l1_statuses": optimized_all["optimization"]["statuses"],
                "l1_max_mip_gap": optimized_all["optimization"]["max_mip_gap"],
                "l3_backend": l3_objective["backend"],
                "l3_status": l3_objective.get("status"),
                "l3_mip_gap": l3_objective.get("mip_gap"),
                "l3_solve_seconds": l3_objective.get("solve_seconds"),
                "l3_end_to_end_seconds": l3_objective.get("end_to_end_seconds"),
                "l2_rule_executed": bool(optimized_all["optimization"].get("l2_near_term_events")),
                "l2_solver": "RULE_ENGINE",
                "service_budget_seconds": 10.0,
                "diagnostic_mode": diagnostic_mode,
                "diagnostic_reason": None if full_milp_executed else l3_milp_error,
            },
            "source_type": "MOCK",
        },
        "headline": {
            "retail_revenue_yuan": _money(float(retail["revenue_yuan"])),
            "optimized_wholesale_cost_yuan": _money(optimized_bill["wholesale_total_yuan"]),
            "optimized_margin_yuan": _money(optimized_margin),
            "margin_improvement_yuan": _money(margin_improvement),
            "cvar95_cost_yuan": _money(cvar95),
            "risk_objective_yuan": _money(risk_objective),
        },
        "park_config": park_config.to_dict(),
        "load_forecast": {
            "selected_snapshot_id": selected_snapshot["snapshot_id"],
            "selected_label": selected_snapshot["label"],
            "period_count": SPOT_PERIODS,
            "interval_minutes": 15,
            "daily_baseline_total_mwh": round(sum(industrial_park_baseline_mw(park_config)) * 0.5, 3),
            "daily_actual_total_mwh": selected_public["quality"]["actual_total_mwh"],
            "month_equivalent_days": factor,
            "monthly_actual_energy_mwh": (
                round(sum(actual_month), 3) if event == "MONTH_END" else None
            ),
            "monthly_forecast_energy_mwh": round(sum(planning_month), 3),
            "quality": selected_public["quality"],
            "convergence": convergence,
            "history": forecast_history,
            "phases": forecast_phases,
            "rows": selected_public["rows"],
        },
        "price_forecast": {
            "unit": "元/MWh",
            "source_type": "MOCK",
            "interval_minutes": 15,
            "selected_snapshot_id": active_price_snapshot["snapshot_id"],
            "forecast_update_events": ["D-3", "D-2", "D-1报量前"],
            "forecast_frozen_during_delivery_day": True,
            "day_ahead_official_visible": day_ahead_is_visible,
            "real_time_official_lag_intervals": 1,
            "real_time_official_visible_through": realized_rt_limit,
            "scenario_note": (
                "按固定种子生成100条96点负荷、日前价和实时价差相关轨迹；"
                "在线保留中心和联合尾部并缩减为64条；日前价公布后该维度固定为真实曲线。"
                "回测真值与预测使用独立随机过程，默认允许少量价格点落在预测带外。"
            ),
            "midday_pv_window": ["11:00", "13:00"],
            "high_price_windows": [["09:00", "11:00"], ["13:00", "17:00"]],
            "rows": price_forecast_rows,
        },
        "scenario_editor": {
            "load": {
                "seed": int(load_scenario_seed),
                "band_source": "D-1",
                "default_rule": "SEEDED_RANDOM_WITHIN_P10_P90",
                "rows": [
                    {
                        "period": index + 1,
                        "time": spot_time(index + 1),
                        "p10": round(float(row["p10_mwh"]), 6),
                        "p50": round(float(row["p50_mwh"]), 6),
                        "p90": round(float(row["p90_mwh"]), 6),
                        "actual": round(float(row["actual_mwh"]), 6),
                        "source": row.get("actual_source", "SEEDED_RANDOM"),
                        "outside_band": (
                            float(row["actual_mwh"]) < float(row["p10_mwh"])
                            or float(row["actual_mwh"]) > float(row["p90_mwh"])
                        ),
                    }
                    for index, row in enumerate(spot_forecasts["D-1"]["rows"])
                ],
            },
            "real_time_price": {
                "seed": int(price_scenario_seed),
                "band_source": "D-1实时价格预测",
                "default_rule": "SEEDED_INDEPENDENT_TRUTH_WITH_NATURAL_TAILS",
                "rows": [
                    {
                        "period": index + 1,
                        "time": spot_time(index + 1),
                        "p10": round(float(row["real_time_p10"]), 3),
                        "p50": round(float(row["real_time_p50"]), 3),
                        "p90": round(float(row["real_time_p90"]), 3),
                        "actual": round(float(prices["real_time"][index]), 3),
                        "source": (
                            "MANUAL_OVERRIDE"
                            if actual_real_time_price_overrides
                            and actual_real_time_price_overrides[index] is not None
                            else "SEEDED_RANDOM"
                        ),
                        "outside_band": (
                            float(prices["real_time"][index])
                            < float(row["real_time_p10"])
                            or float(prices["real_time"][index])
                            > float(row["real_time_p90"])
                        ),
                    }
                    for index, row in enumerate(price_forecasts["D-1"]["rows"])
                ],
            },
        },
        "portfolio": portfolio_payload,
        "rolling_orders": portfolio_payload.get("rolling_orders", {}),
        "declaration": declaration,
        "declaration_breakdown": declaration_breakdown,
        "execution_ledger": l3_path["execution_ledger"],
        "mpc_state": asdict(replayed_state),
        "l3_objective": l3_objective,
        "storage": storage,
        "retail": retail,
        "settlement": {
            "baseline": baseline_bill,
            "optimized": optimized_bill,
            "baseline_margin_yuan": _money(baseline_margin),
            "optimized_margin_yuan": _money(optimized_margin),
            "margin_improvement_yuan": _money(margin_improvement),
            "waterfall": [
                {"key": "revenue", "label": "零售收入", "value": _money(float(retail["revenue_yuan"]))},
                {"key": "wholesale", "label": "批发购电及回收", "value": -_money(optimized_bill["wholesale_total_yuan"])},
                {"key": "margin", "label": "优化后毛利", "value": _money(optimized_margin)},
                {"key": "improvement", "label": "较基线改善", "value": _money(margin_improvement)},
            ],
        },
        "market_profile": [
            {
                "period": index + 1,
                "time": selected_snapshot["rows"][index]["time"],
                "annual_price": prices["annual"][index],
                "monthly_price": prices["monthly"][index],
                "ten_day_price": prices["ten_day"][index],
                "day_ahead_price": prices["day_ahead"][index],
                "real_time_price": prices["real_time"][index],
                "retail_reference_price": retail["reference_curve"][index],
            }
            for index in range(SPOT_PERIODS)
        ],
        "current_action": current_action,
        "timeline": _timeline(event, optimized_all["actions"], forecast_history, rt_period),
        "diagnostics": [
            "负荷预测为年度至 RT-96 的可追溯 Mock 快照；实时未来负荷只用已实现前缀误差更新。",
            (
                "L1合同由MILP求解；L2滚撮由价格阈值和L3边际价值门控规则顺序触发；L3日前/实时/储能由SciPy HiGHS MILP求解。"
                if full_milp_executed
                else "已显式启用diagnostic_mode；当前规则策略不能证明最优，也不计为MILP结果。"
            ),
            "L1当前使用线性补救成本代理；尚未把完整L2整数值函数精确嵌入L1。",
            "零售 70%/20%/10% 权重来自规则示例，已作为可版本化配置而非硬编码市场事实。",
        ],
        "logic": [
            {"step": 1, "label": "更新联合预测", "detail": "D-3、D-2、D-1重算96点负荷、日前价和实时价差联合场景；日内价格预测冻结。"},
            {"step": 2, "label": "计算交易员基线缺口", "detail": "交易员基线按覆盖率和目标分位数计算目标仓位减已成交仓位。"},
            {"step": 3, "label": "求解L1 MILP", "detail": "年度、月度、旬内在48点产品上联合逐点考核、补救成本代理和CVaR求解。"},
            {"step": 4, "label": "捕捉并成交滚撮", "detail": "D-3/D-2按实时现货P50预测价正负100元/MWh捕捉订单，在90%-110%仓位内成交订单量的10%-20%。"},
            {"step": 5, "label": "重求L3 MILP", "detail": "成交曲线写入锁定合同后，从100条96点相关轨迹保留64条联合求解日前净买卖、实时补救、储能和CVaR。"},
            {"step": 6, "label": "只执行当前动作", "detail": "D-3、D-2和日前分别锁定；标的日每15分钟固定历史前缀后重求解剩余储能。"},
            {"step": 7, "label": "统一核算利润", "detail": "零售套餐收入减批发能量、逐笔合同差价、回收与交易费。"},
        ],
    }
    trace.finish()
    payload["meta"]["timing"] = trace.payload()
    return payload


def summary(diagnostic_mode: bool = False) -> dict:
    return simulate(require_milp=not diagnostic_mode, diagnostic_mode=diagnostic_mode)
