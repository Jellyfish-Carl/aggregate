import unittest

from inputs.load_forecast import (
    IndustrialParkConfig,
    build_forecast_snapshots,
    industrial_park_baseline_mw,
    month_equivalent_days,
    quantile_curve,
    ten_day_equivalent_days,
    _curve_rows,
)
from data_objects.domain import ContractFill
from service.simulator import simulate as strict_simulate
from service.trading import (
    TraderConfig,
    apply_l2_rolling_threshold,
    rolling_partial_fill_ratio,
    _rolling_sell_cap,
    default_quotes,
)


def simulate(**kwargs):
    """Business-chain tests explicitly exercise the non-optimizing diagnostic path."""

    return strict_simulate(require_milp=False, diagnostic_mode=True, **kwargs)


class ForecastTests(unittest.TestCase):
    def test_industrial_park_baseline_has_48_points_and_260_mwh(self):
        curve = industrial_park_baseline_mw(IndustrialParkConfig())
        self.assertEqual(len(curve), 48)
        self.assertAlmostEqual(sum(curve) * 0.5, 260.0)
        self.assertEqual(max(curve), 16.0)

    def test_forecast_snapshots_narrow_toward_delivery(self):
        snapshots = build_forecast_snapshots()
        self.assertEqual(len(snapshots), 102)
        widths = {
            snapshot["label"]: snapshot["quality"]["interval_width_mwh"]
            for snapshot in snapshots
        }
        order = ["年度", "月度", "旬内", "D-3 滚撮", "D-2 滚撮", "D-1", "RT-48"]
        self.assertTrue(all(widths[left] > widths[right] for left, right in zip(order, order[1:])))

    def test_p50_revisions_change_direction_and_accuracy_is_measured(self):
        snapshots = build_forecast_snapshots()[:6]
        totals = [snapshot["quality"]["p50_total_mwh"] for snapshot in snapshots]
        changes = [right - left for left, right in zip(totals, totals[1:])]
        directions = [change > 0 for change in changes]
        self.assertTrue(directions[1], "旬内预测应体现相对月度的生产计划上调")
        self.assertIn(True, directions)
        self.assertIn(False, directions)
        self.assertGreaterEqual(
            sum(left != right for left, right in zip(directions, directions[1:])),
            2,
        )
        mapes = [snapshot["quality"]["mape"] for snapshot in snapshots]
        self.assertTrue(all(value >= 0 for value in mapes))
        self.assertEqual(snapshots[-1]["quality"]["percentage_error_metric"], "WAPE")

    def test_product_delivery_scopes_use_explicit_equivalent_days(self):
        self.assertAlmostEqual(month_equivalent_days(), 27.76)
        self.assertAlmostEqual(ten_day_equivalent_days(), 8.88)

    def test_p75_legacy_rows_fall_back_to_interpolation(self):
        snapshot = {
            "rows": [{"p50_mwh": 10.0, "p90_mwh": 18.0}]
        }
        self.assertEqual(quantile_curve(snapshot, "P75"), [15.0])

    def test_forecast_rows_expose_direct_p75_prediction(self):
        snapshot = build_forecast_snapshots()[5]
        row = snapshot["rows"][0]
        self.assertIn("p75_mwh", row)
        self.assertEqual(quantile_curve(snapshot, "P75")[0], row["p75_mwh"])

    def test_future_actual_does_not_change_unrealized_forecast_curve(self):
        anchor = [10.0, 11.0]
        first = _curve_rows("D-1", "D-1", [8.0, 9.0], anchor, 0.02, 0.03)
        second = _curve_rows("D-1", "D-1", [18.0, 19.0], anchor, 0.02, 0.03)
        self.assertEqual(
            [row["p50_mwh"] for row in first],
            [row["p50_mwh"] for row in second],
        )


