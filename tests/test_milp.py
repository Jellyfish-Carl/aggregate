import unittest

from mpc_demo.milp import LinearMilp
from mpc_demo.simulator import simulate


@unittest.skipUnless(LinearMilp.available(), "SciPy/HiGHS is not installed")
class MilpIntegrationTests(unittest.TestCase):
    def test_binary_model_is_solved_by_highs(self):
        model = LinearMilp("binary-smoke")
        model.add_var("x", upper=1.0, objective=-3.0, integer=True)
        model.add_var("y", upper=1.0, objective=-2.0, integer=True)
        model.add_constraint({"x": 1.0, "y": 1.0}, upper=1.0)
        result = model.solve()
        self.assertEqual(result.status, "OPTIMAL")
        self.assertAlmostEqual(result.values["x"], 1.0)
        self.assertAlmostEqual(result.values["y"], 0.0)
        self.assertEqual(result.binary_count, 2)

    def test_full_demo_executes_l1_and_l3_milps_with_l2_rule_engine(self):
        payload = simulate(event="D-1")
        status = payload["meta"]["milp"]
        self.assertTrue(status["executed"])
        self.assertTrue(status["l1_executed"])
        self.assertTrue(status["l2_rule_executed"])
        self.assertEqual(status["l2_solver"], "RULE_ENGINE")
        self.assertLessEqual(status["l3_mip_gap"], 0.001)
        self.assertLessEqual(status["l3_end_to_end_seconds"], 10.0)
        self.assertGreater(payload["l3_objective"]["binary_count"], 0)
        self.assertGreater(payload["l3_objective"]["constraint_count"], 0)

    def test_d2_order_book_keeps_d3_fills_and_reports_order_acceptance(self):
        payload = simulate(event="D-2")
        book = payload["portfolio"]["rolling_orders"]
        self.assertEqual(book["selected_event"], "D-2")
        self.assertEqual(book["order_count"], 48)
        self.assertTrue(any(item["accepted"] for item in book["orders"]))
        fills = payload["portfolio"]["optimized"]["fills"]
        self.assertTrue(any(item["product_class"] == "D-3" for item in fills))
        self.assertTrue(any(item["product_class"] == "D-2" for item in fills))
        d2_action = next(
            item for item in payload["portfolio"]["optimized"]["actions"]
            if item["event"] == "D-2"
        )
        self.assertEqual(d2_action["solver_status"], "OPTIMAL")
        self.assertEqual(len(d2_action["order_decisions"]), 48)
        self.assertEqual(
            d2_action["valuation_summary"]["order_selection_method"],
            "SEQUENTIAL_SPOT_EDGE_PARTIAL_FILL",
        )
        self.assertTrue(
            all(
                item["accepted_quantity_mwh"] == 0.0
                or 0.10 - 1e-9 <= item["accepted_fill_ratio"] <= 0.20 + 1e-9
                for item in d2_action["order_decisions"]
            )
        )
        self.assertGreater(d2_action["buy_quantity_mwh"], 0.0)
        self.assertGreater(d2_action["sell_quantity_mwh"], 0.0)

    def test_l2_contract_curve_changes_day_ahead_net_trade_and_exposes_pricing_lp(self):
        payload = simulate(event="D-2")
        declaration = payload["declaration"]
        self.assertNotEqual(declaration["locked_contract_total_mwh"], 0.0)
        for row in declaration["rows"]:
            self.assertAlmostEqual(
                row["locked_contract_mwh"] + row["day_ahead_net_mwh"],
                row["declared_mwh"],
                delta=1e-4,
            )
        pricing = payload["l3_objective"]["pricing_lp"]
        self.assertEqual(pricing["status"], "OPTIMAL")
        self.assertEqual(
            len(payload["l3_objective"]["rolling_half_hour_marginal_value_yuan_per_mwh"]),
            48,
        )

    def test_three_layer_structure_and_l3_constraints_are_explicit(self):
        payload = simulate(event="D-1")
        self.assertEqual(
            payload["meta"]["model_structure"],
            "L1_CONTRACT_MILP_PLUS_L2_ROLLING_TRIGGER_PLUS_L3_96_POINT_JOINT_SCENARIO_MPC",
        )
        layers = {item["event"]: item["layer"] for item in payload["timeline"]}
        self.assertEqual(layers["ANNUAL"], "L1")
        self.assertEqual(layers["D-3"], "L2")
        self.assertEqual(layers["D-2"], "L2")
        self.assertEqual(layers["D-1"], "L3")
        self.assertEqual(layers["REAL_TIME"], "L3")
        self.assertTrue(payload["meta"]["milp"]["l3_executed"])
        self.assertEqual(payload["l3_objective"]["layer"], "L3")
        constraints = payload["l3_objective"]["constraint_definition"]
        self.assertIn("充放电二进制互斥", constraints["storage_mode"])
        self.assertIn("10%", constraints["execution_deviation_band"])
        self.assertEqual(
            payload["storage"]["rows"][-1]["soc_mwh"],
            payload["storage"]["rows"][0]["soc_start_mwh"],
        )


class MilpConfigurationTests(unittest.TestCase):
    def test_require_milp_does_not_silently_use_reference_path(self):
        if LinearMilp.available():
            self.skipTest("HiGHS available; integration test covers the strict path")
        with self.assertRaises(RuntimeError):
            simulate(event="D-1")

    def test_diagnostic_policy_requires_explicit_opt_in(self):
        if LinearMilp.available():
            self.skipTest("Only relevant when the MILP dependency is absent")
        payload = simulate(event="D-1", require_milp=False, diagnostic_mode=True)
        self.assertEqual(payload["meta"]["model_status"], "DIAGNOSTIC_ONLY")
        self.assertFalse(payload["meta"]["milp"]["executed"])

    def test_conflicting_solver_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            simulate(event="D-1", require_milp=True, diagnostic_mode=True)


if __name__ == "__main__":
    unittest.main()
