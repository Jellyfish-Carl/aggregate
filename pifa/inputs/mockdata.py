from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from inputs.load_forecast import IndustrialParkConfig, hidden_actual_mwh, industrial_park_baseline_mw
from utils.mock_scenario import (
    DEFAULT_PRICE_SCENARIO_SEED,
    apply_point_overrides,
)
from constants.timegrid import SPOT_PERIODS, expand_half_hour_energy, spot_time


SHANGHAI = timezone(timedelta(hours=8))
TARGET_MONTH = "2026-09"
RISK_LAMBDA = 0.25
RISK_ALPHA = 0.95
MIDDAY_PV_WINDOW = (11.0, 13.0)
HIGH_PRICE_WINDOWS = ((9.0, 11.0), (13.0, 17.0))
ROLLING_ORDER_EDGE_BASE = {
    "FAVORABLE": 92.0,
    "MARGINAL": 55.0,
    "UNFAVORABLE": 20.0,
}
PRICE_P10_Z = 1.2815515655446004
PRICE_DA_TRUTH_SEED_OFFSET = 1009
PRICE_SPREAD_TRUTH_SEED_OFFSET = 2017
PRICE_COPULA_CORRELATION = 0.75

PRICE_FORECAST_EVENTS: Sequence[tuple] = (
    ("D-3", "2026-09-12T10:00:00+08:00", 0.070, 0.080, -0.018),
    ("D-2", "2026-09-13T10:00:00+08:00", 0.050, 0.060, -0.008),
    ("D-1", "2026-09-14T09:30:00+08:00", 0.035, 0.045, -0.002),
)


def month_intervals() -> List[str]:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=SHANGHAI)
    return [(start + timedelta(minutes=30 * index)).isoformat() for index in range(1440)]


