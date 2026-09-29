from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterable, List, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class DiscreteLoadScenario:
    scenario_id: str
    quantile_level: float
    z_score: float
    probability: float
    real_time_price_factor: float


@dataclass(frozen=True)
class JointTrajectoryScenario:
    scenario_id: str
    probability: float
    load_quantile: str
    day_ahead_quantile: str
    spread_quantile: str
    load_mwh: Tuple[float, ...]
    day_ahead_price: Tuple[float, ...]
    real_time_price: Tuple[float, ...]


QUANTILE_STATES: Tuple[Tuple[str, float, float], ...] = (
    ("P05", -1.645, 0.0625),
    ("P25", -0.674, 0.2500),
    ("P50", 0.000, 0.3750),
    ("P75", 0.674, 0.2500),
    ("P95", 1.645, 0.0625),
)

JOINT_CANDIDATE_COUNT = 100
L3_OPTIMIZATION_SCENARIO_COUNT = 64
# Compatibility alias for clients that used the pre-three-layer name.
L2_OPTIMIZATION_SCENARIO_COUNT = L3_OPTIMIZATION_SCENARIO_COUNT
JOINT_SCENARIO_SEED = 2026091503
_NORMAL = NormalDist()


def discrete_load_scenarios() -> Tuple[DiscreteLoadScenario, ...]:
    """Five-state marginal load catalog for pre-spot L1 recourse.

    D-3 and later use :func:`reduced_joint_trajectories` instead.  These
    marginal states exist because annual/monthly/ten-day nodes do not yet have
    a delivery-day DA/RT price forecast to form the full joint catalog.
    """

    price_factors = (0.85, 0.95, 1.00, 1.07, 1.18)
    return tuple(
        DiscreteLoadScenario(label, level, z_score, probability, price_factor)
        for (label, z_score, probability), level, price_factor in zip(
            QUANTILE_STATES,
            (0.05, 0.25, 0.50, 0.75, 0.95),
            price_factors,
        )
    )


def discrete_load_value(
    row: Mapping[str, object], scenario: DiscreteLoadScenario
) -> float:
    """Read the forecasted marginal state for one pre-spot scenario."""

    p10 = float(row["p10_mwh"])
    p50 = float(row["p50_mwh"])
    p90 = float(row["p90_mwh"])
    level = scenario.quantile_level
    if scenario.scenario_id == "P75" and "p75_mwh" in row:
        return max(0.0, float(row["p75_mwh"]))
    if level <= 0.50:
        weight = (level - 0.10) / 0.40
        return max(0.0, p10 + weight * (p50 - p10))
    weight = (level - 0.50) / 0.40
    return max(0.0, p50 + weight * (p90 - p50))


def _normal_strata(count: int, rng: random.Random) -> List[float]:
    values = [_NORMAL.inv_cdf((index + 0.5) / count) for index in range(count)]
    rng.shuffle(values)
    return values


def _persistent_path(
    rng: random.Random, initial: float, count: int, persistence: float
) -> List[float]:
    innovation_scale = math.sqrt(max(0.0, 1.0 - persistence * persistence))
    state = float(initial)
    values = []
    for _ in range(count):
        state = persistence * state + innovation_scale * rng.gauss(0.0, 1.0)
        values.append(state)
    return values


def _standardize_columns(matrix: Sequence[Sequence[float]]) -> List[List[float]]:
    if not matrix:
        return []
    row_count = len(matrix)
    column_count = len(matrix[0])
    result = [[0.0] * column_count for _ in range(row_count)]
    for column in range(column_count):
        values = [float(matrix[row][column]) for row in range(row_count)]
        mean = sum(values) / row_count
        variance = sum((value - mean) ** 2 for value in values) / row_count
        scale = math.sqrt(max(variance, 1e-12))
        for row, value in enumerate(values):
            result[row][column] = max(-3.2, min(3.2, (value - mean) / scale))
    return result


def _empirical_labels(scores: Sequence[float]) -> List[str]:
    order = sorted(range(len(scores)), key=lambda index: float(scores[index]))
    labels = ["P50"] * len(scores)
    for rank, index in enumerate(order):
        percentile = (rank + 0.5) / max(len(scores), 1)
        if percentile <= 0.10:
            labels[index] = "P05"
        elif percentile <= 0.375:
            labels[index] = "P25"
        elif percentile <= 0.625:
            labels[index] = "P50"
        elif percentile <= 0.90:
            labels[index] = "P75"
        else:
            labels[index] = "P95"
    return labels


