from __future__ import annotations

from typing import List, Sequence


LONG_TERM_PERIODS = 48
SPOT_PERIODS = 96
LONG_TERM_HOURS = 0.5
SPOT_HOURS = 0.25


def spot_time(period: int) -> str:
    if not 1 <= period <= SPOT_PERIODS:
        raise ValueError("现货时段必须位于 [1, 96]")
    minutes = (period - 1) * 15
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def expand_half_hour_energy(values: Sequence[float]) -> List[float]:
    """Split each 30-minute MWh position equally across two spot intervals."""

    if len(values) != LONG_TERM_PERIODS:
        raise ValueError("中长期曲线必须有48个半小时时段")
    result: List[float] = []
    for value in values:
        result.extend((float(value) * 0.5, float(value) * 0.5))
    return result


def aggregate_quarter_hour_energy(values: Sequence[float]) -> List[float]:
    if len(values) != SPOT_PERIODS:
        raise ValueError("现货曲线必须有96个15分钟时段")
    return [float(values[index]) + float(values[index + 1]) for index in range(0, 96, 2)]


def expand_half_hour_price(values: Sequence[float]) -> List[float]:
    if len(values) != LONG_TERM_PERIODS:
        raise ValueError("中长期价格曲线必须有48个半小时时段")
    return [float(value) for value in values for _ in range(2)]
