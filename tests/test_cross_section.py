"""Tests for the two-point DEM cross-section tool."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app import app
from hydroscreen import HydroScreenError, sample_drawn_cross_section


class DrawnCrossSectionTests(unittest.TestCase):
    def test_rejects_a_zero_length_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HydroScreenError):
                sample_drawn_cross_section(
                    174.7762,
                    -41.2865,
                    174.7762,
                    -41.2865,
                    dem_path=str(Path(tmp) / "unused.tif"),
                )

    def test_samples_elevations_along_a_two_point_line(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 30, 80
        grid = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            grid[:, col] = 40.0 - 0.2 * col
        transform = from_origin(west, north, res, res)
        lat = north - (height / 2) * res
        lon1 = west + 5 * res
        lon2 = west + (width - 5) * res
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dem.tif"
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
            profile = sample_drawn_cross_section(
                lon1, lat, lon2, lat, dem_path=str(path), spacing_m=5.0
            )
        self.assertGreater(profile["n_samples"], 5)
        self.assertGreater(profile["length_m"], 10)
        self.assertEqual(profile["source"], "local")
        self.assertGreater(profile["elevation_m"][0], profile["elevation_m"][-1])
        self.assertEqual(len(profile["distance_m"]), len(profile["elevation_m"]))


class CrossSectionApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_requires_two_points(self):
        response = self.client.post("/api/cross-section", data={})
        self.assertEqual(response.status_code, 400)

    def test_samples_linz_clip_for_the_chosen_bridge(self):
        from unittest.mock import patch
        import shutil
        from hydroscreen import plan_linz_clip

        fixture = Path(__file__).resolve().parent / "fixtures" / "sample_dem.tif"

        def fake_extract(lat, lon, out_tif, progress=None):
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture, out_tif)
            return str(out_tif), plan_linz_clip(lat, lon)

        with patch("hydroscreen.extract_linz_dem_for_bridge", side_effect=fake_extract):
            response = self.client.post(
                "/api/cross-section",
                data={
                    "lat1": -41.2865,
                    "lon1": 174.770,
                    "lat2": -41.2865,
                    "lon2": 174.782,
                    "lat": -41.2865,
                    "lon": 174.7762,
                    "sample_spacing": 5,
                },
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertGreater(data["n_samples"], 5)
        self.assertTrue(data["distance_m"])
        self.assertEqual(len(data["distance_m"]), len(data["elevation_m"]))


if __name__ == "__main__":
    unittest.main()
