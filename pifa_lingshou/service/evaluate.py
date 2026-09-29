from __future__ import annotations

from typing import Iterable, Mapping, Sequence
from dataclasses import asdict

from ..data_objects.model import AggregateInput, PACKAGES
from ..problem_solver.retail_wholesale_milp import solve_candidate_milp


def evaluate(inputs: AggregateInput, packages: Sequence[str] = PACKAGES, risk_lambdas: Sequence[float] = ()) -> Mapping[str, object]:
    """Solve every requested package/lambda pair independently."""

    inputs.validate()
    selected_packages = tuple(packages)
    if not selected_packages or any(package not in PACKAGES for package in selected_packages):
        raise ValueError("packages 只能包含 F、L、S")
    selected_lambdas = tuple(float(value) for value in (risk_lambdas or inputs.risk_lambdas))
    if not selected_lambdas or any(not 0.0 <= value <= 1.0 for value in selected_lambdas):
        raise ValueError("risk_lambdas 必须是 [0, 1] 内的非空序列")
    results = []
    for package in selected_packages:
        for risk_lambda in selected_lambdas:
            results.append(solve_candidate_milp(inputs, package, risk_lambda))
    feasible = [result for result in results if result.get("feasible")]
    return {
        "cvar_alpha": inputs.cvar_alpha,
        "model_parameters": {
            "deviation_buy_penalty": inputs.deviation_buy_penalty,
            "deviation_sell_penalty": inputs.deviation_sell_penalty,
            "deviation_ratio_limit": inputs.deviation_ratio_limit,
            "transaction_friction_yuan_per_mwh": inputs.transaction_friction_yuan_per_mwh,
            "declaration_slack_penalty_yuan_per_mwh": inputs.declaration_slack_penalty_yuan_per_mwh,
            "customer_bill_limits_configured": bool(inputs.customer_bill_limits_yuan) or any(
                c.max_expected_bill_yuan is not None for c in inputs.customers
            ),
            "minimum_expected_profit_yuan": inputs.minimum_expected_profit_yuan,
            "maximum_profit_loss_cvar_yuan": inputs.maximum_profit_loss_cvar_yuan,
        },
        "input_defaults": _input_defaults(inputs),
        "packages": list(selected_packages),
        "risk_lambdas": list(selected_lambdas),
        "results": results,
        "feasible_results": feasible,
        "pareto_frontier": pareto_frontier(feasible),
    }


def _input_defaults(inputs: AggregateInput) -> Mapping[str, object]:
    """Return the complete demo input in a JSON/HTML-friendly structure."""

    return asdict(inputs)


def select_plan(results: Iterable[Mapping[str, object]]) -> Mapping[str, object] | None:
    """Prefer profit, then lower tail loss, then customer savings."""

    candidates = list(results)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda result: (
            float(result.get("expected_profit_yuan", float("-inf"))),
            -float(result.get("profit_loss_cvar_yuan", float("inf"))),
            sum(
                float(customer.get("expected_saving_yuan", 0.0))
                for customer in result.get("customer_results", {}).values()
            ),
        ),
    )


def pareto_frontier(results: Iterable[Mapping[str, object]]) -> list:
    """Return non-dominated results in expected profit / tail loss space."""

    candidates = list(results)
    frontier = []
    for candidate in candidates:
        candidate_profit = float(candidate.get("expected_profit_yuan", float("-inf")))
        candidate_risk = float(candidate.get("profit_loss_cvar_yuan", float("inf")))
        dominated = False
        for other in candidates:
            other_profit = float(other.get("expected_profit_yuan", float("-inf")))
            other_risk = float(other.get("profit_loss_cvar_yuan", float("inf")))
            if (
                other_profit >= candidate_profit
                and other_risk <= candidate_risk
                and (other_profit > candidate_profit or other_risk < candidate_risk)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return frontier
