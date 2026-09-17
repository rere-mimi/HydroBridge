"""The map UI must expose working Run and Cross-section controls."""

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from hydroscreen import plan_linz_clip

SAMPLE_DEM = Path(__file__).resolve().parent / "fixtures" / "sample_dem.tif"


def _fake_extract(lat, lon, out_tif, progress=None, **kwargs):
    Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE_DEM, out_tif)
    return str(out_tif), plan_linz_clip(lat, lon)


def _fake_iter_extract(lat, lon, out_tif, **kwargs):
    path, plan = _fake_extract(lat, lon, out_tif)
    yield {"percent": 8, "message": "Downloading", "plan": plan}
    yield {"percent": 100, "message": "DEM ready", "path": path, "plan": plan}


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
        self.assertIn('name="upstream"', html)
        self.assertIn('name="downstream"', html)
        self.assertIn('name="lateral"', html)
        self.assertIn("area of interest", html)
        self.assertIn("busy-spinner", html)
        self.assertIn("xs-measure", html)
        self.assertIn('id="xs-measure"', html)
        self.assertIn('id="xs-title"', html)
        self.assertIn("id=\"dem-tiles\"", html)
        self.assertIn("LINZ 121859", html)
        self.assertIn("class=\"app-frame\"", html)
        self.assertIn("class=\"workspace\"", html)
        self.assertIn("class=\"map-stage\"", html)
        self.assertIn("area of interest", html)
        self.assertIn("Return period (ARI)", html)
        self.assertIn("1,000-year", html)
        self.assertIn('id="ari-list"', html)
        self.assertIn('data-ari="10"', html)
        self.assertIn('data-ari="25"', html)
        self.assertIn('data-ari="50"', html)
        self.assertIn('data-ari="100"', html)
        self.assertIn('data-ari="1000"', html)
        self.assertNotIn("Bundled Wellington", html)
        self.assertNotIn("Upload a GeoTIFF", html)
        self.assertNotIn("disabled>Run screening", html)

    def test_dem_preview_stream_reports_percent(self):
        with patch("hydroscreen.iter_extract_linz_dem_for_bridge", side_effect=_fake_iter_extract):
            response = self.client.post(
                "/api/dem-preview?stream=1",
                data={
                    "lat": -41.2865,
                    "lon": 174.7762,
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
        self.assertIn("BQ31", done.get("tiles") or [])

    def test_cross_section_api_still_accepts_two_points(self):
        with patch("hydroscreen.extract_linz_dem_for_bridge", side_effect=_fake_extract):
            response = self.client.post(
                "/api/cross-section",
                data={
                    "lat1": -41.2865,
                    "lon1": 174.770,
                    "lat2": -41.2865,
                    "lon2": 174.782,
                    "sample_spacing": 5,
                },
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertGreater(response.get_json()["n_samples"], 5)


if __name__ == "__main__":
    unittest.main()
