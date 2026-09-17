"""Drawn centreline length is the analysis reach."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString

from hydroscreen import generate_transects, projected_length_m, run_screening


class CoverFullLineTests(unittest.TestCase):
    def test_projected_length_is_metres_along_the_line(self):
        lat = -43.532
        line = LineString([(172.630, lat), (172.636, lat)])
        length = projected_length_m(line)
        self.assertGreater(length, 400)
        self.assertLess(length, 800)

    def test_stations_run_from_the_start_to_the_end_of_the_drawn_line(self):
        lat = -43.532
        lon0, lon1 = 172.630, 172.636
        line = LineString([(lon0, lat), (lon1, lat)])
        length = projected_length_m(line)
        transects = generate_transects(
            line,
            interval=100,
            length_m=40,
            bridge_lon=lon0,
            bridge_lat=lat,
            cover_full_line=True,
        )
        offsets = [station for _geom, station in transects]
        self.assertGreaterEqual(len(offsets), 5)
        self.assertAlmostEqual(min(offsets), 0.0, delta=1.0)
        self.assertAlmostEqual(max(offsets), length, delta=2.0)
        steps = np.diff(sorted(offsets))
        self.assertTrue(all(step <= 100.5 for step in steps))

    def test_run_screening_uses_drawn_length_instead_of_along_m(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 40, 80
        grid = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            grid[:, col] = 30.0 - 0.1 * col
        grid[height // 2, :] -= 4.0
        transform = from_origin(west, north, res, res)
        lon = west + (width / 2) * res
        lat = north - (height / 2) * res
        centerline = [
            [west + 5 * res, lat],
            [west + (width - 5) * res, lat],
        ]
        expected = projected_length_m(LineString(centerline))
        with tempfile.TemporaryDirectory() as tmp:
            dem_path = Path(tmp) / "dem.tif"
            with rasterio.open(
                dem_path,
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
                flow_m3_s=1.0,
                mannings_n=0.035,
            )
        self.assertAlmostEqual(result["layout"]["along_m"], expected, delta=1.0)
        self.assertGreater(result["layout"]["along_m"], 200)
        self.assertGreater(result["layout"]["n_transects"], 3)
        offsets = [abs(feat["offset_m"]) for feat in result["transects"]]
        self.assertGreater(max(offsets), 80.0)
        profile = result["centerline_profile"]
        self.assertGreaterEqual(len(profile["distance_m"]), 2)
        self.assertEqual(len(profile["distance_m"]), len(profile["elevation_m"]))
        self.assertIn("origin_m", profile)
        first = result["transects"][0]
        self.assertGreaterEqual(len(first["distance_m"]), 2)
        self.assertEqual(len(first["distance_m"]), len(first["elevation_m"]))
        self.assertIn("station_m", first)
        self.assertGreaterEqual(first["station_m"], 0.0)
        self.assertLessEqual(first["station_m"], profile["length_m"] + 1.0)


if __name__ == "__main__":
    unittest.main()
