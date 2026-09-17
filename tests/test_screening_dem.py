"""Screening uses the request-thread LiDAR clip, not a hanging worker."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString

from hydroscreen import clip_line_to_raster, projected_length_m, run_screening


def _write_sloped_dem(path, west, north, res, height, width):
    grid = np.zeros((height, width), dtype=np.float32)
    for col in range(width):
        grid[:, col] = 20.0 - 0.05 * col
    grid[height // 2, :] -= 3.0
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
    return lat, lon


class ClipCentrelineTests(unittest.TestCase):
    def test_keeps_only_the_segment_on_the_raster(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 20, 40
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dem.tif"
            lat, lon = _write_sloped_dem(path, west, north, res, height, width)
            east = west + width * res
            line = LineString(
                [
                    [west - 0.01, lat],
                    [west + 5 * res, lat],
                    [east - 5 * res, lat],
                    [east + 0.01, lat],
                ]
            )
            clipped = clip_line_to_raster(line, str(path), lon=lon, lat=lat)
            self.assertIsNotNone(clipped)
            xs = [c[0] for c in clipped.coords]
            self.assertGreater(min(xs), west - 1e-6)
            self.assertLess(max(xs), east + 1e-6)
            self.assertLess(projected_length_m(clipped), projected_length_m(line) - 100)


class ScreeningLinzDownloadTests(unittest.TestCase):
    def test_linz_clip_runs_on_the_caller_thread_and_keeps_the_geotiff(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 30, 60
        caller = threading.get_ident()
        seen = {}

        def fake_extract(lat, lon, out_tif, progress=None, **kwargs):
            seen["ident"] = threading.get_ident()
            seen["out"] = out_tif
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            _write_sloped_dem(out_tif, west, north, res, height, width)
            from hydroscreen import plan_linz_clip
            return str(out_tif), plan_linz_clip(lat, lon)

        lat = north - (height / 2) * res
        lon = west + (width / 2) * res
        centerline = [
            [west + 5 * res, lat],
            [west + (width - 5) * res, lat],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache")}):
                with patch("hydroscreen.extract_linz_dem_for_bridge", side_effect=fake_extract):
                    result = run_screening(
                        lat=lat,
                        lon=lon,
                        dem=None,
                        outdir=str(Path(tmp) / "out"),
                        interval=40.0,
                        along_m=80.0,
                        length=20.0,
                        sample_spacing=10.0,
                        centerline_coords=centerline,
                        flow_m3_s=1.0,
                        cancel_event=threading.Event(),
                    )
            self.assertEqual(seen["ident"], caller)
            kept = Path(result["outdir"]) / "dem_aoi.tif"
            self.assertTrue(kept.exists())
            self.assertGreater(kept.stat().st_size, 256)
            self.assertEqual(result["layout"]["dem_source"], "linz-lidar-1m")
            self.assertEqual(result["layout"]["dem_file"], "dem_aoi.tif")
            self.assertIn("BQ31", result["layout"]["tiles"])
            self.assertGreaterEqual(len(result["summary"]), 1)

    def test_long_drawn_line_is_clipped_to_the_dem(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 20, 40
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dem.tif"
            lat, lon = _write_sloped_dem(path, west, north, res, height, width)
            long_line = [
                [west - 0.02, lat],
                [west + (width + 200) * res, lat],
            ]
            result = run_screening(
                lat=lat,
                lon=lon,
                dem=str(path),
                outdir=str(Path(tmp) / "out"),
                interval=40.0,
                along_m=5000.0,
                length=20.0,
                sample_spacing=10.0,
                centerline_coords=long_line,
                flow_m3_s=1.0,
            )
            self.assertLess(result["layout"]["along_m"], 800)
            self.assertGreater(result["layout"]["along_m"], 50)
            self.assertGreaterEqual(len(result["summary"]), 1)


if __name__ == "__main__":
    unittest.main()
