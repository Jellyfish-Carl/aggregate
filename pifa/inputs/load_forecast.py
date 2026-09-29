from __future__ import annotations

import calendar
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from utils.mock_scenario import (
    DEFAULT_LOAD_SCENARIO_SEED,
    apply_point_overrides,
    seeded_band_path,
)
from constants.timegrid import SPOT_PERIODS, aggregate_quarter_hour_energy, expand_half_hour_energy, spot_time


@dataclass(frozen=True)
class IndustrialParkConfig:
    name: str = "MOCK 双班制工业园区"
    target_date: str = "2026-09-15"
    load_scale: float = 1.0
    weekend_factor: float = 0.72
    actual_scale: float = 1.015
    physical_peak_mw: float = 20.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ForecastSpec:
    event: str
    label: str
    published_at: str
    error_scale: float
    half_width: float
    bias_ratio: float
    p75_half_width: Optional[float] = None


FORECAST_SPECS: Sequence[ForecastSpec] = (
    ForecastSpec("ANNUAL", "年度", "2026-01-15T10:00:00+08:00", 0.12, 0.20, -0.09, 0.12),
    ForecastSpec("MONTHLY", "月度", "2026-08-20T10:00:00+08:00", 0.08, 0.14, 0.035, 0.08),
    ForecastSpec("TEN_DAY", "旬内", "2026-09-10T10:00:00+08:00", 0.055, 0.10, 0.055, 0.06),
    ForecastSpec("D-3", "D-3 滚撮", "2026-09-12T10:00:00+08:00", 0.04, 0.07, 0.015, 0.042),
    ForecastSpec("D-2", "D-2 滚撮", "2026-09-13T10:00:00+08:00", 0.03, 0.05, -0.007, 0.03),
    ForecastSpec("D-1", "D-1", "2026-09-14T10:00:00+08:00", 0.02, 0.03, 0.002, 0.018),
)


def industrial_park_baseline_mw(config: IndustrialParkConfig = IndustrialParkConfig()) -> List[float]:
    curve = (
        [5.5] * 12
        + [7.0, 9.0, 11.0, 13.0]
        + [15.0] * 8
        + [11.0] * 3
        + [16.0] * 8
        + [14.0]
        + [12.0] * 7
        + [9.0]
        + [6.5] * 4
    )
    return [round(value * config.load_scale, 6) for value in curve]


def _forecast_rows_without_actual(
    config: IndustrialParkConfig, volatility: str
) -> List[dict]:
    multiplier = 1.0 if volatility == "high" else 0.65
    forecast_anchor = expand_half_hour_energy(
        [
            value * 0.5 * config.actual_scale
            for value in industrial_park_baseline_mw(config)
        ]
    )
    return [
        {
            "snapshot_id": "LOAD-%s" % spec.event.replace("-", ""),
            "event": spec.event,
            "label": spec.label,
            "published_at": spec.published_at,
            "rows": _curve_rows(
                spec.event,
                spec.label,
                forecast_anchor,
                forecast_anchor,
                spec.error_scale * multiplier,
                spec.half_width * multiplier,
                bias_ratio=spec.bias_ratio * multiplier,
                p75_half_width=(
                    spec.p75_half_width
                    if spec.p75_half_width is not None
                    else spec.half_width * 0.6
                )
                * multiplier,
            ),
        }
        for spec in FORECAST_SPECS
    ]


def generate_actual_load_path(
    d1_snapshot: Mapping[str, object],
    seed: int = DEFAULT_LOAD_SCENARIO_SEED,
    overrides: Sequence[Optional[float]] = (),
) -> tuple:
    generated = seeded_band_path(
        d1_snapshot["rows"], "p10_mwh", "p50_mwh", "p90_mwh", seed
    )
    values, sources = apply_point_overrides(
        generated,
        overrides,
        label="实际负荷",
        minimum=0.0,
    )
    return [round(value, 6) for value in values], sources


def hidden_actual_spot_mwh(
    config: IndustrialParkConfig = IndustrialParkConfig(),
    seed: int = DEFAULT_LOAD_SCENARIO_SEED,
    overrides: Sequence[Optional[float]] = (),
) -> List[float]:
    forecasts = _forecast_rows_without_actual(config, "high")
    values, _sources = generate_actual_load_path(forecasts[-1], seed, overrides)
    return values