class RollingOrderRuleTests(unittest.TestCase):
    def test_fill_ratio_scales_from_ten_to_twenty_percent(self):
        config = TraderConfig()
        self.assertAlmostEqual(rolling_partial_fill_ratio(100.0, config), 0.10)
        self.assertAlmostEqual(rolling_partial_fill_ratio(112.5, config), 0.15)
        self.assertAlmostEqual(rolling_partial_fill_ratio(125.0, config), 0.20)
        self.assertAlmostEqual(rolling_partial_fill_ratio(180.0, config), 0.20)

    def test_spot_edge_rule_partially_fills_captured_orders(self):
        park = IndustrialParkConfig()
        config = TraderConfig()
        monthly_point_load = 10.0 * month_equivalent_days(park)
        delivery_curve = {
            "P%02d" % (index + 1): monthly_point_load for index in range(48)
        }
        locked = ContractFill(
            fill_id="LOCKED",
            product_class="MONTHLY",
            event_id="DEMO-MONTHLY",
            side="BUY",
            signed_quantity=sum(delivery_curve.values()),
            price=400.0,
            fee=0.0,
            delivery_curve=delivery_curve,
            assessment_base_eligible=True,
            equivalent_delivery_days=month_equivalent_days(park),
        )
        orders = [
            {
                "order_id": "BUY-EDGE-100",
                "arrival_sequence": 1,
                "period": 1,
                "our_side": "BUY",
                "quantity_mwh": 10.0,
                "price_yuan_per_mwh": 400.0,
                "real_time_reference_yuan_per_mwh": 500.0,
            },
            {
                "order_id": "SELL-EDGE-125",
                "arrival_sequence": 2,
                "period": 2,
                "our_side": "SELL",
                "quantity_mwh": 10.0,
                "price_yuan_per_mwh": 625.0,
                "real_time_reference_yuan_per_mwh": 500.0,
            },
            {
                "order_id": "BUY-EDGE-99",
                "arrival_sequence": 3,
                "period": 3,
                "our_side": "BUY",
                "quantity_mwh": 10.0,
                "price_yuan_per_mwh": 401.0,
                "real_time_reference_yuan_per_mwh": 500.0,
            },
        ]
        action = apply_l2_rolling_threshold(
            event="D-3",
            snapshot={"rows": [{"p50_mwh": 10.0} for _ in range(48)]},
            previous_position=sum(delivery_curve.values()),
            previous_notional=0.0,
            annual_position=0.0,
            overall_position=sum(delivery_curve.values()),
            config=config,
            park_config=park,
            quote=default_quotes()["D-3"],
            expected_spot_price=500.0,
            risk_lambda=0.25,
            rolling_sell_used=0.0,
            rolling_sell_curve=[0.0] * 48,
            joint_scenarios=(),
            prior_fills=[locked],
            order_book={"orders": orders},
            valuation={
                "rolling_half_hour_marginal_value_yuan_per_mwh": [500.0] * 48,
            },
        )
        decisions = {item["order_id"]: item for item in action.order_decisions}
        self.assertAlmostEqual(decisions["BUY-EDGE-100"]["accepted_quantity_mwh"], 1.0)
        self.assertAlmostEqual(decisions["BUY-EDGE-100"]["accepted_fill_ratio"], 0.10)
        self.assertAlmostEqual(decisions["SELL-EDGE-125"]["accepted_quantity_mwh"], 2.0)
        self.assertAlmostEqual(decisions["SELL-EDGE-125"]["accepted_fill_ratio"], 0.20)
        self.assertFalse(decisions["BUY-EDGE-99"]["price_triggered"])
        self.assertEqual(decisions["BUY-EDGE-99"]["accepted_quantity_mwh"], 0.0)

    def test_l3_marginal_value_gate_filters_threshold_qualified_order(self):
        config = TraderConfig()
        park = IndustrialParkConfig()
        locked = ContractFill(
            fill_id="locked",
            product_class="ANNUAL",
            event_id="DEMO-ANNUAL",
            side="BUY",
            signed_quantity=500.0,
            price=400.0,
            fee=0.0,
            delivery_curve={"P%02d" % (i + 1): 500.0 / 48 for i in range(48)},
            assessment_base_eligible=True,
            equivalent_delivery_days=1,
        )
        order = {
            "order_id": "BUY-L3-BLOCK",
            "arrival_sequence": 1,
            "period": 1,
            "our_side": "BUY",
            "quantity_mwh": 10.0,
            "price_yuan_per_mwh": 400.0,
            "real_time_reference_yuan_per_mwh": 500.0,
        }
        action = apply_l2_rolling_threshold(
            event="D-3",
            snapshot={"rows": [{"p50_mwh": 10.0} for _ in range(48)]},
            previous_position=500.0,
            previous_notional=0.0,
            annual_position=500.0,
            overall_position=500.0,
            config=config,
            park_config=park,
            quote=default_quotes()["D-3"],
            expected_spot_price=500.0,
            risk_lambda=0.25,
            rolling_sell_used=0.0,
            rolling_sell_curve=[0.0] * 48,
            joint_scenarios=(),
            prior_fills=[locked],
            order_book={"orders": [order]},
            valuation={
                "rolling_half_hour_marginal_value_yuan_per_mwh": [350.0] * 48,
            },
        )
        decision = action.order_decisions[0]
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["rejection_code"], "L3_MARGINAL_VALUE_INSUFFICIENT")
        self.assertTrue(decision["l3_gate_available"])


