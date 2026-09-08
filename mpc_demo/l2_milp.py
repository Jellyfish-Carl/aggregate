"""Backward-compatible import shim.

The three-layer model's 96-point spot/storage optimizer lives in
``l3_milp``.  This module remains only for callers using the old filename.
"""

from .l3_milp import solve_l3_milp

solve_l2_milp = solve_l3_milp

__all__ = ["solve_l2_milp", "solve_l3_milp"]