def hidden_actual_mwh(config: IndustrialParkConfig = IndustrialParkConfig()) -> List[float]:
    """Compatibility aggregate for 48-point long-term product settlement."""

    return [round(value, 6) for value in aggregate_quarter_hour_energy(hidden_actual_spot_mwh(config))]


def _error_shape(period: int, period_count: int = SPOT_PERIODS) -> float:
    return 0.68 * math.sin(2.0 * math.pi * (period + 6) / period_count) + 0.32 * math.cos(
        6.0 * math.pi * (period - 4) / period_count
    )


def _curve_rows(
    event: str,
    label: str,
    actual: Sequence[float],
    forecast_anchor: Sequence[float],
    error_scale: float,
    half_width: float,
    bias_ratio: float = 0.0,
    realized_periods: int = 0,
    p75_half_width: Optional[float] = None,
) -> List[dict]:
    if len(actual) != len(forecast_anchor):
        raise ValueError("实际曲线和预测锚点长度必须一致")
    rows: List[dict] = []
    for index, (actual_value, anchor_value) in enumerate(zip(actual, forecast_anchor)):
        period = index + 1
        if period <= realized_periods:
            p50 = p10 = p75 = p90 = actual_value
            status = "ACTUAL"
        else:
            p50 = max(
                0.0,
                anchor_value
                * (1.0 + bias_ratio + error_scale * _error_shape(period, len(actual))),
            )
            p10 = max(0.0, p50 * (1.0 - half_width))
            p90 = p50 * (1.0 + half_width)
            if p75_half_width is None:
                # Compatibility for callers constructing legacy rows directly.
                p75 = p50 + 0.625 * (p90 - p50)
            else:
                p75 = p50 * (1.0 + p75_half_width)
            status = "FORECAST"
        rows.append(
            {
                "event": event,
                "label": label,
                "period": period,
                "time": spot_time(period),
                "p10_mwh": round(p10, 6),
                "p50_mwh": round(p50, 6),
                "p75_mwh": round(p75, 6),
                "p90_mwh": round(p90, 6),
                "actual_mwh": round(actual_value, 6),
                "status": status,
            }
        )
    return rows


def _quality(rows: Sequence[Mapping[str, float]]) -> dict:
    absolute_errors = [abs(float(row["p50_mwh"]) - float(row["actual_mwh"])) for row in rows]
    actual_total = sum(float(row["actual_mwh"]) for row in rows)
    width = sum(float(row["p90_mwh"]) - float(row["p10_mwh"]) for row in rows)
    wape = sum(absolute_errors) / max(actual_total, 1e-9)
    return {
        "p10_total_mwh": round(sum(float(row["p10_mwh"]) for row in rows), 3),
        "p50_total_mwh": round(sum(float(row["p50_mwh"]) for row in rows), 3),
        "p90_total_mwh": round(sum(float(row["p90_mwh"]) for row in rows), 3),
        "actual_total_mwh": round(actual_total, 3),
        "mae_mwh": round(sum(absolute_errors) / len(rows), 4),
        "wape": round(wape, 4),
        "mape": round(wape, 4),
        "percentage_error_metric": "WAPE",
        "interval_width_mwh": round(width, 3),
    }


def _real_time_anchor(
    actual: Sequence[float],
    forecast_anchor: Sequence[float],
    realized_periods: int,
    error_scale: float,
    bias_ratio: float,
) -> List[float]:
    """Condition the future median only on the realized load prefix."""

    realized_errors: List[float] = []
    start = max(0, realized_periods - 8)
    for index in range(start, realized_periods):
        base = float(forecast_anchor[index]) * (
            1.0 + bias_ratio + error_scale * _error_shape(index + 1, SPOT_PERIODS)
        )
        realized_errors.append(float(actual[index]) / max(base, 1e-9) - 1.0)
    if not realized_errors:
        return list(forecast_anchor)
    weights = list(range(1, len(realized_errors) + 1))
    bias_state = sum(weight * error for weight, error in zip(weights, realized_errors)) / sum(weights)
    conditioned = list(forecast_anchor)
    for index in range(realized_periods, SPOT_PERIODS):
        lead = index - realized_periods + 1
        decay = 0.88 ** (lead / 4.0)
        conditioned[index] = float(conditioned[index]) * (1.0 + bias_state * decay)
    return conditioned