class DemoSimulatorTests(unittest.TestCase):
    def test_baseline_configuration_changes_contract_decision(self):
        low = simulate(event="ANNUAL", annual_coverage=0.70)
        high = simulate(event="ANNUAL", annual_coverage=0.90)
        low_action = low["portfolio"]["baseline"]["actions"][0]
        high_action = high["portfolio"]["baseline"]["actions"][0]
        self.assertGreater(high_action["quantity_mwh"], low_action["quantity_mwh"])

    def test_price_gate_can_stop_contract_purchase(self):
        blocked = simulate(event="ANNUAL", minimum_edge=1000.0)
        action = blocked["portfolio"]["baseline"]["actions"][0]
        self.assertEqual(action["side"], "HOLD")
        self.assertEqual(blocked["portfolio"]["baseline"]["position_mwh"], 0.0)

    def test_risk_changes_optimized_contract_and_declaration(self):
        neutral = simulate(risk=0.0)
        averse = simulate(risk=1.0)
        neutral_position = neutral["portfolio"]["optimized"]["position_mwh"]
        averse_position = averse["portfolio"]["optimized"]["position_mwh"]
        self.assertGreater(averse_position, neutral_position)
        self.assertGreater(averse["declaration"]["total_mwh"], neutral["declaration"]["total_mwh"])

    def test_coverage_target_changes_optimized_contract_quantity(self):
        low = simulate(event="ANNUAL", annual_coverage=0.20)
        high = simulate(event="ANNUAL", annual_coverage=0.80)
        self.assertLess(
            low["portfolio"]["optimized"]["actions"][0]["quantity_mwh"],
            high["portfolio"]["optimized"]["actions"][0]["quantity_mwh"],
        )

    def test_cvar_is_identical_in_objective_and_headline(self):
        payload = simulate(event="D-1")
        self.assertEqual(
            payload["headline"]["cvar95_cost_yuan"],
            payload["l3_objective"]["cvar95_yuan"],
        )

    def test_declaration_band_reports_slack_without_hiding_fallback(self):
        annual = simulate(event="ANNUAL")
        d1 = simulate(event="D-1")
        self.assertGreaterEqual(annual["declaration"]["total_band_slack_mwh"], 0.0)
        self.assertEqual(d1["declaration"]["total_band_slack_mwh"], 0.0)

    def test_sell_permissions_assessments_and_rolling_rule(self):
        payload = simulate(event="D-1")
        actions = payload["portfolio"]["optimized"]["actions"]
        self.assertNotIn("SELL", [action["side"] for action in actions[:3]])
        self.assertEqual(actions[2]["side"], "BUY")
        self.assertTrue(any(action["side"] == "SELL" for action in actions[3:]))
        rule = payload["portfolio"]["rolling_rule"]
        self.assertEqual(rule["price_reference"], "REAL_TIME_P50_FORECAST")
        self.assertEqual(rule["price_edge_yuan_per_mwh"], 100.0)
        self.assertEqual(rule["min_fill_ratio"], 0.10)
        self.assertEqual(rule["max_fill_ratio"], 0.20)
        self.assertEqual(rule["position_lower_ratio"], 0.90)
        self.assertEqual(rule["position_upper_ratio"], 1.10)
        assessment = payload["portfolio"]["assessment"]
        self.assertIsNone(assessment["annual"]["upper_limit"])
        self.assertEqual(assessment["granularity"], "48_HALF_HOUR_PERIODS")
        self.assertEqual(len(assessment["periods"]), 48)
        self.assertEqual(assessment["annual"]["compliant_periods"], 48)
        self.assertEqual(assessment["overall"]["compliant_periods"], 48)
        self.assertEqual(assessment["annual"]["status"], "PASS")
        self.assertEqual(assessment["overall"]["status"], "PASS")

    def test_l1_contract_curve_and_assessment_are_both_48_point(self):
        payload = simulate(event="D-1")
        optimized = payload["portfolio"]["optimized"]
        l1_fills = [
            fill for fill in optimized["fills"]
            if fill["product_class"] in {"ANNUAL", "MONTHLY", "TEN_DAY"}
        ]
        self.assertTrue(l1_fills)
        for fill in l1_fills:
            self.assertEqual(len(fill["delivery_curve"]), 48)
            self.assertAlmostEqual(
                sum(fill["delivery_curve"].values()),
                fill["signed_quantity"],
                places=6,
            )
        annual_fill = next(
            fill for fill in l1_fills if fill["product_class"] == "ANNUAL"
        )
        self.assertTrue(
            all(value > 1e-8 for value in annual_fill["delivery_curve"].values())
        )
        assessment = payload["portfolio"]["assessment"]
        self.assertTrue(
            all(row["overall_contract_mwh"] > 1e-8 for row in assessment["periods"])
        )
        optimization = optimized["optimization"]
        self.assertEqual(
            optimization["l1_assessment_granularity"],
            "48_HALF_HOUR_PERIODS",
        )
        self.assertEqual(
            optimization["overall_assessment_products"],
            ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
        )
        self.assertEqual(
            optimization["l1_period_curve_role"],
            "HEDGE_DELIVERY_RECOURSE_VALUATION_AND_PERIOD_ASSESSMENT",
        )
        self.assertEqual(
            optimization["spot_assessment_granularity"],
            "96_QUARTER_HOUR_PERIODS",
        )

    def test_ten_day_mock_update_triggers_a_visible_trade(self):
        payload = simulate(event="TEN_DAY")
        action = payload["portfolio"]["optimized"]["actions"][2]
        self.assertEqual(action["event"], "TEN_DAY")
        self.assertEqual(action["side"], "BUY")
        self.assertGreater(action["quantity_mwh"], 0.0)

    def test_pre_spot_exposure_is_forecast_minus_locked_long_term(self):
        payload = simulate(event="TEN_DAY")
        breakdown = payload["declaration_breakdown"]
        self.assertEqual(
            breakdown["planning_spot_exposure_definition"],
            "P50预测负荷 - 已锁定中长期净合约量",
        )
        for row in breakdown["rows"]:
            self.assertAlmostEqual(
                row["spot_exposure_mwh"],
                row["forecast_load_mwh"] - row["long_term_mwh"],
                places=3,
            )
            self.assertAlmostEqual(
                row["spot_exposure_mwh"],
                row["spot_exposure_buy_mwh"] - row["spot_exposure_sell_mwh"],
                places=6,
            )

    def test_assessment_guard_exposes_long_term_and_spot_granularity(self):
        payload = simulate(event="D-1")
        breakdown = payload["declaration_breakdown"]
        guard = breakdown["assessment_guard"]
        self.assertEqual(
            guard["long_term_assessment_granularity"],
            "48_HALF_HOUR_PERIODS",
        )
        self.assertEqual(
            guard["long_term_assessment_products"],
            ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"],
        )
        self.assertEqual(
            guard["long_term_delivery_allocation_grid"],
            "48_HALF_HOUR_PRODUCTS",
        )
        self.assertEqual(
            guard["spot_deviation_assessment_granularity"],
            "96_QUARTER_HOUR_PERIODS",
        )
        self.assertEqual(guard["assessment_period_count"], 48)
        periods = payload["portfolio"]["assessment"]["periods"]
        self.assertEqual(len(periods), 48)
        for index, period in enumerate(periods):
            for row in breakdown["rows"][index * 2 : index * 2 + 2]:
                self.assertEqual(
                    row["annual_assessment_ratio"], period["annual_ratio"]
                )
                self.assertEqual(
                    row["overall_assessment_ratio"], period["overall_ratio"]
                )

    def test_overall_assessment_includes_all_long_term_hedges(self):
        payload = simulate(
            event="D-1",
            annual_coverage=0.60,
            monthly_coverage=0.60,
            ten_day_coverage=1.00,
        )
        optimized = payload["portfolio"]["optimized"]
        fills = optimized["fills"]
        self.assertTrue(any(fill["product_class"] == "TEN_DAY" for fill in fills))
        assessed_curve = [
            sum(
                fill["delivery_curve"]["P%02d" % (index + 1)]
                for fill in fills
                if fill["assessment_base_eligible"]
            )
            for index in range(48)
        ]
        self.assertAlmostEqual(
            optimized["overall_position_mwh"], sum(assessed_curve), places=3
        )
        self.assertTrue(
            all(
                fill["assessment_base_eligible"]
                for fill in fills
                if fill["product_class"]
                in {"ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"}
            )
        )
        assessment = payload["portfolio"]["assessment"]
        self.assertEqual(len(assessment["periods"]), 48)
        for expected, period in zip(assessed_curve, assessment["periods"]):
            self.assertAlmostEqual(period["overall_contract_mwh"], expected, places=5)
            self.assertAlmostEqual(
                period["overall_ratio"],
                expected / period["assessment_demand_mwh"],
                places=5,
            )
        settlement = payload["settlement"]["optimized"]
        self.assertEqual(
            settlement["long_term_assessment_granularity"],
            "48_HALF_HOUR_PERIODS",
        )
        self.assertEqual(len(settlement["annual_point_ratios"]), 48)
        self.assertEqual(len(settlement["overall_point_ratios"]), 48)

    def test_annual_daily_forecast_is_converted_to_monthly_contract_basis(self):
        payload = simulate(event="ANNUAL")
        action = payload["portfolio"]["optimized"]["actions"][0]
        expected = (
            sum(
                row["p50_mwh"]
                for row in payload["load_forecast"]["phases"][0]["rows"]
            )
            * payload["load_forecast"]["month_equivalent_days"]
        )
        self.assertAlmostEqual(action["forecast_basis_mwh"], expected, places=2)
        self.assertLess(action["quantity_mwh"], 6600.0)
        self.assertIn("折算月度P50", action["reason"])

    def test_rolling_price_and_fill_parameters_are_reported(self):
        payload = simulate(
            event="D-1",
            rolling_price_edge=120.0,
            rolling_min_fill_ratio=0.12,
            rolling_max_fill_ratio=0.18,
        )
        rule = payload["portfolio"]["rolling_rule"]
        self.assertEqual(rule["price_edge_yuan_per_mwh"], 120.0)
        self.assertEqual(rule["min_fill_ratio"], 0.12)
        self.assertEqual(rule["max_fill_ratio"], 0.18)

    def test_rolling_capacity_accounts_for_prior_interval_curve(self):
        rows = [
            {"p10_mwh": 1.0, "p50_mwh": 1.0, "p90_mwh": 1.0}
            for _ in range(2)
        ]
        config = TraderConfig(
            rolling_interval_limit_ratio=0.20,
            rolling_daily_limit_ratio=1.0,
        )
        remaining = _rolling_sell_cap(
            {"rows": rows}, config, prior_sell_curve=[0.19, 0.0]
        )
        self.assertAlmostEqual(remaining, 0.02)

    def test_forecast_history_has_dates_deltas_and_non_anticipative_visibility(self):
        annual = simulate(event="ANNUAL")
        self.assertEqual(len(annual["load_forecast"]["history"]), 102)
        self.assertEqual(len(annual["load_forecast"]["phases"]), 102)
        self.assertEqual(len(annual["load_forecast"]["phases"][0]["rows"]), 96)
        self.assertEqual(sum(item["available"] for item in annual["load_forecast"]["history"]), 1)
        d1 = simulate(event="D-1")
        visible = [item for item in d1["load_forecast"]["history"] if item["available"]]
        self.assertEqual(len(visible), 6)
        self.assertEqual(visible[1]["published_at"], "2026-08-20T10:00:00+08:00")
        self.assertGreater(visible[1]["p50_change_mwh"], 0)

    def test_api_hides_future_actual_meter_values(self):
        d1 = simulate(event="D-1")
        self.assertTrue(
            all(row["actual_mwh"] is None for row in d1["load_forecast"]["rows"])
        )
        self.assertIsNone(d1["load_forecast"]["daily_actual_total_mwh"])
        self.assertIsNone(d1["load_forecast"]["quality"]["wape"])
        self.assertTrue(
            all(item["actual_total_mwh"] is None for item in d1["load_forecast"]["history"])
        )
        future_history = [
            item for item in d1["load_forecast"]["history"] if not item["available"]
        ]
        self.assertTrue(
            all(
                item["p50_total_mwh"] is None and item["decision"] is None
                for item in future_history
            )
        )
        self.assertTrue(
            all(
                not phase["rows"]
                for phase in d1["load_forecast"]["phases"]
                if not phase["available"]
            )
        )
        rt = simulate(event="REAL_TIME", rt_period=20)
        self.assertTrue(
            all(row["actual_mwh"] is not None for row in rt["load_forecast"]["rows"][:19])
        )
        self.assertTrue(
            all(row["actual_mwh"] is None for row in rt["load_forecast"]["rows"][19:])
        )
        self.assertEqual(rt["load_forecast"]["quality"]["realized_periods"], 19)
        month_end = simulate(event="MONTH_END")
        self.assertTrue(
            all(
                row["actual_mwh"] is not None
                for row in month_end["load_forecast"]["rows"]
            )
        )

    def test_default_realized_path_stays_inside_d1_prediction_interval(self):
        month_end = simulate(event="MONTH_END")
        rows = month_end["load_forecast"]["phases"][5]["rows"]
        outside = [
            row
            for row in rows
            if row["actual_mwh"] < row["p10_mwh"]
            or row["actual_mwh"] > row["p90_mwh"]
        ]
        self.assertFalse(outside)

    def test_scenario_editor_is_seeded_and_supports_outside_band_manual_overrides(self):
        first = simulate(event="MONTH_END")
        second = simulate(event="MONTH_END")
        load_first = first["scenario_editor"]["load"]["rows"]
        load_second = second["scenario_editor"]["load"]["rows"]
        price_first = first["scenario_editor"]["real_time_price"]["rows"]
        price_second = second["scenario_editor"]["real_time_price"]["rows"]
        self.assertEqual([row["actual"] for row in load_first], [row["actual"] for row in load_second])
        self.assertEqual([row["actual"] for row in price_first], [row["actual"] for row in price_second])
        self.assertTrue(all(row["p10"] <= row["actual"] <= row["p90"] for row in load_first))
        price_coverage = sum(not row["outside_band"] for row in price_first) / len(price_first)
        self.assertGreater(price_coverage, 0.65)
        self.assertLess(price_coverage, 0.90)
        self.assertEqual(
            first["scenario_editor"]["real_time_price"]["default_rule"],
            "SEEDED_INDEPENDENT_TRUTH_WITH_NATURAL_TAILS",
        )
        load_overrides = [None] * 96
        load_overrides[48] = 0.1
        price_overrides = [None] * 96
        price_overrides[47] = 5000.0
        edited = simulate(
            event="MONTH_END",
            actual_load_overrides=load_overrides,
            actual_real_time_price_overrides=price_overrides,
        )
        self.assertEqual(edited["scenario_editor"]["load"]["rows"][48]["source"], "MANUAL_OVERRIDE")
        self.assertTrue(edited["scenario_editor"]["load"]["rows"][48]["outside_band"])
        self.assertEqual(edited["scenario_editor"]["real_time_price"]["rows"][47]["actual"], 5000.0)
        self.assertTrue(edited["scenario_editor"]["real_time_price"]["rows"][47]["outside_band"])
        self.assertNotEqual(
            edited["settlement"]["optimized"]["wholesale_total_yuan"],
            first["settlement"]["optimized"]["wholesale_total_yuan"],
        )

    def test_scenario_seed_changes_generated_path(self):
        first = simulate(event="MONTH_END", load_scenario_seed=101)
        second = simulate(event="MONTH_END", load_scenario_seed=102)
        self.assertNotEqual(
            [row["actual"] for row in first["scenario_editor"]["load"]["rows"]],
            [row["actual"] for row in second["scenario_editor"]["load"]["rows"]],
        )

    def test_manual_override_validation_and_point_isolation(self):
        with self.assertRaises(ValueError):
            simulate(actual_load_overrides=[None] * 95)
        with self.assertRaises(ValueError):
            simulate(actual_real_time_price_overrides=[None] * 97)
        with self.assertRaises(ValueError):
            simulate(actual_load_overrides=[-0.1] + [None] * 95)
        base = simulate(event="MONTH_END")
        overrides = [None] * 96
        overrides[10] = 9.0
        edited = simulate(event="MONTH_END", actual_load_overrides=overrides)
        base_values = [row["actual"] for row in base["scenario_editor"]["load"]["rows"]]
        edited_values = [row["actual"] for row in edited["scenario_editor"]["load"]["rows"]]
        self.assertEqual(edited_values[10], 9.0)
        self.assertEqual(base_values[:10] + base_values[11:], edited_values[:10] + edited_values[11:])

    def test_real_time_prefix_updates_future_load_forecast(self):
        early = simulate(event="REAL_TIME", rt_period=8)
        later = simulate(event="REAL_TIME", rt_period=24)
        self.assertNotEqual(
            early["load_forecast"]["rows"][40]["p50_mwh"],
            later["load_forecast"]["rows"][40]["p50_mwh"],
        )

    def test_rt_decision_does_not_see_current_interval_actual_load(self):
        first = simulate(event="REAL_TIME", rt_period=1)
        self.assertTrue(
            all(row["actual_mwh"] is None for row in first["load_forecast"]["rows"])
        )
        twentieth = simulate(event="REAL_TIME", rt_period=20)
        self.assertIsNotNone(twentieth["load_forecast"]["rows"][18]["actual_mwh"])
        self.assertIsNone(twentieth["load_forecast"]["rows"][19]["actual_mwh"])

    def test_manual_future_truth_still_respects_rt_prefix_visibility(self):
        load_overrides = [None] * 96
        load_overrides[19] = 9.0
        price_overrides = [None] * 96
        price_overrides[19] = 5000.0
        twentieth = simulate(
            event="REAL_TIME",
            rt_period=20,
            actual_load_overrides=load_overrides,
            actual_real_time_price_overrides=price_overrides,
        )
        self.assertEqual(twentieth["scenario_editor"]["load"]["rows"][19]["actual"], 9.0)
        self.assertEqual(twentieth["scenario_editor"]["real_time_price"]["rows"][19]["actual"], 5000.0)
        self.assertIsNone(twentieth["load_forecast"]["rows"][19]["actual_mwh"])
        self.assertIsNone(twentieth["price_forecast"]["rows"][19]["official_real_time"])

    def test_price_information_timeline_locks_declaration_and_lags_rt_truth(self):
        d1 = simulate(event="D-1")
        reveal = simulate(event="D-1_PRICE")
        self.assertEqual(
            [row["declared_mwh"] for row in d1["declaration"]["rows"]],
            [row["declared_mwh"] for row in reveal["declaration"]["rows"]],
        )
        self.assertFalse(d1["price_forecast"]["day_ahead_official_visible"])
        self.assertTrue(reveal["price_forecast"]["day_ahead_official_visible"])
        self.assertEqual(reveal["execution_ledger"]["fixed_until"], 0)
        rt = simulate(event="REAL_TIME", rt_period=20)
        self.assertTrue(
            all(row["official_real_time"] is not None for row in rt["price_forecast"]["rows"][:19])
        )
        self.assertTrue(
            all(row["official_real_time"] is None for row in rt["price_forecast"]["rows"][19:])
        )
        self.assertEqual(
            [row["real_time_price"] for row in rt["execution_ledger"]["rows"][:19]],
            [row["official_real_time"] for row in rt["price_forecast"]["rows"][:19]],
        )
        self.assertTrue(
            all(
                row["official_real_time"] is None
                for row in rt["price_forecast"]["rows"][19:]
            )
        )

    def test_month_end_locks_the_complete_execution_day(self):
        payload = simulate(event="MONTH_END")
        self.assertEqual(payload["execution_ledger"]["fixed_until"], 96)

    def test_storage_visibility_distinguishes_conditional_plan_from_execution(self):
        cases = (
            ("TEN_DAY", False, "CONDITIONAL_PLAN_HIDDEN"),
            ("D-1", True, "DAY_AHEAD_CONDITIONAL_PLAN"),
            ("D-1_PRICE", True, "DAY_AHEAD_CONDITIONAL_PLAN"),
            ("REAL_TIME", True, "REAL_TIME_MPC"),
            ("MONTH_END", True, "EXECUTED_HISTORY"),
        )
        for event, visible, mode in cases:
            with self.subTest(event=event):
                payload = simulate(event=event, rt_period=20)
                self.assertEqual(payload["meta"]["storage_visibility"]["visible"], visible)
                self.assertEqual(payload["meta"]["storage_visibility"]["mode"], mode)

    def test_forecast_quality_is_explicitly_wape(self):
        payload = simulate(event="D-1")
        quality = payload["load_forecast"]["quality"]
        self.assertEqual(quality["percentage_error_metric"], "WAPE")
        self.assertEqual(quality["wape"], quality["mape"])

    def test_package_cap_changes_retail_revenue(self):
        capped = simulate(cap_price=440.0, service_fee=60.0)
        uncapped = simulate(cap_price=520.0, service_fee=60.0)
        self.assertTrue(capped["retail"]["cap_applied"])
        self.assertGreater(uncapped["retail"]["revenue_yuan"], capped["retail"]["revenue_yuan"])

    def test_d1_returns_96_decisions(self):
        payload = simulate(event="D-1")
        self.assertEqual(payload["declaration"]["period_count"], 96)
        self.assertEqual(len(payload["declaration"]["rows"]), 96)
        self.assertEqual(payload["declaration"]["status"], "LOCKED")
        self.assertEqual(len(payload["execution_ledger"]["rows"]), 96)

    def test_d1_energy_stack_reconciles_to_grid_load_after_storage(self):
        payload = simulate(event="D-1")
        breakdown = payload["declaration_breakdown"]
        self.assertEqual(len(breakdown["rows"]), 96)
        for row in breakdown["rows"]:
            self.assertAlmostEqual(
                row["long_term_mwh"]
                + row["day_ahead_buy_mwh"]
                - row["day_ahead_sell_mwh"]
                + row["real_time_buy_mwh"]
                - row["real_time_sell_mwh"],
                row["net_grid_load_after_storage_mwh"],
                places=3,
            )
        self.assertEqual(breakdown["assessment_guard"]["status"], "PASS")
        self.assertFalse(breakdown["assessment_guard"]["spot_counts_toward_long_term_assessment"])
        self.assertGreater(breakdown["totals"]["day_ahead_sell_mwh"], 0.0)
        self.assertGreater(breakdown["totals"]["real_time_buy_mwh"], 0.0)
        self.assertGreater(breakdown["totals"]["real_time_sell_mwh"], 0.0)
        self.assertGreater(
            breakdown["totals"]["real_time_sell_mwh"],
            breakdown["totals"]["real_time_buy_mwh"],
        )
        buy_periods = [
            row["period"] for row in breakdown["rows"] if row["real_time_buy_mwh"] > 0.0
        ]
        self.assertTrue(buy_periods)
        self.assertTrue(buy_periods)
        self.assertTrue(
            all(
                payload["execution_ledger"]["rows"][period - 1]["real_time_buy_mwh"]
                > 0.0
                for period in buy_periods
            )
        )

    def test_spot_deviation_is_hard_limited_at_each_quarter_hour(self):
        payload = strict_simulate(event="D-1")
        self.assertEqual(
            payload["declaration_breakdown"]["totals"]["spot_deviation_breach_periods"],
            0,
        )
        for row in payload["declaration_breakdown"]["rows"]:
            self.assertLessEqual(
                row["spot_deviation_abs_mwh"],
                row["spot_deviation_limit_mwh"] + 1e-6,
            )

    def test_storage_power_is_five_mw_on_quarter_hour_grid(self):
        payload = strict_simulate(event="D-1")
        self.assertAlmostEqual(payload["storage"]["power_mw"], 5.0)
        for row in payload["storage"]["rows"]:
            self.assertLessEqual(row["charge_from_grid_mwh"], 1.25 + 1e-8)
            self.assertLessEqual(row["discharge_to_load_mwh"], 1.25 + 1e-8)

    def test_real_time_prefix_remains_feasible_after_soc_locking(self):
        for period in (25, 48, 96):
            payload = strict_simulate(event="REAL_TIME", rt_period=period)
            self.assertEqual(payload["l3_objective"]["status"], "OPTIMAL")
            self.assertEqual(
                payload["declaration_breakdown"]["totals"]["spot_deviation_breach_periods"],
                0,
            )

    def test_execution_ledger_is_the_single_energy_balance_source(self):
        payload = simulate(event="D-1")
        for row in payload["execution_ledger"]["rows"]:
            self.assertAlmostEqual(row["balance_residual_mwh"], 0.0, places=6)
            self.assertAlmostEqual(
                row["grid_load_after_storage_mwh"],
                row["declared_mwh"]
                + row["real_time_buy_mwh"]
                - row["real_time_sell_mwh"],
                places=3,
            )

    def test_wholesale_physical_cost_uses_executed_real_time_and_storage(self):
        payload = simulate(event="D-1")
        factor = payload["load_forecast"]["month_equivalent_days"]
        rows = payload["execution_ledger"]["rows"]
        expected = sum(
            factor
            * (
                row["declared_mwh"] * row["day_ahead_price"]
                + (row["real_time_buy_mwh"] - row["real_time_sell_mwh"])
                * row["real_time_price"]
            )
            for row in rows
        )
        self.assertAlmostEqual(
            payload["settlement"]["optimized"]["physical_energy_cost_yuan"],
            expected,
            delta=5.0,
        )

    def test_real_time_replay_locks_declaration_and_executed_prefix(self):
        d1 = simulate(event="D-1")
        rt = simulate(event="REAL_TIME", rt_period=20)
        self.assertEqual(rt["execution_ledger"]["fixed_until"], 19)
        self.assertEqual(rt["mpc_state"]["state_version"], 19)
        self.assertEqual(
            [row["declared_mwh"] for row in d1["declaration"]["rows"]],
            [row["declared_mwh"] for row in rt["declaration"]["rows"]],
        )
        for d1_row, rt_row in zip(
            d1["storage"]["rows"][:19], rt["storage"]["rows"][:19]
        ):
            self.assertEqual(
                d1_row["charge_from_grid_mwh"], rt_row["charge_from_grid_mwh"]
            )
            self.assertEqual(
                d1_row["discharge_to_load_mwh"], rt_row["discharge_to_load_mwh"]
            )

    def test_portfolio_reports_product_scope_and_daily_position_separately(self):
        payload = simulate(event="D-1")
        actions = payload["portfolio"]["optimized"]["actions"]
        self.assertEqual(actions[0]["equivalent_delivery_days"], 27.76)
        self.assertEqual(actions[2]["equivalent_delivery_days"], 8.88)
        self.assertEqual(actions[3]["equivalent_delivery_days"], 1.0)
        self.assertEqual(
            payload["portfolio"]["optimized"]["position_unit"],
            "MWh/delivery_day",
        )

    def test_price_mock_has_midday_pv_discount_and_96_points(self):
        payload = simulate(event="D-1")
        rows = payload["price_forecast"]["rows"]
        self.assertEqual(len(rows), 96)
        midday = [row for row in rows if row["scenario"] == "MIDDAY_PV_SURPLUS"]
        self.assertTrue(midday)
        self.assertTrue(
            all(
                row["real_time_p50"] < row["day_ahead_p50"]
                for row in midday
            )
        )

    def test_l3_objective_includes_locked_contract_cost_and_storage(self):
        payload = simulate(event="D-1")
        objective = payload["l3_objective"]
        self.assertIn("锁定合同成本", objective["objective_definition"])
        self.assertNotEqual(objective["rolling_cfd_yuan"], 0.0)
        self.assertNotEqual(
            objective.get("locked_contract_cost_yuan", objective["l1_contract_cfd_yuan"]),
            0.0,
        )
        self.assertGreater(objective["storage_charge_mwh"], 0.0)
        self.assertGreater(objective["storage_discharge_mwh"], 0.0)

    def test_real_time_storage_state_progresses(self):
        early = simulate(event="REAL_TIME", rt_period=1)
        later = simulate(event="REAL_TIME", rt_period=20)
        self.assertEqual(early["storage"]["current"]["period"], 1)
        self.assertEqual(later["storage"]["current"]["period"], 20)
        self.assertNotEqual(early["storage"]["current"]["soc_mwh"], later["storage"]["current"]["soc_mwh"])

    def test_storage_uses_duck_curve_low_and_high_price_windows(self):
        payload = simulate(event="REAL_TIME")
        self.assertGreater(payload["storage"]["charged_mwh"], 0.0)
        self.assertGreater(payload["storage"]["discharged_mwh"], 0.0)
        charge_times = {
            row["time"] for row in payload["storage"]["rows"]
            if row["mode"] == "CHARGE"
        }
        discharge_times = {
            row["time"] for row in payload["storage"]["rows"]
            if row["mode"] == "DISCHARGE"
        }
        self.assertTrue(any("11:00" <= time < "13:00" for time in charge_times))
        self.assertTrue(any(
            "09:00" <= time < "11:00" or "13:00" <= time < "17:00"
            for time in discharge_times
        ))
        self.assertGreater(
            payload["storage"]["average_discharge_price_yuan_per_mwh"],
            payload["storage"]["average_charge_price_yuan_per_mwh"],
        )

    def test_retail_price_is_explicitly_marked_as_mock_assumption(self):
        payload = simulate()
        self.assertEqual(payload["retail"]["pricing_status"], "MOCK_ASSUMPTION_NOT_CUSTOMER_CONTRACT")
        configured = simulate(retail_pricing_source="USER_INPUT", package_type="FIXED", fixed_price=470.0)
        self.assertEqual(configured["retail"]["pricing_status"], "USER_CONFIGURED_CUSTOMER_CONTRACT")
        self.assertEqual(configured["retail"]["settled_price_yuan_per_mwh"], 470.0)

    def test_profit_identity(self):
        payload = simulate()
        expected = payload["retail"]["revenue_yuan"] - payload["settlement"]["optimized"]["wholesale_total_yuan"]
        self.assertAlmostEqual(payload["settlement"]["optimized_margin_yuan"], expected, places=2)


if __name__ == "__main__":
    unittest.main()
