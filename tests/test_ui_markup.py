"""The map UI must expose working Run and Cross-section controls."""

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
        self.assertIn("500 m", html)
        self.assertIn("busy-spinner", html)
        self.assertNotIn("disabled>Run screening", html)

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