def build_forecast_snapshots(
    config: IndustrialParkConfig = IndustrialParkConfig(),
    volatility: str = "high",
    scenario_seed: int = DEFAULT_LOAD_SCENARIO_SEED,
    actual_overrides: Sequence[Optional[float]] = (),
) -> List[dict]:
    if volatility not in {"low", "high"}:
        raise ValueError("volatility 只能为 low 或 high")
    multiplier = 1.0 if volatility == "high" else 0.65
    snapshots = _forecast_rows_without_actual(config, volatility)
    forecast_anchor = expand_half_hour_energy(
        [
            value * 0.5 * config.actual_scale
            for value in industrial_park_baseline_mw(config)
        ]
    )
    actual, actual_sources = generate_actual_load_path(
        snapshots[-1], scenario_seed, actual_overrides
    )
    for snapshot in snapshots:
        for index, row in enumerate(snapshot["rows"]):
            row["actual_mwh"] = actual[index]
            row["actual_source"] = actual_sources[index]
        snapshot["quality"] = _quality(snapshot["rows"])
        snapshot["scenario_seed"] = int(scenario_seed)
    for current_period in range(1, SPOT_PERIODS + 1):
        realized_periods = current_period - 1
        label = "RT-%02d" % current_period
        published_at = datetime(2026, 9, 15, 0, 0, tzinfo=timezone(timedelta(hours=8))) + timedelta(
            minutes=15 * (current_period - 1)
        )
        rt_error_scale = 0.01 * multiplier
        rt_bias = 0.001 * multiplier
        conditioned_anchor = _real_time_anchor(
            actual,
            forecast_anchor,
            realized_periods,
            rt_error_scale,
            rt_bias,
        )
        rows = _curve_rows(
            "REAL_TIME",
            label,
            actual,
            conditioned_anchor,
            rt_error_scale,
            0.015 * multiplier,
            bias_ratio=rt_bias,
            realized_periods=realized_periods,
            p75_half_width=0.009 * multiplier,
        )
        for index, row in enumerate(rows):
            row["actual_source"] = actual_sources[index]
        snapshots.append(
            {
                "snapshot_id": "LOAD-%s" % label,
                "event": "REAL_TIME",
                "label": label,
                "published_at": published_at.isoformat(),
                "rt_period": current_period,
                "rows": rows,
                "quality": _quality(rows),
            }
        )
    return snapshots


def snapshot_index(snapshots: Iterable[dict]) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for snapshot in snapshots:
        key = snapshot["event"]
        if key == "REAL_TIME":
            key = snapshot["label"]
        result[key] = snapshot
    return result


def select_snapshot(snapshots: Sequence[dict], event: str, rt_period: int = 24) -> dict:
    key = "RT-%02d" % max(1, min(SPOT_PERIODS, rt_period)) if event == "REAL_TIME" else event
    try:
        return snapshot_index(snapshots)[key]
    except KeyError as exc:
        raise ValueError("未知预测事件: " + key) from exc


def quantile_curve(snapshot: Mapping[str, object], quantile: str) -> List[float]:
    if quantile not in {"P50", "P75", "P90"}:
        raise ValueError("target_quantile 只能为 P50、P75 或 P90")
    rows = snapshot["rows"]
    if not isinstance(rows, list):
        raise ValueError("预测快照 rows 无效")
    if quantile == "P50":
        return [float(row["p50_mwh"]) for row in rows]
    if quantile == "P90":
        return [float(row["p90_mwh"]) for row in rows]
    return [
        float(row["p75_mwh"])
        if "p75_mwh" in row
        else float(row["p50_mwh"]) + 0.625 * (float(row["p90_mwh"]) - float(row["p50_mwh"]))
        for row in rows
    ]


