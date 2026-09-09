"""Layer 1: annual, monthly, and ten-day contract optimization.

The portfolio builder owns shared position and settlement bookkeeping; this
module is the explicit public name for its Layer 1 contract MILP entry point.
"""

from .trading import solve_l1_contract_milp

__all__ = ["solve_l1_contract_milp"]
