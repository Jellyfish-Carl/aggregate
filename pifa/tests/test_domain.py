import unittest

from data_objects.domain import ContractFill, DataStatus, MpcState


class StateTests(unittest.TestCase):
    def test_s01_fill_is_idempotent(self):
        state = MpcState(state_version=1, event_id="E1", current_interval=None)
        fill = ContractFill("F1", "ANNUAL", "E1", "BUY", 10.0, 400.0, 0.0, {"T1": 10.0}, True)
        self.assertTrue(state.apply_fill(fill, before_version=1))
        self.assertFalse(state.apply_fill(fill, before_version=2))
        self.assertEqual(len(state.posted_contracts), 1)
        self.assertEqual(state.state_version, 2)

    def test_s07_lower_quality_measurement_does_not_overwrite_official(self):
        state = MpcState(state_version=3, event_id="E1", current_interval="T1", soc_mwh=9.0)
        applied = state.apply_measurement(
            "M1",
            2.0,
            DataStatus.OPERATIONAL_ESTIMATE,
            DataStatus.MONTHLY_OFFICIAL,
            before_version=3,
        )
        self.assertFalse(applied)
        self.assertEqual(state.soc_mwh, 9.0)
        self.assertEqual(state.state_version, 3)

    def test_stale_state_version_is_rejected(self):
        state = MpcState(state_version=4, event_id="E1", current_interval=None)
        fill = ContractFill("F1", "ANNUAL", "E1", "BUY", 1.0, 400.0, 0.0, {"T1": 1.0}, True)
        with self.assertRaisesRegex(ValueError, "STALE_STATE_VERSION"):
            state.apply_fill(fill, before_version=3)


if __name__ == "__main__":
    unittest.main()