def public_snapshot(
    snapshot: Mapping[str, object],
    reveal_all_actual: bool = False,
    realized_limit: int = 0,
) -> dict:
    """Remove future meter values from a snapshot before it crosses the API boundary."""

    public = {key: value for key, value in snapshot.items() if key != "rows"}
    public_rows = []
    for source_row in snapshot["rows"]:
        row = dict(source_row)
        actual_is_visible = reveal_all_actual or (
            row.get("status") == "ACTUAL"
            and int(row["period"]) <= realized_limit
        )
        if not actual_is_visible:
            row["actual_mwh"] = None
            row["actual_source"] = None
        public_rows.append(row)
    public["rows"] = public_rows
    quality = dict(snapshot["quality"])
    realized_rows = [row for row in public_rows if row["actual_mwh"] is not None]
    if realized_rows:
        actual_total = sum(float(row["actual_mwh"]) for row in realized_rows)
        absolute_errors = [
            abs(float(row["p50_mwh"]) - float(row["actual_mwh"]))
            for row in realized_rows
        ]
        wape = sum(absolute_errors) / max(actual_total, 1e-9)
        quality.update(
            {
                "actual_total_mwh": round(actual_total, 3),
                "mae_mwh": round(sum(absolute_errors) / len(realized_rows), 4),
                "wape": round(wape, 4),
                "mape": round(wape, 4),
                "quality_scope": (
                    "FULL_DAY" if len(realized_rows) == len(public_rows) else "REALIZED_PREFIX"
                ),
                "realized_periods": len(realized_rows),
            }
        )
    else:
        quality.update(
            {
                "actual_total_mwh": None,
                "mae_mwh": None,
                "wape": None,
                "mape": None,
                "quality_scope": "NOT_AVAILABLE",
                "realized_periods": 0,
            }
        )
    public["quality"] = quality
    return public


def month_equivalent_days(config: IndustrialParkConfig = IndustrialParkConfig()) -> float:
    target = date.fromisoformat(config.target_date)
    _, days = calendar.monthrange(target.year, target.month)
    total = 0.0
    for day in range(1, days + 1):
        current = date(target.year, target.month, day)
        total += config.weekend_factor if current.weekday() >= 5 else 1.0
    return round(total, 6)


def ten_day_equivalent_days(config: IndustrialParkConfig = IndustrialParkConfig()) -> float:
    """Return weighted delivery days for the ten-day block containing target_date."""

    target = date.fromisoformat(config.target_date)
    _, month_days = calendar.monthrange(target.year, target.month)
    if target.day <= 10:
        start_day, end_day = 1, 10
    elif target.day <= 20:
        start_day, end_day = 11, 20
    else:
        start_day, end_day = 21, month_days
    total = 0.0
    for day in range(start_day, end_day + 1):
        current = date(target.year, target.month, day)
        total += config.weekend_factor if current.weekday() >= 5 else 1.0
    return round(total, 6)


def product_equivalent_days(
    event: str, config: IndustrialParkConfig = IndustrialParkConfig()
) -> float:
    """Map product quantities to the representative target delivery day."""

    if event in {"ANNUAL", "MONTHLY"}:
        return month_equivalent_days(config)
    if event == "TEN_DAY":
        return ten_day_equivalent_days(config)
    if event in {"D-3", "D-2", "D-1", "REAL_TIME"}:
        return 1.0
    raise ValueError("未知产品交割范围: " + event)


def monthly_curve(daily_curve: Sequence[float], config: IndustrialParkConfig) -> List[float]:
    factor = month_equivalent_days(config)
    return [value * factor for value in daily_curve]


def aggregate_spot_snapshot(snapshot: Mapping[str, object]) -> dict:
    """Map a 96-point forecast to the 48-point long-term product grid."""

    source_rows = snapshot["rows"]
    if len(source_rows) != SPOT_PERIODS:
        raise ValueError("现货预测快照必须有96个点")
    rows = []
    for index in range(0, SPOT_PERIODS, 2):
        left = source_rows[index]
        right = source_rows[index + 1]
        rows.append(
            {
                "event": snapshot["event"],
                "label": snapshot["label"],
                "period": index // 2 + 1,
                "time": left["time"],
                "p10_mwh": round(float(left["p10_mwh"]) + float(right["p10_mwh"]), 6),
                "p50_mwh": round(float(left["p50_mwh"]) + float(right["p50_mwh"]), 6),
                "p90_mwh": round(float(left["p90_mwh"]) + float(right["p90_mwh"]), 6),
                "actual_mwh": round(float(left["actual_mwh"]) + float(right["actual_mwh"]), 6),
                "status": "ACTUAL" if left["status"] == right["status"] == "ACTUAL" else "FORECAST",
            }
        )
    result = dict(snapshot)
    result["snapshot_id"] = str(snapshot["snapshot_id"]) + "-LT48"
    result["rows"] = rows
    result["quality"] = _quality(rows)
    result["grid"] = "LONG_TERM_48"
    return result