def _joint_factor_paths(count: int, periods: int, seed: int) -> tuple:
    """Generate correlated but non-identical load and price error factors."""

    rng = random.Random(int(seed))
    demand_levels = _normal_strata(count, rng)
    supply_levels = _normal_strata(count, rng)
    load_levels = _normal_strata(count, rng)
    day_ahead_levels = _normal_strata(count, rng)
    spread_levels = _normal_strata(count, rng)
    load_raw: List[List[float]] = []
    day_ahead_raw: List[List[float]] = []
    spread_raw: List[List[float]] = []
    for scenario in range(count):
        demand = _persistent_path(rng, demand_levels[scenario], periods, 0.88)
        supply = _persistent_path(rng, supply_levels[scenario], periods, 0.90)
        load_noise = _persistent_path(rng, load_levels[scenario], periods, 0.84)
        day_ahead_noise = _persistent_path(
            rng, day_ahead_levels[scenario], periods, 0.80
        )
        spread_noise = _persistent_path(rng, spread_levels[scenario], periods, 0.76)
        load_path: List[float] = []
        day_ahead_path: List[float] = []
        spread_path: List[float] = []
        for index in range(periods):
            hour = index / 4.0
            if 9.0 <= hour < 11.0 or 13.0 <= hour < 17.0:
                scarcity_coupling = 1.0
            elif 11.0 <= hour < 13.0:
                scarcity_coupling = 0.35
            else:
                scarcity_coupling = 0.65
            load_factor = 0.72 * demand[index] + 0.58 * load_noise[index]
            day_ahead_factor = (
                (0.24 + 0.22 * scarcity_coupling) * load_factor
                + 0.22 * demand[index]
                - 0.48 * supply[index]
                + 0.58 * day_ahead_noise[index]
            )
            spread_factor = (
                (0.14 + 0.20 * scarcity_coupling) * load_factor
                + 0.14 * demand[index]
                - 0.58 * supply[index]
                + 0.68 * spread_noise[index]
            )
            load_path.append(load_factor)
            day_ahead_path.append(day_ahead_factor)
            spread_path.append(spread_factor)
        load_raw.append(load_path)
        day_ahead_raw.append(day_ahead_path)
        spread_raw.append(spread_path)
    return (
        _standardize_columns(load_raw),
        _standardize_columns(day_ahead_raw),
        _standardize_columns(spread_raw),
    )


