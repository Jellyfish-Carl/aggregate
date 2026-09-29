"""Compatibility accessor for the internally maintained wholesale engine."""
from __future__ import annotations



def engine():
    from pifa_lingshou.service.simulator import simulate
    return simulate
