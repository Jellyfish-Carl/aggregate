import unittest

from inputs.load_forecast import build_forecast_snapshots
from inputs.mockdata import build_price_forecasts, build_rolling_order_book, price_curves
from data_objects.scenario import (
    candidate_joint_trajectories,
    cvar,
    discrete_load_scenarios,
    discrete_load_value,
    joint_scenario_diagnostics,
    reduced_joint_trajectories,
)


class ScenarioTests(unittest.TestCase):
    def test_m02_cvar_equals_deterministic_cost(self):
        self.assertAlmostEqual(cvar([123.45] * 16, [1 / 16] * 16), 123.45)

    def test_cvar95_uses_only_the_worst_five_percent_probability_mass(self):
        self.assertAlmostEqual(cvar([10.0, 20.0, 100.0], [0.2, 0.5, 0.3]), 100.0)

    def test_pre_spot_load_scenario_catalog_is_normalized(self):
        scenarios = discrete_load_scenarios()
        self.assertEqual(
            [item.scenario_id for item in scenarios],
            ["P05", "P25", "P50", "P75", "P95"],
        )
        self.assertAlmostEqual(sum(item.probability for item in scenarios), 1.0)
        row = {"p10_mwh": 8.0, "p50_mwh": 10.0, "p90_mwh": 14.0}
        values = [discrete_load_value(row, item) for item in scenarios]
        self.assertTrue(all(left < right for left, right in zip(values, values[1:])))

    def test_joint_scenario_reduction_retains_tail_states(self):
        load = build_forecast_snapshots()[3]
        price = build_price_forecasts()["D-3"]
        candidates = candidate_joint_trajectories(load, price)
        scenarios = reduced_joint_trajectories(load, price, 12)
        self.assertEqual(len(scenarios), 12)
        self.assertAlmostEqual(sum(item.probability for item in scenarios), 1.0)
        ids = {item.scenario_id for item in scenarios}
        self.assertIn("L50_DA50_SP50", ids)
        metrics = (
            lambda item: sum(item.load_mwh),
            lambda item: sum(item.day_ahead_price),
            lambda item: sum(
                rt - da
                for rt, da in zip(item.real_time_price, item.day_ahead_price)
            ),
        )
        for metric in metrics:
            self.assertIn(min(candidates, key=metric).scenario_id, ids)
            self.assertIn(max(candidates, key=metric).scenario_id, ids)
        varying = scenarios[-1]
        ratios = [
            load_value / max(row["p50_mwh"], 1e-9)
            for load_value, row in zip(varying.load_mwh, load["rows"])
        ]
        self.assertGreater(max(ratios) - min(ratios), 0.01)

    def test_joint_catalog_has_100_reproducible_correlated_time_paths(self):
        load = build_forecast_snapshots()[3]
        price = build_price_forecasts()["D-3"]
        first = candidate_joint_trajectories(load, price)
        second = candidate_joint_trajectories(load, price)
        self.assertEqual(len(first), 100)
        self.assertEqual(first, second)
        self.assertTrue(all(len(item.load_mwh) == 96 for item in first))
        diagnostics = joint_scenario_diagnostics(load, price, first)
        self.assertGreater(diagnostics["load_day_ahead_error_correlation"], 0.2)
        self.assertLess(diagnostics["load_day_ahead_error_correlation"], 0.9)
        self.assertGreater(diagnostics["load_real_time_error_correlation"], 0.2)
        self.assertLess(diagnostics["load_real_time_error_correlation"], 0.9)
        self.assertGreater(diagnostics["day_ahead_real_time_error_correlation"], 0.6)
        self.assertLess(diagnostics["day_ahead_real_time_error_correlation"], 0.99)
        self.assertTrue(
            any(
                value < float(row["p10_mwh"])
                for item in first
                for value, row in zip(item.load_mwh, load["rows"])
            )
        )
        self.assertTrue(
            any(
                value > float(row["p90_mwh"])
                for item in first
                for value, row in zip(item.load_mwh, load["rows"])
            )
        )

    def test_official_day_ahead_price_collapses_da_dimension(self):
        load = build_forecast_snapshots()[5]
        price = dict(build_price_forecasts()["D-1"])
        price["information_state"] = "OFFICIAL_DAY_AHEAD_REVEALED"
        candidates = candidate_joint_trajectories(load, price)
        self.assertEqual(len(candidates), 100)
        self.assertEqual({item.day_ahead_quantile for item in candidates}, {"P50"})
        for item in candidates:
            self.assertEqual(
                list(item.day_ahead_price),
                [float(row["day_ahead_p50"]) for row in price["rows"]],
            )

    def test_rolling_mock_orders_cover_midday_buy_and_peak_sell_windows(self):
        forecasts = build_price_forecasts()
        book = build_rolling_order_book("D-3", forecasts["D-3"])
        self.assertEqual(book["order_count"], 48)
        self.assertEqual(
            [item["arrival_sequence"] for item in book["orders"]],
            list(range(1, 49)),
        )
        midday = [item for item in book["orders"] if item["our_side"] == "BUY"]
        peak = [item for item in book["orders"] if item["our_side"] == "SELL"]
        self.assertEqual(
            sorted({item["time"] for item in midday}),
            ["11:00", "11:30", "12:00", "12:30"],
        )
        self.assertEqual(
            sorted({item["time"] for item in peak}),
            [
                "09:00", "09:30", "10:00", "10:30",
                "13:00", "13:30", "14:00", "14:30",
                "15:00", "15:30", "16:00", "16:30",
            ],
        )
        self.assertEqual({item["price_tier"] for item in midday}, {"FAVORABLE", "MARGINAL", "UNFAVORABLE"})
        self.assertEqual({item["price_tier"] for item in peak}, {"FAVORABLE", "MARGINAL", "UNFAVORABLE"})
        self.assertEqual(
            sum(
                item["spot_price_edge_yuan_per_mwh"] >= 100.0
                for item in book["orders"]
            ),
            4,
        )

    def test_price_forecast_has_duck_curve_high_low_high_shape(self):
        rows = build_price_forecasts()["D-2"]["rows"]

        def average(start: int, end: int) -> float:
            return sum(float(row["real_time_p50"]) for row in rows[start * 4:end * 4]) / ((end - start) * 4)

        midday = average(11, 13)
        self.assertGreater(average(9, 11), midday)
        self.assertGreater(average(13, 17), midday)

    def test_price_forecast_revisions_converge_and_intervals_narrow(self):
        forecasts = build_price_forecasts()

        def average_width(event: str, prefix: str) -> float:
            rows = forecasts[event]["rows"]
            return sum(
                float(row[prefix + "_p90"]) - float(row[prefix + "_p10"])
                for row in rows
            ) / len(rows)

        for prefix in ("day_ahead", "real_time"):
            self.assertGreater(average_width("D-3", prefix), average_width("D-2", prefix))
            self.assertGreater(average_width("D-2", prefix), average_width("D-1", prefix))

        def revision(left: str, right: str, key: str) -> float:
            return sum(
                abs(float(a[key]) - float(b[key]))
                for a, b in zip(forecasts[left]["rows"], forecasts[right]["rows"])
            ) / 96.0

        for key in ("day_ahead_p50", "real_time_p50"):
            self.assertGreater(revision("D-3", "D-2", key), revision("D-2", "D-1", key))

    def test_hidden_price_truth_is_independent_and_has_natural_tail_points(self):
        forecasts = build_price_forecasts()
        d1 = forecasts["D-1"]["rows"]
        default_truth = price_curves()["real_time"]
        alternate_truth = price_curves(2026091599)["real_time"]
        self.assertNotEqual(default_truth, alternate_truth)
        self.assertNotIn("official_real_time", d1[0])
        covered = sum(
            float(row["real_time_p10"]) <= actual <= float(row["real_time_p90"])
            for row, actual in zip(d1, default_truth)
        ) / 96.0
        self.assertGreater(covered, 0.65)
        self.assertLess(covered, 0.90)

    def test_real_time_quantiles_use_joint_variance_and_price_steps_are_smoothed(self):
        rows = build_price_forecasts()["D-1"]["rows"]
        for row in rows:
            da_half = float(row["day_ahead_p90"]) - float(row["day_ahead_p50"])
            spread_half = float(row["spread_p90"]) - float(row["spread_p50"])
            rt_half = float(row["real_time_p90"]) - float(row["real_time_p50"])
            self.assertGreater(rt_half, max(da_half, spread_half))
            self.assertLess(rt_half, da_half + spread_half)
        real_time = [float(row["real_time_p50"]) for row in rows]
        self.assertLess(max(abs(right - left) for left, right in zip(real_time, real_time[1:])), 200.0)


if __name__ == "__main__":
    unittest.main()
