import unittest
import importlib.util
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from pifa_lingshou.inputs.mockdata import build_demo_input
from pifa_lingshou.accounting.retail import settle_scenario, weighted_cvar, weighted_quantile
from pifa_lingshou.utils.report.renderer import render_report
from pifa_lingshou.problem_solver.retail_wholesale_milp import solve_candidate_milp
from pifa_lingshou.service.evaluate import evaluate, select_plan


class RetailSettlementTests(unittest.TestCase):
    def test_demo_input_validates_and_fixed_package_is_deterministic(self):
        inputs = build_demo_input()
        inputs.validate()
        settlement = settle_scenario(inputs, "F", 1)
        self.assertEqual(len(settlement.customer_prices["C1"]), 96)
        self.assertAlmostEqual(settlement.customer_prices["C1"][0], 455.0)
        self.assertGreater(settlement.retail_revenue_yuan, 0.0)

    def test_demo_baseline_bills_use_the_same_daily_energy_horizon(self):
        inputs = build_demo_input()
        for customer in inputs.customers:
            expected_baseline = sum(
                scenario.probability
                * sum(scenario.customer_load_mwh[customer.customer_id])
                * customer.share_base_price
                for scenario in inputs.scenarios
            )
            self.assertAlmostEqual(customer.base_bill_yuan, expected_baseline)

    def test_plan_tiebreaker_does_not_reward_higher_retail_revenue(self):
        lower_bill = {
            "expected_profit_yuan": 100.0,
            "profit_loss_cvar_yuan": 20.0,
            "expected_retail_revenue_yuan": 140.0,
            "customer_results": {"C1": {"expected_saving_yuan": 10.0}},
        }
        higher_bill = {
            "expected_profit_yuan": 100.0,
            "profit_loss_cvar_yuan": 20.0,
            "expected_retail_revenue_yuan": 160.0,
            "customer_results": {"C1": {"expected_saving_yuan": -10.0}},
        }
        self.assertIs(select_plan([higher_bill, lower_bill]), lower_bill)

    def test_market_linked_package_uses_scenario_spot_reference(self):
        inputs = build_demo_input()
        low = settle_scenario(inputs, "L", 0)
        high = settle_scenario(inputs, "L", 2)
        self.assertLess(
            low.customer_prices["C1"][40],
            high.customer_prices["C1"][40],
        )

    def test_weighted_statistics(self):
        values = [1.0, 4.0, 10.0]
        probabilities = [0.25, 0.5, 0.25]
        self.assertEqual(weighted_quantile(values, probabilities, 0.1), 1.0)
        self.assertEqual(weighted_quantile(values, probabilities, 0.9), 10.0)
        self.assertAlmostEqual(weighted_cvar(values, probabilities, 0.5), 7.0)

    def test_non_finite_inputs_are_rejected(self):
        inputs = build_demo_input()
        broken = list(inputs.annual_reference_price)
        broken[4] = float("nan")
        with self.assertRaisesRegex(ValueError, "有限数值"):
            from dataclasses import replace

            replace(inputs, annual_reference_price=broken).validate()

    def test_model_builds_ninety_six_point_plan_and_scenario_ledgers(self):
        def fake_solve(model, time_limit_seconds=15.0, mip_relative_gap=1e-3):
            return SimpleNamespace(
                status="OPTIMAL",
                objective=0.0,
                values={name: 0.0 for name in model._names},
                mip_gap=0.0,
                solve_seconds=0.0,
                variable_count=model.variable_count,
                binary_count=model.binary_count,
                constraint_count=model.constraint_count,
                backend="test backend",
                message="model assembly smoke test",
            )

        with patch("pifa_lingshou.problem_solver.retail_wholesale_milp.LinearMilp.solve", fake_solve):
            result = solve_candidate_milp(build_demo_input(), "F", 0.25)
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(len(result["schedule"]), 96)
        self.assertEqual(len(result["contracts"]["total_position_mwh_per_half_hour"]), 48)
        self.assertEqual(len(result["scenario_results"]), 3)
        self.assertEqual(result["binary_count"], 192)
        self.assertEqual(result["package_selection_matrix"]["C1"], {"F": 1, "L": 0, "S": 0})

    def test_report_renders_comparison_tables_charts_and_storage_totals(self):
        row = {
            "period": 1,
            "aggregate_load_by_scenario_mwh": [0.2, 0.3],
            "declaration_mwh": 0.25,
            "contract_supply_mwh": 0.24,
            "charge_mwh": 0.01,
            "discharge_mwh": 0.0,
            "soc_mwh": 0.5,
        }
        result = {
            "package": "F",
            "risk_lambda": 0.5,
            "status": "OPTIMAL",
            "feasible": True,
            "package_selection_matrix": {"C1": {"F": 1, "L": 0, "S": 0}},
            "schedule": [row],
            "expected_profit_yuan": 100.0,
            "profit_p10_yuan": 90.0,
            "profit_p90_yuan": 110.0,
            "profit_loss_cvar_yuan": -90.0,
            "expected_wholesale_cost_yuan": 500.0,
            "expected_retail_revenue_yuan": 600.0,
            "customer_results": {
                "C1": {
                    "package": "F",
                    "expected_bill_yuan": 250.0,
                    "bill_p10_yuan": 230.0,
                    "bill_p90_yuan": 270.0,
                    "expected_saving_yuan": 50.0,
                    "saving_p10_yuan": 40.0,
                    "saving_p90_yuan": 60.0,
                    "allocated_profit_stats_yuan": {"mean": 50.0},
                }
            },
            "scenario_results": [
                {"scenario_id": "LOW", "probability": 0.25, "profit_yuan": 90.0},
                {"scenario_id": "HIGH", "probability": 0.75, "profit_yuan": 110.0},
            ],
        }
        report = render_report({"results": [result], "selected": result})
        for expected in (
            'id="wholesale-page"', 'id="retail-page"', 'id="defaults"',
            'id="customer-table"', 'id="storage-chart"',
            "售电公司总成本与收益", "λ–期望价差与预测区间",
        ):
            self.assertIn(expected, report)
        self.assertNotIn('最高期望利润候选', report)
        self.assertNotIn("@@", report)

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "requires SciPy/HiGHS")
    def test_real_milp_reconciles_accounts_and_storage_dispatch(self):
        baseline = solve_candidate_milp(build_demo_input(), "F", 0.0)
        self.assertEqual(baseline["status"], "OPTIMAL")
        self.assertAlmostEqual(
            baseline["expected_profit_yuan"],
            baseline["expected_retail_revenue_yuan"] - baseline["expected_wholesale_cost_yuan"],
            places=5,
        )
        self.assertLess(baseline["accounting_checks"]["max_energy_balance_residual_mwh"], 1e-6)
        self.assertLess(baseline["accounting_checks"]["max_soc_recursion_residual_mwh"], 1e-6)
        self.assertAlmostEqual(baseline["storage_summary"]["total_charge_mwh"], 0.0, places=6)
        self.assertAlmostEqual(baseline["storage_summary"]["total_discharge_mwh"], 0.0, places=6)
        expected_breakdown = baseline["wholesale_cost_breakdown"]["expected"]
        self.assertAlmostEqual(
            expected_breakdown["total_wholesale_cost_yuan"],
            sum(expected_breakdown[key] for key in (
                "contract_cost_yuan", "day_ahead_cost_yuan", "real_time_cost_yuan",
                "deviation_penalty_yuan", "storage_degradation_yuan",
            )),
            places=5,
        )

        sensitivity = solve_candidate_milp(build_demo_input("arbitrage"), "F", 0.0)
        sensitivity_inputs = build_demo_input("arbitrage")
        no_storage = solve_candidate_milp(replace(
            sensitivity_inputs,
            storage=replace(sensitivity_inputs.storage, maximum_charge_mwh=0.0, maximum_discharge_mwh=0.0),
        ), "F", 0.0)
        self.assertGreater(sensitivity["storage_summary"]["total_charge_mwh"], 0.0)
        self.assertGreater(sensitivity["storage_summary"]["total_discharge_mwh"], 0.0)
        self.assertAlmostEqual(
            sensitivity["storage_summary"]["terminal_soc_mwh"],
            sensitivity["storage_summary"]["initial_soc_mwh"],
            places=6,
        )
        self.assertGreater(sensitivity["expected_profit_yuan"], no_storage["expected_profit_yuan"])
        actual_report = render_report({
            "results": [baseline], "selected": baseline, "cvar_alpha": build_demo_input().cvar_alpha,
            "model_parameters": {
                "deviation_buy_penalty": 1000.0, "deviation_sell_penalty": 1000.0,
                "deviation_ratio_limit": 0.1, "transaction_friction_yuan_per_mwh": 1.0,
            },
        })
        for label in ("年度电能量", "月度电能量", "旬内电能量", "D-3滚撮电能量", "D-2滚撮电能量", "日前现货", "实时现货", "罚款/偏差考核", "储能退化"):
            self.assertIn(label, actual_report)
        self.assertNotIn("@@", actual_report)

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "requires SciPy/HiGHS")
    def test_all_candidates_match_independent_physical_and_financial_calculations(self):
        inputs = build_demo_input()
        payload = evaluate(inputs)
        self.assertEqual(len(payload["results"]), 9)
        for result in payload["results"]:
            with self.subTest(package=result["package"], risk_lambda=result["risk_lambda"]):
                self.assertEqual(result["status"], "OPTIMAL")
                self.assertTrue(result["feasible"])
                self.assertIn("HiGHS", result["backend"])
                contracts = result["contracts"]
                contract_cost = inputs.old_contract_cost_yuan
                for product, trade in zip(inputs.contract_products, contracts["trades_by_product"]):
                    contract_cost += sum(
                        buy * bp - sell * sp for buy, bp, sell, sp in zip(
                            trade["buy_mwh_per_half_hour"], product.buy_price,
                            trade["sell_mwh_per_half_hour"], product.sell_price,
                        )
                    )
                for h, demand in enumerate(inputs.monthly_delivery_mwh):
                    position = contracts["total_position_mwh_per_half_hour"][h]
                    annual = contracts["annual_position_mwh_per_half_hour"][h]
                    self.assertGreaterEqual(annual + 1e-7, inputs.annual_coverage_minimum * demand)
                    self.assertGreaterEqual(position + 1e-7, inputs.overall_coverage_minimum * demand)
                    self.assertLessEqual(position, inputs.overall_coverage_maximum * demand + 1e-7)
                costs = []
                for si, scenario in enumerate(inputs.scenarios):
                    cost = contract_cost
                    previous_soc = inputs.storage.initial_soc_mwh
                    for period, row in enumerate(result["schedule"]):
                        buy, sell = row["day_ahead_buy_mwh"], row["day_ahead_sell_mwh"]
                        rt_buy, rt_sell = row["real_time_buy_by_scenario_mwh"][si], row["real_time_sell_by_scenario_mwh"][si]
                        charge, discharge = row["charge_mwh"], row["discharge_mwh"]
                        load = sum(scenario.customer_load_mwh[c.customer_id][period] for c in inputs.customers)
                        self.assertAlmostEqual(row["declaration_mwh"] + rt_buy - rt_sell - charge + discharge, load, places=6)
                        self.assertAlmostEqual(row["declaration_mwh"] - buy + sell, row["contract_supply_mwh"], places=6)
                        self.assertAlmostEqual(row["contract_supply_mwh"], contracts["total_position_mwh_per_half_hour"][period // 2] / 2, places=6)
                        self.assertLessEqual(min(charge, discharge), 1e-7)
                        self.assertGreaterEqual(row["soc_mwh"] + 1e-7, inputs.storage.minimum_soc_mwh)
                        self.assertLessEqual(row["soc_mwh"], inputs.storage.maximum_soc_mwh + 1e-7)
                        self.assertAlmostEqual(row["soc_mwh"], previous_soc + charge * inputs.storage.efficiency - discharge / inputs.storage.efficiency, places=6)
                        previous_soc = row["soc_mwh"]
                        buy_slack = row["deviation_buy_slack_by_scenario_mwh"][si]
                        sell_slack = row["deviation_sell_slack_by_scenario_mwh"][si]
                        self.assertLessEqual(rt_buy - rt_sell - buy_slack, inputs.deviation_ratio_limit * load + 1e-7)
                        self.assertLessEqual(rt_sell - rt_buy - sell_slack, inputs.deviation_ratio_limit * load + 1e-7)
                        cost += scenario.day_ahead_price[period] * (buy - sell)
                        cost += scenario.real_time_price[period] * (rt_buy - rt_sell)
                        cost += inputs.transaction_friction_yuan_per_mwh * (buy + sell + rt_buy + rt_sell)
                        cost += inputs.deviation_buy_penalty * buy_slack + inputs.deviation_sell_penalty * sell_slack
                        cost += inputs.storage.degradation_yuan_per_mwh * (charge + discharge)
                    ledger = result["scenario_results"][si]
                    self.assertAlmostEqual(cost, ledger["wholesale_cost_yuan"], places=5)
                    revenue = settle_scenario(inputs, result["package"], si).retail_revenue_yuan
                    self.assertAlmostEqual(revenue - cost, ledger["profit_yuan"], places=5)
                    costs.append(cost)
                self.assertAlmostEqual(sum(c * s.probability for c, s in zip(costs, inputs.scenarios)), result["expected_wholesale_cost_yuan"], places=5)
                self.assertAlmostEqual(sum(c["expected_bill_yuan"] for c in result["customer_results"].values()), result["expected_retail_revenue_yuan"], places=5)
                self.assertAlmostEqual(sum(c["allocated_profit_stats_yuan"]["mean"] for c in result["customer_results"].values()), result["expected_profit_yuan"], places=5)
                self.assertAlmostEqual(result["objective_yuan"], -(1-result["risk_lambda"]) * result["expected_profit_yuan"] + result["risk_lambda"] * result["profit_loss_cvar_yuan"] + result["declaration_band_penalty_yuan"], places=5)

        report = render_report(payload)
        static_html = report.split('<script id="report-data"')[0]
        for raw_svg in re.findall(r"<svg\b.*?</svg>", static_html, re.S):
            ET.fromstring(raw_svg)
        class TableAudit(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []
                self.tables = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.ids.append(attrs["id"])
                if tag == "table":
                    self.tables.append([])
                elif tag == "tr":
                    self.tables[-1].append(0)
                elif tag in ("th", "td"):
                    self.tables[-1][-1] += int(attrs.get("colspan", 1))
        parsed = TableAudit()
        parsed.feed(report)
        self.assertEqual(len(parsed.ids), len(set(parsed.ids)))
        for table in parsed.tables:
            self.assertEqual(len(set(table)), 1, table)
        self.assertNotIn("@@", report)


if __name__ == "__main__":
    unittest.main()
