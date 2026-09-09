"""Layer 2: D-3/D-2 rolling threshold trading.

Layer 2 is a rule engine, not a MILP. It applies the spot-P50 price-edge
trigger, the 90%-110% position band, and 10%-20% partial fills. L3 can supply
an optional marginal-value gate without changing this decision rule.
"""

from .trading import apply_l2_rolling_threshold, rolling_partial_fill_ratio

__all__ = ["apply_l2_rolling_threshold", "rolling_partial_fill_ratio"]
