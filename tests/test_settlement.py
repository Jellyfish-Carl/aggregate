import unittest

from mpc_demo.domain import ContractFill
from mpc_demo.settlement import (
    adjustment_energy_cost,
    annual_ratio_recovery,
    contract_difference_cost,
    day_ahead_recovery,
    over_profit_recovery,
    minimax_declaration_slack,
    robust_declaration_band,
)


class SettlementSpecificationTests(unittest.TestCase):
    def test_m06_fill_by_fill_contract_difference_preserves_round_trip(self):
        fills = [
            ContractFill("BUY-1", "ROLL", "E1", "BUY", 10.0, 400.0, 0.0, {"T1": 10.0}, True),
            ContractFill("SELL-1", "ROLL", "E2", "SELL", -10.0, 420.0, 0.0, {"T1": -10.0}, True),
        ]
        self.assertAlmostEqual(contract_difference_cost(fills, {"T1": 450.0}), -200.0)

    def test_b01_deviation_inside_band_has_no_recovery(self):
        self.assertEqual(day_ahead_recovery(100.0, 95.0, 450.0, 400.0), 0.0)

    def test_b03_annual_ratio_recovery(self):
        self.assertAlmostEqual(annual_ratio_recovery(760.0, 405.0, 412.0, 403.0), 481.95)

    def test_b04_low_contract_over_profit_recovery(self):
        self.assertAlmostEqual(over_profit_recovery(760.0, 615.0, 403.0, 390.0), 941.85)

    def test_b05_adjustment_energy_cost(self):
        self.assertAlmostEqual(adjustment_energy_cost(800.0, 760.0, [760.0], [390.9]), 15636.0)

    def test_m12_robust_declaration_band(self):
        lower, upper = robust_declaration_band(95.0, 105.0, 999.0)
        self.assertAlmostEqual(lower, 95.0)
        self.assertAlmostEqual(upper, 105.0)

    def test_declaration_band_is_capped_by_physical_upper(self):
        lower, upper = robust_declaration_band(4.0, 6.0, 5.0)
        self.assertAlmostEqual(lower, 4.0)
        self.assertAlmostEqual(upper, 5.0)

    def test_declaration_band_reports_physical_conflict_as_slack(self):
        lower, upper = robust_declaration_band(6.0, 8.0, 5.0)
        self.assertAlmostEqual(lower, 6.0)
        self.assertAlmostEqual(upper, 5.0)
        self.assertAlmostEqual(minimax_declaration_slack(6.0, 8.0, 5.0), 0.5)


if __name__ == "__main__":
    unittest.main()