def candidate_joint_trajectories(
    load_snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    candidate_count: int = JOINT_CANDIDATE_COUNT,
    seed: int = JOINT_SCENARIO_SEED,
) -> List[JointTrajectoryScenario]:
    if len(load_snapshot["rows"]) != len(price_snapshot["rows"]):
        raise ValueError("负荷和价格预测时段数不一致")
    if candidate_count < 8:
        raise ValueError("candidate_count 至少为8，以覆盖中心和联合尾部")
    periods = len(load_snapshot["rows"])
    load_factors, day_ahead_factors, spread_factors = _joint_factor_paths(
        candidate_count, periods, seed
    )
    official_day_ahead = (
        price_snapshot.get("information_state") == "OFFICIAL_DAY_AHEAD_REVEALED"
    )
    average_load_factor = [sum(path) / periods for path in load_factors]
    average_day_ahead_factor = [sum(path) / periods for path in day_ahead_factors]
    average_spread_factor = [sum(path) / periods for path in spread_factors]
    load_labels = _empirical_labels(average_load_factor)
    day_ahead_labels = (
        ["P50"] * candidate_count
        if official_day_ahead
        else _empirical_labels(average_day_ahead_factor)
    )
    spread_labels = _empirical_labels(average_spread_factor)
    center_index = min(
        range(candidate_count),
        key=lambda index: abs(average_load_factor[index])
        + (0.0 if official_day_ahead else abs(average_day_ahead_factor[index]))
        + abs(average_spread_factor[index]),
    )
    scenarios: List[JointTrajectoryScenario] = []
    for scenario in range(candidate_count):
        load_values: List[float] = []
        day_ahead_values: List[float] = []
        real_time_values: List[float] = []
        for index, load_row in enumerate(load_snapshot["rows"]):
            price_row = price_snapshot["rows"][index]
            load_p50 = float(load_row["p50_mwh"])
            load_sigma = max(
                (float(load_row["p90_mwh"]) - float(load_row["p10_mwh"]))
                / 2.563,
                0.015 * max(load_p50, 0.01),
            )
            load = (
                load_p50
                if load_row.get("status") == "ACTUAL"
                else max(0.0, load_p50 + load_factors[scenario][index] * load_sigma)
            )
            day_ahead_p50 = float(price_row["day_ahead_p50"])
            day_ahead_sigma = max(
                (
                    float(price_row["day_ahead_p90"])
                    - float(price_row["day_ahead_p10"])
                )
                / 2.563,
                5.0,
            )
            day_ahead = (
                day_ahead_p50
                if official_day_ahead
                else day_ahead_p50
                + day_ahead_factors[scenario][index] * day_ahead_sigma
            )
            day_ahead = max(-2000.0, min(5000.0, day_ahead))
            spread_p50 = float(price_row["spread_p50"])
            spread_sigma = max(
                (float(price_row["spread_p90"]) - float(price_row["spread_p10"]))
                / 2.563,
                7.0,
            )
            spread = (
                spread_p50
                if price_row.get("real_time_observed")
                else spread_p50
                + spread_factors[scenario][index] * spread_sigma
            )
            real_time = max(-2000.0, min(5000.0, day_ahead + spread))
            load_values.append(load)
            day_ahead_values.append(day_ahead)
            real_time_values.append(real_time)
        load_label = load_labels[scenario]
        day_ahead_label = day_ahead_labels[scenario]
        spread_label = spread_labels[scenario]
        scenario_id = (
            "L50_DA50_SP50"
            if scenario == center_index
            else "MC%03d_L%s_DA%s_SP%s"
            % (
                scenario + 1,
                load_label[1:],
                day_ahead_label[1:],
                spread_label[1:],
            )
        )
        scenarios.append(
            JointTrajectoryScenario(
                scenario_id=scenario_id,
                probability=1.0 / candidate_count,
                load_quantile=load_label,
                day_ahead_quantile=day_ahead_label,
                spread_quantile=spread_label,
                load_mwh=tuple(load_values),
                day_ahead_price=tuple(day_ahead_values),
                real_time_price=tuple(real_time_values),
            )
        )
    return scenarios


def _catalog_scales(
    candidates: Sequence[JointTrajectoryScenario],
) -> Tuple[float, float, float]:
    periods = len(candidates[0].load_mwh)

    def scale(field: str, floor: float) -> float:
        variance_sum = 0.0
        for index in range(periods):
            values = [float(getattr(item, field)[index]) for item in candidates]
            mean = sum(values) / len(values)
            variance_sum += sum((value - mean) ** 2 for value in values) / len(values)
        return max(floor, math.sqrt(variance_sum / periods))

    return (
        scale("load_mwh", 0.01),
        scale("day_ahead_price", 5.0),
        scale("real_time_price", 7.0),
    )


def _trajectory_distance(
    left: JointTrajectoryScenario,
    right: JointTrajectoryScenario,
    scales: Tuple[float, float, float],
) -> float:
    count = len(left.load_mwh)
    load_scale, day_ahead_scale, real_time_scale = scales
    return sum(
        ((left.load_mwh[index] - right.load_mwh[index]) / load_scale) ** 2
        + (
            (left.day_ahead_price[index] - right.day_ahead_price[index])
            / day_ahead_scale
        )
        ** 2
        + (
            (left.real_time_price[index] - right.real_time_price[index])
            / real_time_scale
        )
        ** 2
        for index in range(count)
    ) / max(count, 1)


def _trajectory_distance_matrix(
    candidates: Sequence[JointTrajectoryScenario],
    scales: Tuple[float, float, float],
) -> List[List[float]]:
    count = len(candidates)
    distances = [[0.0] * count for _ in range(count)]
    for left in range(count):
        for right in range(left + 1, count):
            distance = _trajectory_distance(
                candidates[left], candidates[right], scales
            )
            distances[left][right] = distance
            distances[right][left] = distance
    return distances


