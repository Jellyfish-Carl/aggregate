"""Regression tests for the unified solver-test run.py entry point."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pifa_lingshou.problem_solver.milp import LinearMilp, MilpSolveError
from pifa_lingshou.solver_test.request_generation import (
    SCENARIO_COUNTS, build_request,
)
from pifa_lingshou.solver_test import run as runner
from pifa_lingshou.solver_test import request_generation as generation


class RequestConfigurationTests(unittest.TestCase):
    def test_batches_increment_and_downstream_keeps_its_test_batch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(generation, 'RESOURCE', Path(tmp)):
            (Path(tmp) / 'input/solver_test/request_07').mkdir(parents=True)
            request = {'node': 'ANNUAL', 'benchmark': {'scenario_count': 5}}
            first = generation.save_request(request)
            second = generation.save_request(request)
            self.assertEqual(first.parent.name, 'request_08')
            self.assertEqual(second.parent.name, 'request_09')
            upstream = Path(tmp) / 'output/solver_test/response_08/annual/run_1/response.json'
            monthly = generation.save_request(dict(request, node='MONTHLY',
                                                   upstream_response_file=str(upstream)))
            self.assertEqual(monthly.parent, first.parent)
            with self.assertRaisesRegex(ValueError, '请新建测试批次'):
                generation.save_request({'node': 'ANNUAL', 'benchmark': {'scenario_count': 20}},
                                        run_number=8)

    def test_requested_customers_scenarios_and_limits_are_enforced(self):
        for count in (5, 500):
            request = build_request("ANNUAL", customer_count=10, scenario_count=count)
            self.assertEqual(len(request["inputs"]["customers"]), 10)
            self.assertEqual(request["inputs"]["scenario_count"], count)
            self.assertEqual(request["inputs"]["storage"]["optimization_scenario_count"], count)
            self.assertTrue(request["inputs"]["storage"]["full_scenario_optimization"])
            self.assertEqual(request["inputs"]["storage"]["time_limit_seconds"], 60.)
            self.assertEqual(request["inputs"]["l1_solver"]["time_limit_seconds"], 60.)
        self.assertEqual(SCENARIO_COUNTS, (5, 20, 50, 100, 200, 300, 500))
        for customers, scenarios in ((1, 7), (2, 64), (11, 5), (20, 8)):
            request = build_request('ANNUAL', customer_count=customers, scenario_count=scenarios)
            self.assertEqual(len(request['inputs']['customers']), customers)
            self.assertEqual(len(request['inputs']['customer_storage']), customers)
            self.assertEqual(request['inputs']['scenario_count'], scenarios)
        with self.assertRaisesRegex(ValueError, "客户数量"):
            build_request("ANNUAL", customer_count=0, scenario_count=5)

    def test_watchdog_failure_retains_constructed_model_size(self):
        model = LinearMilp("metrics-on-timeout")
        model.add_var("binary", integer=True)
        model.add_constraint({"binary": 1}, lower=0, upper=1)
        with patch("pifa_lingshou.problem_solver.milp._run_isolated",
                   side_effect=MilpSolveError("TIME_LIMIT_REACHED")):
            with self.assertRaises(MilpSolveError) as raised:
                model.solve(time_limit_seconds=60.)
        self.assertEqual(raised.exception.solver_metrics["variable_count"], 1)
        self.assertEqual(raised.exception.solver_metrics["binary_count"], 1)
        self.assertEqual(raised.exception.solver_metrics["constraint_count"], 1)
        self.assertIsNone(raised.exception.solver_metrics["mip_gap"])


class SingleRequestRunTests(unittest.TestCase):
    def _request(self, folder, customer_count=10, scenario_count=5):
        request = {
            "node": "ANNUAL",
            "inputs": {"customers": [{} for _ in range(customer_count)], "scenario_count": scenario_count},
            "benchmark": {"customer_count": customer_count, "scenario_count": scenario_count},
        }
        path = Path(folder) / "request_01_sample_annual.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_only_one_node_is_solved_and_table_is_printed(self):
        solver = dict(
            milp_executed=True, status="OPTIMAL", variable_count=100,
            binary_count=10, constraint_count=80, optimization_scenario_count=5,
            time_count=48, solve_seconds=.2, solver_seconds=.2,
            time_limit_seconds=60., mip_gap=.01,
        )
        result = {"node": "ANNUAL", "status": "OPTIMAL", "solver": solver,
                  "inputs": {}, "raw_solution": {}}
        with tempfile.TemporaryDirectory() as tmp:
            request_path = self._request(tmp)
            output = StringIO()
            with patch.object(runner, "RESOURCE", Path(tmp)), \
                    patch.object(runner, "solve_request", return_value=result) as solve, \
                    redirect_stdout(output):
                response, response_path = runner.run_request(request_path)
            solve.assert_called_once()
            self.assertEqual(response["solver_test"]["time_limit_seconds"], 60.)
            self.assertEqual(response["solver_test"]["solved_scenario_count"], 5)
            self.assertEqual(response["solver_test"]["variable_count"], 100)
            self.assertIn("| 阶段 | 客户数 | 时间数量 | 场景数 | 变量数 | 约束数 |", output.getvalue())
            self.assertIn("| ANNUAL | 10 | 48 | 5 | 100 | 80 |", output.getvalue())
            saved = json.loads(Path(response_path).read_text(encoding="utf-8"))
            self.assertEqual(saved["solver_test"]["time_limit_seconds"], 60.)

    def test_solver_timeout_still_prints_truthful_table(self):
        metrics = dict(variable_count=100, binary_count=10, constraint_count=80,
                       solve_seconds=60., time_limit_seconds=60., mip_gap=None,
                       gap_status="INFINITE_NO_INCUMBENT")
        with tempfile.TemporaryDirectory() as tmp:
            request_path = self._request(tmp)
            output = StringIO()
            with patch.object(runner, "RESOURCE", Path(tmp)), \
                    patch.object(runner, "solve_request",
                                 side_effect=MilpSolveError("LIMIT_REACHED: Time limit reached", metrics)), \
                    redirect_stdout(output):
                response, _ = runner.run_request(request_path)
            self.assertEqual(response["status"], "TIME_LIMIT")
            self.assertEqual(response["solver_test"]["variable_count"], 100)
            self.assertIsNone(response["solver_test"]["mip_gap"])
            self.assertEqual(response["solver_test"]["time_limit_seconds"], 60.)
            self.assertIn("N/A", output.getvalue())
            self.assertIn("| ANNUAL | 10 | 48 | 5 | 100 | 80 |", output.getvalue())

    def test_run_uses_request_sizes_instead_of_generator_defaults(self):
        for customers, scenarios in ((1, 7), (2, 64), (11, 8), (20, 3)):
            with self.subTest(customers=customers, scenarios=scenarios), tempfile.TemporaryDirectory() as tmp:
                path = self._request(tmp, customers, scenarios)
                result = dict(node='ANNUAL', status='OPTIMAL', inputs={}, raw_solution={},
                    solver=dict(milp_executed=True, variable_count=100, constraint_count=80,
                        scenario_count=scenarios, time_count=48, solve_seconds=.001,
                        time_limit_seconds=60., mip_gap=0.))
                with patch.object(runner, 'RESOURCE', Path(tmp)), \
                        patch.object(runner, 'solve_request', return_value=result) as solve, \
                        redirect_stdout(StringIO()) as output:
                    response, saved_path = runner.run_request(path)
                solve.assert_called_once()
                actual = solve.call_args.args[0]['inputs']
                self.assertEqual(len(actual['customers']), customers)
                self.assertEqual(actual['scenario_count'], scenarios)
                self.assertEqual(actual['storage']['optimization_scenario_count'], scenarios)
                self.assertEqual(response['status'], 'OPTIMAL')
                self.assertEqual(response['solver_test']['customer_count'], customers)
                self.assertEqual(response['solver_test']['solved_scenario_count'], scenarios)
                self.assertEqual(json.loads(saved_path.read_text())['solver_test'], response['solver_test'])
                self.assertIn(f'| ANNUAL | {customers} | 48 | {scenarios} |', output.getvalue())

    def test_real_annual_solves_non_default_customer_and_scenario_counts(self):
        for customers, scenarios in ((2, 64), (11, 7)):
            with self.subTest(customers=customers, scenarios=scenarios), tempfile.TemporaryDirectory() as tmp:
                request = build_request('ANNUAL', customer_count=customers, scenario_count=scenarios)
                path = Path(tmp) / 'request_annual.json'
                path.write_text(json.dumps(request), encoding='utf-8')
                with patch.object(runner, 'RESOURCE', Path(tmp)), redirect_stdout(StringIO()) as output:
                    response, saved_path = runner.run_request(path)
                self.assertEqual(response['status'], 'OPTIMAL', response.get('error'))
                self.assertEqual(response['solver_test']['customer_count'], customers)
                self.assertEqual(response['solver_test']['solved_scenario_count'], scenarios)
                self.assertEqual(response['solver_test']['time_limit_seconds'], 60.)
                self.assertEqual(len(response['inputs']['customer_storage']), customers)
                self.assertEqual(json.loads(saved_path.read_text())['solver_test'], response['solver_test'])
                self.assertIn('HiGHS |', output.getvalue())
                self.assertIn(f'| ANNUAL | {customers} | 48 | {scenarios} |', output.getvalue())


class UnifiedEntryTests(unittest.TestCase):
    def test_single_request_cli_never_starts_matrix(self):
        with patch.object(sys, 'argv', ['run.py', 'selected_request.json']), \
                patch.object(runner, 'run_request', return_value=({'status': 'OPTIMAL'}, None)) as single, \
                patch.object(runner, 'run_matrix') as matrix:
            runner.main()
        single.assert_called_once_with('selected_request.json', None)
        matrix.assert_not_called()

    def test_matrix_cli_requires_explicit_flag(self):
        with patch.object(sys, 'argv', ['run.py', '--matrix', '--scenarios', '5', '20']), \
                patch.object(runner, 'REQUEST_FILE', ''), \
                patch.object(runner, 'UPSTREAM_RESPONSE_FILE', ''), \
                patch.object(runner, 'run_request') as single, \
                patch.object(runner, 'run_matrix', return_value=({'runs': [{'verified': True}]}, None)) as matrix:
            runner.main()
        matrix.assert_called_once_with([5, 20], None, False)
        single.assert_not_called()

    def test_merged_matrix_keeps_all_49_requests_and_metrics(self):
        self._check_matrix(10, SCENARIO_COUNTS)

    def test_matrix_uses_generator_customer_configuration(self):
        self._check_matrix(3, (7, 64))

    def _check_matrix(self, customer_count, scenario_counts):
        from pifa_lingshou.solver_test import request_generation as generation

        def solve(path):
            count, node = path
            metrics = dict(customer_count=customer_count, time_count=48 if node == 'ANNUAL' else 96,
                solved_scenario_count=count, variable_count=100, constraint_count=80,
                solver_seconds=.2, other_processing_seconds=.1, elapsed_seconds=.3,
                mip_gap=.01, time_limit_seconds=60.)
            return dict(status='OPTIMAL', solver_test=metrics), Path(f'{count}_{node}_response.json')

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()) as output, \
                patch.object(generation, 'CUSTOMER_COUNT', customer_count), \
                patch.object(generation, 'build_request', side_effect=lambda **kw: kw) as build, \
                patch.object(generation, 'allocate_run_number', side_effect=range(8, 8 + len(scenario_counts))) as allocate, \
                patch.object(generation, 'save_request', side_effect=lambda r, run_number: (r['scenario_count'], r['node'])) as save, \
                patch.object(runner, 'run_request', side_effect=solve) as run:
            manifest, path = runner.run_matrix(scenario_counts, output_dir=tmp)
            self.assertEqual(run.call_count, 7 * len(scenario_counts))
            self.assertEqual(allocate.call_count, len(scenario_counts))
            self.assertEqual([c.kwargs['run_number'] for c in save.call_args_list],
                             [number for number in range(8, 8 + len(scenario_counts)) for _ in range(7)])
            self.assertEqual(len(manifest['runs']), 7 * len(scenario_counts))
            self.assertTrue(all(r['verified'] for r in manifest['runs']))
            self.assertTrue(all(c.kwargs['customer_count'] == customer_count for c in build.call_args_list))
            self.assertEqual({r['scenario_count'] for r in manifest['runs']}, set(scenario_counts))
            self.assertEqual(json.loads(path.read_text()), manifest)
            self.assertIn('| 阶段 | 客户数 | 时间数量 | 场景数 |', output.getvalue())
            self.assertEqual((Path(tmp) / 'terminal.log').read_text(), output.getvalue())


if __name__ == "__main__":
    unittest.main()
