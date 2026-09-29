import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from service.simulator import summary


ROOT = Path(__file__).resolve().parent.parent


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.append(value)


class WebContractTests(unittest.TestCase):
    def test_html_ids_are_unique_and_cover_javascript_bindings(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        parser = IdParser()
        parser.feed(html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        requested_ids = set(re.findall(r'getElementById\("([^"]+)"\)', javascript))
        self.assertFalse(requested_ids - set(parser.ids))

    def test_summary_is_json_serializable_and_has_eight_timeline_events(self):
        payload = summary(diagnostic_mode=True)
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(len(payload["timeline"]), 9)
        self.assertEqual(len(payload["load_forecast"]["rows"]), 96)
        self.assertEqual(len(payload["declaration"]["rows"]), 96)
        self.assertEqual(len(payload["declaration_breakdown"]["rows"]), 96)
        self.assertEqual(len(payload["storage"]["rows"]), 96)
        self.assertIn("l3_objective", payload)
        self.assertEqual(len(payload["load_forecast"]["history"]), 102)
        self.assertEqual(len(payload["load_forecast"]["phases"]), 102)
        self.assertEqual(len(payload["price_forecast"]["rows"]), 96)
        self.assertIn("portfolio", payload)
        self.assertIn("retail", payload)
        self.assertTrue(
            all(row["actual_mwh"] is None for row in payload["load_forecast"]["rows"])
        )

    def test_frontend_exposes_forecast_and_three_result_sections(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for required in (
            "forecast-chart",
            "decision-table",
            "declaration-chart",
            "storage-chart",
            "mock-data-dialog",
            "mock-load-table",
            "mock-price-table",
            "mock-load-chart",
            "mock-price-chart",
            "confirm-scenario-edits",
            "confirm-scenario-edits-price",
            "load-scenario-seed",
            "price-scenario-seed",
            "margin-improvement",
            "overall-assessment",
            "declaration-exposure-total",
            "declaration-grid-total",
        ):
            self.assertIn('id="%s"' % required, html)
        for removed in ("metric-revenue", "margin-bridge", "audit-list", "portfolio-chart"):
            self.assertNotIn('id="%s"' % removed, html)
        for required_line in (
            "forecast-confidence-area",
            "forecast-p10-line",
            "forecast-p50",
            "forecast-p90-line",
            "forecast-actual",
        ):
            self.assertIn(required_line, javascript)
        self.assertNotIn("forecast-stage-line", javascript)
        self.assertNotIn("forecast-band", javascript)
        self.assertIn("row.actual_mwh == null", javascript)
        self.assertIn("button.disabled = !phase.available", javascript)
        self.assertIn("actual_load_overrides", javascript)
        self.assertIn("actual_real_time_price_overrides", javascript)
        self.assertIn("scenarioDirty", javascript)
        self.assertIn("pointerdown", javascript)
        self.assertIn("declaration-zero-axis", javascript)
        self.assertIn("declaration-layer-boundary", javascript)
        self.assertIn("annual_assessment_ratio", javascript)
        self.assertIn("overall_assessment_ratio", javascript)
        self.assertIn("spot_exposure_buy_mwh", javascript)
        self.assertIn("spot_exposure_sell_mwh", javascript)
        self.assertIn("exposureOnly", javascript)
        self.assertNotIn("const minY = -maxY", javascript)
        self.assertIn("compliant_periods", javascript)
        self.assertIn('svgNode("line", {', javascript)
        self.assertNotRegex(
            javascript,
            r'svgNode\("path"[^\n]+declaration-layer-boundary',
        )
        self.assertIn('svgNode("line", {', javascript)
        self.assertNotIn("path(boundary)", javascript)
        self.assertIn("柱内虚线分层", html)
        self.assertIn('-Number(row.day_ahead_sell_mwh || 0)', javascript)
        self.assertIn('-Number(row.real_time_sell_mwh || 0)', javascript)
        self.assertIn('储能电量 / MWh', javascript)
        self.assertIn('储能总电量 SOC', html)
        self.assertIn('class="visual-section storage-section"', html)
        self.assertIn('storage-section" aria-labelledby="storage-title" hidden', html)
        self.assertIn("storage_visibility", javascript)
        self.assertIn("const storageVisible = Boolean(storageVisibility.visible);", javascript)
        self.assertIn("if (storageVisible) {", javascript)
        self.assertIn("renderStorage(data);", javascript)
        self.assertIn("D-1 储能充放电条件计划", html)
        self.assertIn("DAY_AHEAD_CONDITIONAL_PLAN", javascript)
        self.assertIn('id="storage-rt-selector" hidden', html)
        self.assertIn('storageMode !== "REAL_TIME_MPC"', javascript)
        self.assertIn('storageMode === "REAL_TIME_MPC"', javascript)
        self.assertIn("全天96点条件计划 · 尚未执行", javascript)
        self.assertNotIn('ΔSOC', html)
        self.assertIn('实时电价 / 元·MWh⁻¹', javascript)
        self.assertIn('const rawEnergyDomain = niceDomain([...socStarts, ...socValues])', javascript)
        self.assertIn('min: Math.max(0, rawEnergyDomain.min)', javascript)
        self.assertIn('const actionTop = margin.top + mainH + actionGap', javascript)
        self.assertIn('const yAction = (value)', javascript)
        self.assertIn('"storage-action-bg"', javascript)
        self.assertIn('额定单点上限', javascript)
        self.assertIn('峰值利用率', javascript)
        self.assertNotIn('socChanges', javascript)
        self.assertIn('const priceDomain = niceDomain(prices)', javascript)
        self.assertNotIn('value / 700', javascript)
        self.assertNotIn(
            'drawSegment(index, row.long_term_mwh, row.declaration_mwh',
            javascript,
        )
        self.assertNotIn(
            'drawSegment(index, row.declaration_mwh, row.actual_load_mwh',
            javascript,
        )
        self.assertFalse(list(ROOT.glob("data/**/*.csv")))


if __name__ == "__main__":
    unittest.main()