def _anchor_scenarios(
    candidates: Sequence[JointTrajectoryScenario], official_day_ahead: bool
) -> List[JointTrajectoryScenario]:
    center = next(item for item in candidates if item.scenario_id == "L50_DA50_SP50")
    metrics = [
        lambda item: sum(item.load_mwh),
        lambda item: sum(
            rt - da
            for rt, da in zip(item.real_time_price, item.day_ahead_price)
        ),
    ]
    if not official_day_ahead:
        metrics.insert(1, lambda item: sum(item.day_ahead_price))
    anchors = [center]
    for metric in metrics:
        anchors.extend((min(candidates, key=metric), max(candidates, key=metric)))
    unique: List[JointTrajectoryScenario] = []
    seen = set()
    for item in anchors:
        if item.scenario_id not in seen:
            unique.append(item)
            seen.add(item.scenario_id)
    return unique


def reduced_joint_trajectories(
    load_snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    scenario_count: int = L2_OPTIMIZATION_SCENARIO_COUNT,
) -> Tuple[JointTrajectoryScenario, ...]:
    """Reduce joint trajectories while retaining weighted tail diversity."""

    candidates = candidate_joint_trajectories(load_snapshot, price_snapshot)
    if not 1 <= scenario_count <= len(candidates):
        raise ValueError("scenario_count 必须位于候选场景数范围内")
    if scenario_count == len(candidates):
        return tuple(candidates)
    official_day_ahead = (
        price_snapshot.get("information_state") == "OFFICIAL_DAY_AHEAD_REVEALED"
    )
    scales = _catalog_scales(candidates)
    distances = _trajectory_distance_matrix(candidates, scales)
    candidate_index = {
        item.scenario_id: index for index, item in enumerate(candidates)
    }
    selected = _anchor_scenarios(candidates, official_day_ahead)
    if scenario_count < len(selected):
        selected = selected[:scenario_count]
    remaining = [item for item in candidates if item not in selected]
    while len(selected) < scenario_count:
        next_item = max(
            remaining,
            key=lambda item: item.probability
            * min(
                distances[candidate_index[item.scenario_id]][
                    candidate_index[chosen.scenario_id]
                ]
                for chosen in selected
            ),
        )
        selected.append(next_item)
        remaining.remove(next_item)

    assigned_probability = {item.scenario_id: 0.0 for item in selected}
    for candidate in candidates:
        nearest = min(
            selected,
            key=lambda item: distances[candidate_index[candidate.scenario_id]][
                candidate_index[item.scenario_id]
            ],
        )
        assigned_probability[nearest.scenario_id] += candidate.probability
    return tuple(
        JointTrajectoryScenario(
            scenario_id=item.scenario_id,
            probability=assigned_probability[item.scenario_id],
            load_quantile=item.load_quantile,
            day_ahead_quantile=item.day_ahead_quantile,
            spread_quantile=item.spread_quantile,
            load_mwh=item.load_mwh,
            day_ahead_price=item.day_ahead_price,
            real_time_price=item.real_time_price,
        )
        for item in selected
    )


