"""The map UI must expose working Run and Cross-section controls."""

import json
import unittest

from app import app


class UiMarkupTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_index_includes_run_and_cross_section_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="run-btn"', html)
        self.assertIn('id="mode-xs"', html)
        self.assertIn('id="run-status"', html)
        self.assertIn("novalidate", html)
        self.assertIn('id="busy"', html)
        self.assertIn('id="busy-bar"', html)
        self.assertIn('id="busy-pct"', html)
        self.assertIn('id="dem-opacity"', html)
        self.assertIn("500 m", html)
        self.assertIn("busy-spinner", html)
        self.assertNotIn("disabled>Run screening", html)

    def test_dem_preview_stream_reports_percent(self):
        response = self.client.post(
            "/api/dem-preview?stream=1",
            data={
                "lat": -41.2865,
                "lon": 174.7762,
                "dem_source": "sample",
                "stream": "1",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:500])
        events = [
            json.loads(line)
            for line in response.get_data(as_text=True).splitlines()
            if line.strip()
        ]
        self.assertTrue(events)
        percents = [item["percent"] for item in events if "percent" in item]
        self.assertTrue(percents)
        self.assertEqual(percents[0], 0)
        self.assertEqual(percents[-1], 100)
        done = events[-1]
        self.assertTrue(done.get("png"))
        self.assertTrue(done.get("bounds"))

    def test_cross_section_api_still_accepts_two_points(self):
        response = self.client.post(
            "/api/cross-section",
            data={
                "lat1": -41.2865,
                "lon1": 174.770,
                "lat2": -41.2865,
                "lon2": 174.782,
                "dem_source": "sample",
                "sample_spacing": 5,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertGreater(response.get_json()["n_samples"], 5)


if __name__ == "__main__":
    unittest.main()
