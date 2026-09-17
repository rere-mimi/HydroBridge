"""Multi-ARI flow scenarios overlay water level and velocity."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app import app
from hydroscreen import (
    HydroScreenError,
    normalize_flow_scenarios,
    run_screening,
)


def _write_channel_dem(path, west=174.770, north=-41.280, res=0.0001, height=40, width=80):
    grid = np.zeros((height, width), dtype=np.float32)
    for col in range(width):
        grid[:, col] = 30.0 - 0.1 * col
    grid[height // 2, :] -= 4.0
    transform = from_origin(west, north, res, res)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999,
    ) as dst:
        dst.write(grid, 1)
    lat = north - (height / 2) * res
    lon = west + (width / 2) * res
    centerline = [
        [west + 5 * res, lat],
        [west + (width - 5) * res, lat],
    ]
    return lat, lon, centerline


class NormalizeFlowScenariosTests(unittest.TestCase):
    def test_default_is_100_year(self):
        scenarios = normalize_flow_scenarios(10)
        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0]["years"], 100)
        self.assertEqual(scenarios[0]["key"], "100y")
        self.assertEqual(scenarios[0]["flow_m3_s"], 10.0)
        self.assertEqual(scenarios[0]["color"], "#7c3aed")

    def test_keeps_selected_order_and_colours(self):
        scenarios = normalize_flow_scenarios(
            10,
            [
                {"years": 10, "flow_m3_s": 4},
                {"years": 1000, "flow_m3_s": 20},
                {"years": 100, "flow_m3_s": 9},
            ],
        )
        self.assertEqual([item["years"] for item in scenarios], [10, 1000, 100])
        self.assertEqual(scenarios[0]["label"], "10-year ARI")
        self.assertEqual(scenarios[1]["label"], "1,000-year ARI")
        self.assertEqual(scenarios[0]["color"], "#0284c7")
        self.assertEqual(scenarios[1]["color"], "#be123c")

    def test_rejects_unknown_and_empty_selection(self):
        with self.assertRaises(HydroScreenError):
            normalize_flow_scenarios(10, [{"years": 20, "flow_m3_s": 1}])
        with self.assertRaises(HydroScreenError):
            normalize_flow_scenarios(10, [])
        with self.assertRaises(HydroScreenError):
            normalize_flow_scenarios(10, [{"years": 10, "flow_m3_s": -1}])


class MultiAriScreeningTests(unittest.TestCase):
    def test_run_screening_solves_each_selected_ari(self):
        with tempfile.TemporaryDirectory() as tmp:
            dem_path = Path(tmp) / "dem.tif"
            lat, lon, centerline = _write_channel_dem(dem_path)
            result = run_screening(
                lat=lat,
                lon=lon,
                dem=str(dem_path),
                outdir=str(Path(tmp) / "out"),
                interval=80.0,
                along_m=80.0,
                length=40.0,
                sample_spacing=10.0,
                centerline_coords=centerline,
                flow_m3_s=4.0,
                flow_scenarios=[
                    {"years": 10, "flow_m3_s": 2.0},
                    {"years": 100, "flow_m3_s": 8.0},
                ],
                mannings_n=0.035,
            )
        keys = [item["key"] for item in result["layout"]["flow_scenarios"]]
        self.assertEqual(keys, ["10y", "100y"])
        self.assertAlmostEqual(result["layout"]["flow_m3_s"], 2.0)
        self.assertGreater(result["layout"]["slope"], 0)
        self.assertIn("slope", result["centerline_profile"])
        self.assertIn("fit_slope", result["centerline_profile"])
        self.assertIn("fit_intercept", result["centerline_profile"])
        first = result["transects"][0]
        self.assertIn("10y", first["aris"])
        self.assertIn("100y", first["aris"])
        low = first["aris"]["10y"]["water_level_m"]
        high = first["aris"]["100y"]["water_level_m"]
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertGreaterEqual(high, low)
        self.assertGreater(first["aris"]["100y"]["velocity_m_s"], 0)
        summary = result["summary"][0]
        self.assertIn("10y", summary["aris"])
        self.assertAlmostEqual(summary["target_discharge_m3_s"], 2.0)


class AriApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_invalid_ari_is_rejected_before_screening(self):
        with patch("app.run_screening") as mock_run:
            response = self.client.post(
                "/api/run",
                data={
                    "lat": -41.2865,
                    "lon": 174.7762,
                    "interval": 50,
                    "length": 200,
                    "sample_spacing": 1,
                    "mannings_n": 0.035,
                    "flow": 10,
                    "aris": json.dumps([{"years": 20, "flow_m3_s": 3}]),
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("return period", response.get_json()["error"].lower())
        mock_run.assert_not_called()

    def test_run_passes_selected_aris(self):
        fake = {
            "centerline_source": "drawn",
            "used_synthetic_centerline": False,
            "layout": {
                "flow_m3_s": 4.0,
                "flow_scenarios": [
                    {"years": 10, "key": "10y", "label": "10-year ARI", "flow_m3_s": 4.0, "color": "#0284c7"},
                    {"years": 100, "key": "100y", "label": "100-year ARI", "flow_m3_s": 10.0, "color": "#7c3aed"},
                ],
                "slope": 0.002,
                "mannings_n": 0.035,
                "n_transects": 0,
                "sample_spacing_m": 1,
            },
            "centerline": [],
            "centerline_profile": {"distance_m": [], "elevation_m": [], "slope": 0.002},
            "transects": [],
            "summary": [],
            "summary_xlsx": "summary.xlsx",
        }
        with patch("app.run_screening", return_value=fake) as mock_run:
            response = self.client.post(
                "/api/run",
                data={
                    "lat": -41.2865,
                    "lon": 174.7762,
                    "interval": 50,
                    "length": 200,
                    "sample_spacing": 1,
                    "mannings_n": 0.035,
                    "flow": 10,
                    "aris": json.dumps([
                        {"years": 10, "flow_m3_s": 4},
                        {"years": 100, "flow_m3_s": 10},
                    ]),
                },
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:400])
        kwargs = mock_run.call_args.kwargs
        self.assertEqual([item["years"] for item in kwargs["flow_scenarios"]], [10, 100])
        payload = response.get_json()
        self.assertEqual(len(payload["layout"]["flow_scenarios"]), 2)
        self.assertEqual(payload["centerline_profile"]["slope"], 0.002)


class AriSummaryMarkupTests(unittest.TestCase):
    def test_index_summary_table_is_return_period_first(self):
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertIn("Return period", html)
        self.assertIn("Water level (max depth)", html)
        self.assertIn("Flow (m³/s)", html)
        self.assertNotIn("<th>Samples</th>", html)
        self.assertNotIn("<th>Width (m)</th>", html)
        self.assertNotIn("<th>Area (m²)</th>", html)
        self.assertNotIn("<th>Hyd. radius (m)</th>", html)
        self.assertIn("<th>Transect</th>", html)
        self.assertIn("<th>Offset (m)</th>", html)
        self.assertIn("<th>Status</th>", html)

    def test_client_groups_summary_rows_by_return_period_colour(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("function hydStatus", js)
        self.assertIn("result-ari-row", js)
        self.assertIn("resultsArisExpanded", js)
        self.assertIn("Orange numbered dots = transect stations", js)
        self.assertIn("orange numbered dots mark transect stations", js)
        self.assertIn("--ari-color", js)
        self.assertIn(".results-scroll", css)
        self.assertIn("result-card-label", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("inset 5px 0 0 var(--ari-color", css)


if __name__ == "__main__":
    unittest.main()