def joint_scenario_diagnostics(
    load_snapshot: Mapping[str, object],
    price_snapshot: Mapping[str, object],
    scenarios: Sequence[JointTrajectoryScenario],
) -> dict:
    """Summarize dependence in normalized load and price forecast errors."""

    load_errors: List[float] = []
    day_ahead_errors: List[float] = []
    real_time_errors: List[float] = []
    weights: List[float] = []
    for scenario in scenarios:
        for index, load in enumerate(scenario.load_mwh):
            load_row = load_snapshot["rows"][index]
            price_row = price_snapshot["rows"][index]
            load_sigma = max(
                (float(load_row["p90_mwh"]) - float(load_row["p10_mwh"]))
                / 2.563,
                0.015 * max(float(load_row["p50_mwh"]), 0.01),
            )
            day_ahead_sigma = max(
                (
                    float(price_row["day_ahead_p90"])
                    - float(price_row["day_ahead_p10"])
                )
                / 2.563,
                5.0,
            )
            real_time_sigma = max(
                (
                    float(price_row["real_time_p90"])
                    - float(price_row["real_time_p10"])
                )
                / 2.563,
                7.0,
            )
            load_errors.append(
                (float(load) - float(load_row["p50_mwh"])) / load_sigma
            )
            day_ahead_errors.append(
                (
                    float(scenario.day_ahead_price[index])
                    - float(price_row["day_ahead_p50"])
                )
                / day_ahead_sigma
            )
            real_time_errors.append(
                (
                    float(scenario.real_time_price[index])
                    - float(price_row["real_time_p50"])
                )
                / real_time_sigma
            )
            weights.append(float(scenario.probability))

    def correlation(left: Sequence[float], right: Sequence[float]):
        total_weight = sum(weights)
        left_mean = sum(
            weight * value for weight, value in zip(weights, left)
        ) / total_weight
        right_mean = sum(
            weight * value for weight, value in zip(weights, right)
        ) / total_weight
        covariance = sum(
            weight * (x - left_mean) * (y - right_mean)
            for weight, x, y in zip(weights, left, right)
        )
        left_scale = math.sqrt(
            sum(
                weight * (value - left_mean) ** 2
                for weight, value in zip(weights, left)
            )
        )
        right_scale = math.sqrt(
            sum(
                weight * (value - right_mean) ** 2
                for weight, value in zip(weights, right)
            )
        )
        if left_scale <= 1e-12 or right_scale <= 1e-12:
            return None
        return covariance / (left_scale * right_scale)

    def rounded(value):
        return None if value is None else round(float(value), 6)

    return {
        "method": "SEEDED_CORRELATED_96_POINT_MONTE_CARLO",
        "seed": JOINT_SCENARIO_SEED,
        "trajectory_count": len(scenarios),
        "period_count": len(load_snapshot["rows"]),
        "probability_sum": round(sum(item.probability for item in scenarios), 12),
        "load_day_ahead_error_correlation": rounded(
            correlation(load_errors, day_ahead_errors)
        ),
        "load_real_time_error_correlation": rounded(
            correlation(load_errors, real_time_errors)
        ),
        "day_ahead_real_time_error_correlation": rounded(
            correlation(day_ahead_errors, real_time_errors)
        ),
    }


def weighted_mean(values: Sequence[float], probabilities: Sequence[float]) -> float:
    total_probability = sum(probabilities)
    if total_probability <= 0:
        raise ValueError("场景概率之和必须大于 0")
    return sum(value * probability for value, probability in zip(values, probabilities)) / total_probability


def weighted_quantile(
    values: Sequence[float], probabilities: Sequence[float], quantile: float
) -> float:
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile 必须位于 [0, 1]")
    pairs = sorted(zip(values, probabilities), key=lambda item: item[0])
    threshold = quantile * sum(probabilities)
    cumulative = 0.0
    for value, probability in pairs:
        cumulative += probability
        if cumulative + 1e-12 >= threshold:
            return value
    return pairs[-1][0]


def cvar(values: Sequence[float], probabilities: Sequence[float], alpha: float = 0.95) -> float:
    if not 0.0 <= alpha < 1.0:
        raise ValueError("alpha 必须位于 [0, 1)")
    var = weighted_quantile(values, probabilities, alpha)
    tail_probability = 1.0 - alpha
    strict_tail = sum(
        probability for value, probability in zip(values, probabilities) if value > var
    )
    strict_cost = sum(
        value * probability
        for value, probability in zip(values, probabilities)
        if value > var
    )
    mass_at_var = max(0.0, tail_probability - strict_tail)
    return (strict_cost + var * mass_at_var) / tail_probability


def distribution_stats(values: Sequence[float], probabilities: Sequence[float]) -> dict:
    mean = weighted_mean(values, probabilities)
    variance = weighted_mean([(value - mean) ** 2 for value in values], probabilities)
    return {
        "mean": mean,
        "std": math.sqrt(max(0.0, variance)),
        "p5": weighted_quantile(values, probabilities, 0.05),
        "p50": weighted_quantile(values, probabilities, 0.50),
        "p95": weighted_quantile(values, probabilities, 0.95),
        "cvar95": cvar(values, probabilities, 0.95),
        "worst": max(values),
    }


def probability_negative(values: Iterable[float], probabilities: Iterable[float]) -> float:
    return sum(probability for value, probability in zip(values, probabilities) if value < 0.0)
