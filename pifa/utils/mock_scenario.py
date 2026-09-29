from __future__ import annotations

import math
import random
from typing import List, Mapping, Optional, Sequence, Tuple


DEFAULT_LOAD_SCENARIO_SEED = 2026091501
DEFAULT_PRICE_SCENARIO_SEED = 2026091502


def seeded_band_path(
    rows: Sequence[Mapping[str, object]],
    lower_key: str,
    center_key: str,
    upper_key: str,
    seed: int,
    persistence: float = 0.82,
) -> List[float]:
    """Generate a smooth, reproducible path inside pointwise forecast bands."""

    if not 0.0 <= persistence < 1.0:
        raise ValueError("persistence 必须位于 [0, 1)")
    rng = random.Random(int(seed))
    state = rng.uniform(-0.25, 0.25)
    innovation_scale = math.sqrt(1.0 - persistence * persistence)
    values: List[float] = []
    for row in rows:
        state = persistence * state + innovation_scale * rng.gauss(0.0, 0.42)
        state = max(-1.0, min(1.0, state))
        lower = float(row[lower_key])
        center = float(row[center_key])
        upper = float(row[upper_key])
        width = upper - center if state >= 0.0 else center - lower
        values.append(center + state * max(width, 0.0))
    return values


def apply_point_overrides(
    generated: Sequence[float],
    overrides: Sequence[Optional[float]] = (),
    *,
    label: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Tuple[List[float], List[str]]:
    """Apply nullable point overrides while retaining source diagnostics."""

    if overrides and len(overrides) != len(generated):
        raise ValueError("%s人工覆盖必须包含%d个点" % (label, len(generated)))
    values = [float(value) for value in generated]
    sources = ["SEEDED_RANDOM"] * len(values)
    if not overrides:
        return values, sources
    for index, override in enumerate(overrides):
        if override is None:
            continue
        value = float(override)
        if not math.isfinite(value):
            raise ValueError("%s第%d点不是有限数值" % (label, index + 1))
        if minimum is not None and value < minimum:
            raise ValueError("%s第%d点不得低于%s" % (label, index + 1, minimum))
        if maximum is not None and value > maximum:
            raise ValueError("%s第%d点不得高于%s" % (label, index + 1, maximum))
        values[index] = value
        sources[index] = "MANUAL_OVERRIDE"
    return values, sources
