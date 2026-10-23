import time
import unittest
from unittest.mock import patch

from pifa_lingshou.problem_solver.milp import MilpSolveError, _run_isolated


def _sleeping_solver(connection, limit):
    time.sleep(10)
    connection.send((True, {}))
    connection.close()


def _return_limit_incumbent(connection, limit):
    connection.send((True, {
        "status": 1,
        "values": [1.0],
        "mip_gap": 0.3275,
        "worker_limit": limit,
    }))
    connection.close()


class SolverWatchdogTests(unittest.TestCase):
    def test_solver_limit_result_preserves_incumbent_and_gap(self):
        result, elapsed, effective_limit = _run_isolated(_return_limit_incumbent, (), 1.5)
        self.assertEqual(result["status"], 1)
        self.assertEqual(result["values"], [1.0])
        self.assertEqual(result["mip_gap"], 0.3275)
        self.assertEqual(result["worker_limit"], effective_limit)
        self.assertEqual(effective_limit, 1.5)
        self.assertLess(elapsed, 1.5)

    def test_external_watchdog_terminates_overdue_solver_process(self):
        started = time.perf_counter()
        with patch("pifa_lingshou.problem_solver.milp.WATCHDOG_TERMINATION_MARGIN_SECONDS", .2):
            with self.assertRaisesRegex(MilpSolveError, "TIME_LIMIT_REACHED"):
                _run_isolated(_sleeping_solver, (), 1.5)
        self.assertLess(time.perf_counter() - started, 4.0)


if __name__ == "__main__":
    unittest.main()
