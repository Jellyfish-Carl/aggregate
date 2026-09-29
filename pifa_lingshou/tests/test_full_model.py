import unittest
from unittest.mock import patch

from pifa_lingshou.inputs.full_model import FullModelInput
from pifa_lingshou.service.wholesale import engine
from pifa_lingshou.service.full_evaluate import evaluate_full
from pifa_lingshou.service.storage_validation import compare_storage

engine()
from pifa_lingshou.problem_solver.l3_milp import solve_l3_milp
from pifa_lingshou.problem_solver.milp import LinearMilp, MilpSolveError
from pifa_lingshou.data_objects.scenario import JointTrajectoryScenario
from pifa_lingshou.inputs.load_forecast import IndustrialParkConfig, build_forecast_snapshots, snapshot_index
from pifa_lingshou.inputs.wholesale_mockdata import build_price_forecasts


class FullModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = FullModelInput(risk_lambdas=(0.0,))
        cls.payload = evaluate_full(cls.config, packages=('F',))
        cls.result = cls.payload['results'][0]

    def test_complete_chain_and_customer_interval_allocation(self):
        r = self.result
        status = r['wholesale_engine']['meta']['milp']
        for key in ('l1_executed', 'l2_rule_executed', 'l3_executed'):
            self.assertTrue(status[key])
        self.assertNotIn('selected', self.payload)
        self.assertEqual(len(r['scenario_results']), 64)
        for value in r['accounting_checks'].values():
            self.assertLess(abs(value), 1e-6)
        for si, scenario in enumerate(r['scenario_results']):
            costs = revenues = 0.0
            for ci, cust in enumerate(self.config.customers):
                customer = r['customer_results'][cust.customer_id]['scenarios'][si]
                expected_cost = expected_revenue = 0.0
                for j, period in enumerate(scenario['periods']):
                    q = period['load_mwh'] * self.config.customer_shares[ci][j]
                    cost = q * period['wholesale_cost_yuan'] / period['load_mwh']
                    revenue = q * period['retail_revenue_yuan'] / period['load_mwh']
                    self.assertAlmostEqual(customer['periods'][j]['cost_yuan'], cost, places=7)
                    self.assertAlmostEqual(customer['periods'][j]['revenue_yuan'], revenue, places=7)
                    expected_cost += cost
                    expected_revenue += revenue
                self.assertAlmostEqual(customer['cost_yuan'], expected_cost, places=6)
                self.assertAlmostEqual(customer['profit_yuan'], expected_revenue - expected_cost, places=6)
                costs += expected_cost
                revenues += expected_revenue
            self.assertAlmostEqual(costs, scenario['wholesale_cost_yuan'], places=6)
            self.assertAlmostEqual(revenues, scenario['retail_revenue_yuan'], places=6)
        # Different time-of-use load shapes must not receive identical buy prices.
        a, b = r['customer_results'].values()
        self.assertGreater(abs(a['expected_buy_price_yuan_per_mwh'] - b['expected_buy_price_yuan_per_mwh']), 1)

    def test_default_storage_saves_cost_with_identical_locked_contracts(self):
        r = self.result
        comparison = compare_storage(r)
        self.assertEqual(comparison['status'], 'OPTIMAL')
        self.assertGreater(comparison['cost_reduction_yuan'], 100)
        storage = r['storage_summary']
        self.assertGreater(storage['total_charge_mwh'], 0)
        self.assertGreater(storage['average_discharge_price_yuan_per_mwh'], storage['average_charge_price_yuan_per_mwh'])
        self.assertAlmostEqual(storage['total_discharge_mwh'], storage['total_charge_mwh'] * .92**2, places=7)

    def test_realtime_buy_sell_are_exclusive_in_every_scenario(self):
        r = self.result
        self.assertEqual(r['status'], 'OPTIMAL')
        opt = r['wholesale_engine']['l3_objective']
        self.assertEqual(opt['binary_count'], 96)
        for row in r['schedule']:
            for buy, sell in zip(row['real_time_buy_by_scenario_mwh'], row['real_time_sell_by_scenario_mwh']):
                self.assertGreaterEqual(min(buy, sell), -1e-8)
                self.assertLessEqual(min(buy, sell), 1e-8)

    def test_realtime_mutual_exclusion_is_a_hard_constraint(self):
        # Forcing a matched round trip preserves net energy balance but must
        # be infeasible even when the objective would tolerate it.
        prices = (100.0,) * 96
        scenario = JointTrajectoryScenario('L50_DA50_SP50', 1, 'P50', 'P50', 'P50', (1.0,) * 96, prices, prices)
        snapshot = {'rows': [dict(period=j+1, p10_mwh=1, p50_mwh=1, p90_mwh=1) for j in range(96)]}
        price_snapshot = {'rows': [dict(day_ahead_p10=100, day_ahead_p50=100, day_ahead_p90=100,
            real_time_p10=100, real_time_p50=100, real_time_p90=100) for _ in range(96)]}
        original_solve = LinearMilp.solve
        def force_round_trip(model, **kwargs):
            model.add_constraint({'rt_buy_00_00': 1.0}, lower=.05)
            model.add_constraint({'rt_sell_00_00': 1.0}, lower=.05)
            return original_solve(model, **kwargs)
        with patch.object(LinearMilp, 'solve', force_round_trip), patch('pifa_lingshou.problem_solver.l3_milp.joint_scenario_diagnostics', return_value={}):
            for builder in (None, lambda scenarios: [0.0 for _ in scenarios]):
                with self.subTest(retail_coupled=builder is not None), self.assertRaisesRegex(MilpSolveError, 'INFEASIBLE'):
                    solve_l3_milp(snapshot, price_snapshot, [1]*96, 0, 20, prices, prices, 1, True,
                        joint_scenarios=(scenario,), storage_power_override_mw=0, locked_declaration=(1.0,)*96,
                        retail_revenue_builder=builder)

    def test_deterministic_low_charge_high_discharge_matches_cash_saving(self):
        prices = (100.0,) * 48 + (800.0,) * 48
        scenario = JointTrajectoryScenario('L50_DA50_SP50', 1, 'P50', 'P50', 'P50', (1.0,) * 96, prices, prices)
        snapshot = {'rows': [dict(period=j+1, p10_mwh=1, p50_mwh=1, p90_mwh=1) for j in range(96)]}
        price_snapshot = {'rows': [dict(day_ahead_p10=p, day_ahead_p50=p, day_ahead_p90=p,
            real_time_p10=p, real_time_p50=p, real_time_p90=p, spread_p10=0, spread_p50=0, spread_p90=0) for p in prices]}
        with patch('pifa_lingshou.problem_solver.l3_milp.reduced_joint_trajectories', return_value=(scenario,)), patch('pifa_lingshou.problem_solver.l3_milp.joint_scenario_diagnostics', return_value={}):
            enabled = solve_l3_milp(snapshot, price_snapshot, [1]*96, 0, 20, prices, prices, 1, True)
            disabled = solve_l3_milp(snapshot, price_snapshot, [1]*96, 0, 20, prices, prices, 1, True, storage_power_override_mw=0)
        raw = enabled['raw_solution']
        self.assertAlmostEqual(sum(raw['charge_mwh']), 4.8, places=6)
        self.assertAlmostEqual(sum(raw['discharge_mwh']), 4.8 * .92**2, places=6)
        self.assertAlmostEqual(sum(raw['charge_mwh'][48:]), 0, places=6)
        self.assertAlmostEqual(sum(raw['discharge_mwh'][:48]), 0, places=6)
        self.assertAlmostEqual(raw['soc_mwh'][-1], 10, places=6)
        saving = disabled['raw_solution']['scenarios'][0]['wholesale_cost_yuan'] - raw['scenarios'][0]['wholesale_cost_yuan']
        expected = 800 * 4.8 * .92**2 - 100 * 4.8 - 2.01 * (4.8 + 4.8 * .92**2)
        self.assertAlmostEqual(saving, expected, places=6)


if __name__ == '__main__':
    unittest.main()
