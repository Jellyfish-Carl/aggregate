import unittest
from unittest.mock import patch

from pifa_lingshou.service.request_runner import solve_request
from pifa_lingshou.service.layer_runner import run_layer


class RequestRunnerTests(unittest.TestCase):
    def test_package_stage_rejects_procurement_inputs_and_upstream(self):
        with self.assertRaisesRegex(ValueError, '完整案例'):
            solve_request({'node': 'PACKAGE_RECOMMEND', 'inputs': {'risk_lambda': 0.25}})
        with self.assertRaisesRegex(ValueError, '签约前'):
            solve_request({'node': 'PACKAGE_RECOMMEND', 'inputs': {}}, {'node': 'ANNUAL'})

    def test_history_envelope_passes_exact_inputs_and_upstream(self):
        inputs = {'annual_price': 123.0}
        upstream = {'node': 'ANNUAL', 'fills': [{'fill_id': 'previous'}]}
        request = {'node': 'MONTHLY', 'inputs': inputs, 'input_revision': 7}
        with patch('pifa_lingshou.service.request_runner.run_layer', return_value={}) as run:
            result = solve_request(request, upstream)
        run.assert_called_once_with('MONTHLY', inputs, upstream, None)
        self.assertEqual(result['input_revision'], 7)

    def test_historical_replay_cannot_silently_use_mock_positions(self):
        with self.assertRaisesRegex(ValueError, 'upstream'):
            solve_request({'node': 'MONTHLY', 'inputs': {}, 'input_revision': 1})

    def test_plain_inputs_are_not_misread_as_full_request(self):
        for request in ({'risk_lambda': 0.5}, {'node': 'ANNUAL', 'inputs': []}, []):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, 'request'):
                solve_request(request)

    def test_full_request_cannot_silently_fall_back_to_default_inputs(self):
        with self.assertRaisesRegex(ValueError, 'solve_request'):
            run_layer('ANNUAL', {'node': 'ANNUAL', 'inputs': {'annual_price': 123.0}})


if __name__ == '__main__':
    unittest.main()