def interval_index(interval: str) -> int:
    current = datetime.fromisoformat(interval)
    start = datetime(2026, 9, 1, 0, 0, tzinfo=SHANGHAI)
    return int((current - start).total_seconds() // 1800)


def period_in_day(index: int) -> int:
    return index % 48 + 1


def _smooth_noise(
    seed: int, count: int, scale: float, persistence: float = 0.78
) -> List[float]:
    """Create an independent, persistent error path for mock truth."""

    rng = random.Random(int(seed))
    innovation_scale = math.sqrt(1.0 - persistence * persistence)
    state = rng.gauss(0.0, 1.0)
    values = []
    for _ in range(count):
        state = persistence * state + innovation_scale * rng.gauss(0.0, 1.0)
        values.append(scale * state)
    return values


def _interpolated_level(hour: float) -> float:
    """Smooth the dispatch-level changes around hour boundaries."""

    points = (
        (0.0, 275.0),
        (6.0, 430.0),
        (9.0, 565.0),
        (11.0, 325.0),
        (13.0, 545.0),
        (17.0, 465.0),
        (21.0, 455.0),
        (23.0, 315.0),
        (24.0, 275.0),
    )
    for index in range(1, len(points)):
        left_hour, left_value = points[index - 1]
        right_hour, right_value = points[index]
        width = min(0.75, (right_hour - left_hour) / 3.0)
        start = right_hour - width / 2.0
        end = right_hour + width / 2.0
        if hour < start:
            return left_value
        if hour <= end:
            progress = (hour - start) / width
            smooth = progress * progress * (3.0 - 2.0 * progress)
            return left_value + (right_value - left_value) * smooth
    return points[-1][1]


def _base_price_curves() -> Dict[str, List[float]]:
    annual: List[float] = []
    monthly: List[float] = []
    day_ahead: List[float] = []
    real_time: List[float] = []
    for period in range(1, SPOT_PERIODS + 1):
        hour = (period - 1) / 4.0
        da = _interpolated_level(hour)
        wave = 18.0 * math.sin(2.0 * math.pi * (period + 8) / SPOT_PERIODS)
        pv_relief = 0.0
        if 10.5 <= hour < 13.5:
            pv_relief = 70.0 + 110.0 * math.sin(
                math.pi * (hour - 10.5) / 3.0
            )
        annual.append(405.0 + 0.20 * wave)
        monthly.append(418.0 + 0.35 * wave)
        day_ahead.append(da + 0.30 * wave)
        real_time.append(
            da
            + 0.65 * wave
            + 14.0 * math.sin(6.0 * math.pi * period / SPOT_PERIODS)
            - pv_relief
        )
    return {
        "annual": [round(value, 3) for value in annual],
        "monthly": [round(value, 3) for value in monthly],
        "day_ahead": [round(value, 3) for value in day_ahead],
        "real_time": [round(value, 3) for value in real_time],
    }


def _official_price_curves(
    seed: int = DEFAULT_PRICE_SCENARIO_SEED,
) -> Dict[str, List[float]]:
    """Generate hidden truth independently from the forecast snapshots."""

    base = _base_price_curves()
    day_ahead_noise = _smooth_noise(
        int(seed) + PRICE_DA_TRUTH_SEED_OFFSET, SPOT_PERIODS, 9.0, 0.82
    )
    spread_noise = _smooth_noise(
        int(seed) + PRICE_SPREAD_TRUTH_SEED_OFFSET, SPOT_PERIODS, 14.0, 0.76
    )
    day_ahead = [
        base["day_ahead"][index] + day_ahead_noise[index]
        for index in range(SPOT_PERIODS)
    ]
    spread = [
        base["real_time"][index] - base["day_ahead"][index] + spread_noise[index]
        for index in range(SPOT_PERIODS)
    ]
    real_time = [day_ahead[index] + spread[index] for index in range(SPOT_PERIODS)]
    return {
        "annual": list(base["annual"]),
        "monthly": list(base["monthly"]),
        "day_ahead": [round(value, 3) for value in day_ahead],
        "real_time": [round(value, 3) for value in real_time],
    }


def _price_forecast_rows(
    event: str,
    published_at: str,
    da_width: float,
    spread_width: float,
    bias: float,
) -> dict:
    base = _base_price_curves()
    # Forecast revisions share the same latent model error and damp it as the
    # delivery day approaches. Hidden truth uses separate seeds above.
    forecast_seed = 2026091520
    da_noise = _smooth_noise(forecast_seed, SPOT_PERIODS, da_width * 180.0, 0.86)
    spread_noise = _smooth_noise(
        forecast_seed + 97, SPOT_PERIODS, spread_width * 220.0, 0.80
    )
    rows = []
    for index in range(SPOT_PERIODS):
        period = index + 1
        shape = math.sin(2.0 * math.pi * (period + 5) / SPOT_PERIODS)
        shape += 0.35 * math.cos(6.0 * math.pi * (period - 3) / SPOT_PERIODS)
        reference_da = base["day_ahead"][index]
        reference_spread = base["real_time"][index] - reference_da
        da_p50 = max(
            -500.0,
            reference_da * (1.0 + bias + 0.25 * da_width * shape) + da_noise[index],
        )
        spread_p50 = (
            reference_spread
            + 35.0 * bias
            + 45.0 * spread_width * shape
            + spread_noise[index]
        )
        da_half = max(18.0, abs(da_p50) * da_width)
        spread_half = max(8.0, 140.0 * spread_width)
        rt_p50 = da_p50 + spread_p50
        da_sigma = da_half / PRICE_P10_Z
        spread_sigma = spread_half / PRICE_P10_Z
        rt_half = PRICE_P10_Z * math.sqrt(
            da_sigma * da_sigma
            + spread_sigma * spread_sigma
            + 2.0 * PRICE_COPULA_CORRELATION * da_sigma * spread_sigma
        )
        rows.append(
            {
                "period": period,
                "time": spot_time(period),
                "day_ahead_p10": round(da_p50 - da_half, 3),
                "day_ahead_p50": round(da_p50, 3),
                "day_ahead_p90": round(da_p50 + da_half, 3),
                "spread_p10": round(spread_p50 - spread_half, 3),
                "spread_p50": round(spread_p50, 3),
                "spread_p90": round(spread_p50 + spread_half, 3),
                "real_time_p10": round(rt_p50 - rt_half, 3),
                "real_time_p50": round(rt_p50, 3),
                "real_time_p90": round(rt_p50 + rt_half, 3),
            }
        )
    return {
        "snapshot_id": "PRICE-" + event.replace("-", ""),
        "event": event,
        "published_at": published_at,
        "rows": rows,
    }


def build_price_forecasts() -> Mapping[str, dict]:
    return {
        event: _price_forecast_rows(event, published_at, da_width, spread_width, bias)
        for event, published_at, da_width, spread_width, bias in PRICE_FORECAST_EVENTS
    }


def build_rolling_order_book(
    event: str,
    price_snapshot: Mapping[str, object],
    quantity_mwh: float = 0.25,
) -> dict:
    """Create deterministic counterparty orders for the D-3/D-2 mock book.

    ``SELL`` means the counterparty offers energy and we may buy it; ``BUY``
    means the counterparty wants energy and we may sell it.  Orders are placed
    around the photovoltaic trough and duck-curve high-price windows so the rolling rule
    can compare each order with the matching real-time P50 forecast.
    """

    if event not in {"D-3", "D-2"}:
        raise ValueError("滚撮订单只适用于 D-3 或 D-2")
    rows = price_snapshot["rows"]
    orders = []
    arrival_sequence = 0
    arrival_date = "2026-09-12" if event == "D-3" else "2026-09-13"
    tiers = ("FAVORABLE", "MARGINAL", "UNFAVORABLE")
    event_phase = 0.2 if event == "D-3" else 1.1
    for half_hour in range(48):
        time = str(rows[2 * half_hour]["time"])
        hour = half_hour / 2.0
        if MIDDAY_PV_WINDOW[0] <= hour < MIDDAY_PV_WINDOW[1]:
            da = 0.5 * (
                float(rows[2 * half_hour]["day_ahead_p50"])
                + float(rows[2 * half_hour + 1]["day_ahead_p50"])
            )
            rt = 0.5 * (
                float(rows[2 * half_hour]["real_time_p50"])
                + float(rows[2 * half_hour + 1]["real_time_p50"])
            )
            rt_p10 = 0.5 * (
                float(rows[2 * half_hour]["real_time_p10"])
                + float(rows[2 * half_hour + 1]["real_time_p10"])
            )
            rt_p90 = 0.5 * (
                float(rows[2 * half_hour]["real_time_p90"])
                + float(rows[2 * half_hour + 1]["real_time_p90"])
            )
            for tier_index, tier in enumerate(tiers, start=1):
                edge = (
                    ROLLING_ORDER_EDGE_BASE[tier]
                    + 18.0 * math.sin(0.73 * (half_hour + 1) + event_phase)
                    + 4.0 * math.cos(0.31 * (half_hour + 1) + event_phase)
                )
                arrival_sequence += 1
                order_price = rt - edge
                minute = 5 + tier_index * 4 + half_hour
                orders.append(
                    {
                        "order_id": "%s-PV-%02d-%s" % (event, half_hour + 1, tier[0]),
                        "arrival_sequence": arrival_sequence,
                        "arrival_time": "%sT%02d:%02d:00+08:00" % (
                            arrival_date, 10 + minute // 60, minute % 60
                        ),
                        "valid_until": "%sT15:00:00+08:00" % arrival_date,
                        "period": half_hour + 1,
                        "time": time,
                        "price_tier": tier,
                        "counterparty_side": "SELL",
                        "our_side": "BUY",
                        "quantity_mwh": round(float(quantity_mwh), 3),
                        "price_yuan_per_mwh": round(order_price, 3),
                        "spot_price_edge_yuan_per_mwh": round(edge, 3),
                        "day_ahead_reference_yuan_per_mwh": round(da, 3),
                        "real_time_reference_yuan_per_mwh": round(rt, 3),
                        "real_time_p10_yuan_per_mwh": round(rt_p10, 3),
                        "real_time_p90_yuan_per_mwh": round(rt_p90, 3),
                        "reason": "午间光伏大发低价卖单-%s档" % tier,
                    }
                )
        elif any(start <= hour < end for start, end in HIGH_PRICE_WINDOWS):
            da = 0.5 * (
                float(rows[2 * half_hour]["day_ahead_p50"])
                + float(rows[2 * half_hour + 1]["day_ahead_p50"])
            )
            rt = 0.5 * (
                float(rows[2 * half_hour]["real_time_p50"])
                + float(rows[2 * half_hour + 1]["real_time_p50"])
            )
            rt_p10 = 0.5 * (
                float(rows[2 * half_hour]["real_time_p10"])
                + float(rows[2 * half_hour + 1]["real_time_p10"])
            )
            rt_p90 = 0.5 * (
                float(rows[2 * half_hour]["real_time_p90"])
                + float(rows[2 * half_hour + 1]["real_time_p90"])
            )
            for tier_index, tier in enumerate(tiers, start=1):
                edge = (
                    ROLLING_ORDER_EDGE_BASE[tier]
                    + 18.0 * math.sin(0.73 * (half_hour + 1) + event_phase)
                    + 4.0 * math.cos(0.31 * (half_hour + 1) + event_phase)
                )
                arrival_sequence += 1
                order_price = rt + edge
                minute = 5 + tier_index * 4 + half_hour
                orders.append(
                    {
                        "order_id": "%s-PEAK-%02d-%s" % (event, half_hour + 1, tier[0]),
                        "arrival_sequence": arrival_sequence,
                        "arrival_time": "%sT%02d:%02d:00+08:00" % (
                            arrival_date, 10 + minute // 60, minute % 60
                        ),
                        "valid_until": "%sT15:00:00+08:00" % arrival_date,
                        "period": half_hour + 1,
                        "time": time,
                        "price_tier": tier,
                        "counterparty_side": "BUY",
                        "our_side": "SELL",
                        "quantity_mwh": round(float(quantity_mwh), 3),
                        "price_yuan_per_mwh": round(order_price, 3),
                        "spot_price_edge_yuan_per_mwh": round(edge, 3),
                        "day_ahead_reference_yuan_per_mwh": round(da, 3),
                        "real_time_reference_yuan_per_mwh": round(rt, 3),
                        "real_time_p10_yuan_per_mwh": round(rt_p10, 3),
                        "real_time_p90_yuan_per_mwh": round(rt_p90, 3),
                        "reason": "鸭子曲线上午或午后高价买单-%s档" % tier,
                    }
                )
    return {
        "event": event,
        "unit": "MWh/30分钟产品",
        "order_count": len(orders),
        "orders": orders,
        "source": "MOCK_COUNTERPARTY_ORDER_BOOK",
        "midday_pv_window": ["11:00", "13:00"],
        "high_price_windows": [["09:00", "11:00"], ["13:00", "17:00"]],
    }


def price_curves(
    real_time_seed: int = DEFAULT_PRICE_SCENARIO_SEED,
    real_time_overrides: Sequence[Optional[float]] = (),
) -> Dict[str, List[float]]:
    """Generate hidden official curves independently from forecast bands."""

    reference = _official_price_curves(real_time_seed)
    generated = reference["real_time"]
    real_time, _sources = apply_point_overrides(
        generated,
        real_time_overrides,
        label="实际实时价格",
        minimum=-2000.0,
        maximum=5000.0,
    )
    reference["real_time"] = [round(value, 3) for value in real_time]
    return reference


def base_price_yuan(period: int) -> float:
    return price_curves()["real_time"][period - 1]


def load_rows(config: IndustrialParkConfig = IndustrialParkConfig()) -> Iterable[Dict[str, object]]:
    baseline = [value * 0.5 for value in industrial_park_baseline_mw(config)]
    actual = hidden_actual_mwh(config)
    for index, interval in enumerate(month_intervals()):
        day = index // 48 + 1
        period = period_in_day(index)
        current = datetime.fromisoformat(interval)
        day_factor = config.weekend_factor if current.weekday() >= 5 else 1.0
        yield {
            "interval": interval,
            "day": day,
            "period": period,
            "baseline_mwh": round(baseline[period - 1] * day_factor, 6),
            "hidden_actual_mwh": round(actual[period - 1] * day_factor, 6),
            "source_type": "MOCK",
        }


def september_15_profile(config: IndustrialParkConfig = IndustrialParkConfig()) -> List[Dict[str, float]]:
    baseline = expand_half_hour_energy([value * 0.5 for value in industrial_park_baseline_mw(config)])
    actual = expand_half_hour_energy(hidden_actual_mwh(config))
    prices = price_curves()
    return [
        {
            "period": index + 1,
            "hour": index / 4.0,
            "baseline_load": round(baseline[index], 6),
            "actual_load": round(actual[index], 6),
            "day_ahead_price": prices["day_ahead"][index],
            "real_time_price": prices["real_time"][index],
        }
        for index in range(SPOT_PERIODS)
    ]
