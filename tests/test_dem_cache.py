"""DEM clip cache and faster screening helpers."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from hydroscreen import (
    download_linz_lidar_1m,
    expand_bbox_for_cache,
    plot_cross_section,
    sample_dem_along_line,
    square_clip_2193,
    square_clip_bbox_4326,
)
from shapely.geometry import LineString


class BboxCacheTests(unittest.TestCase):
    def test_nearby_windows_snap_to_the_same_clip(self):
        a = expand_bbox_for_cache((174.771, -41.287, 174.779, -41.281))
        b = expand_bbox_for_cache((174.773, -41.286, 174.778, -41.282))
        self.assertEqual(a, b)
        self.assertLessEqual(a[0], 174.771)
        self.assertGreaterEqual(a[2], 174.779)


class LinzClipCacheTests(unittest.TestCase):
    def test_second_download_reuses_the_cached_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            out1 = Path(tmp) / "a.tif"
            out2 = Path(tmp) / "b.tif"
            calls = []

            def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None):
                calls.append(out_tif)
                Path(out_tif).write_bytes(b"CLIPPED-DEM" * 40)
                return out_tif

            with patch.dict(os.environ, {"HYDROBRIDGE_DEM_CACHE": str(cache)}):
                with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                    first = download_linz_lidar_1m(
                        (174.77, -41.29, 174.78, -41.28), str(out1)
                    )
                    second = download_linz_lidar_1m(
                        (174.771, -41.289, 174.779, -41.281), str(out2)
                    )
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(first).read_bytes(), Path(second).read_bytes())

    def test_site_square_clips_share_one_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            out1 = Path(tmp) / "a.tif"
            out2 = Path(tmp) / "b.tif"
            calls = []

            def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None):
                calls.append(bounds)
                Path(out_tif).write_bytes(b"SQUARE-CLIP" * 40)
                return out_tif

            bounds_a = square_clip_2193(-41.2865, 174.7762)
            bounds_b = square_clip_2193(-41.28655, 174.77625)
            bbox_a = square_clip_bbox_4326(-41.2865, 174.7762)
            bbox_b = square_clip_bbox_4326(-41.28655, 174.77625)
            with patch.dict(os.environ, {"HYDROBRIDGE_DEM_CACHE": str(cache)}):
                with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                    download_linz_lidar_1m(bbox_a, str(out1), bounds_2193=bounds_a)
                    download_linz_lidar_1m(bbox_b, str(out2), bounds_2193=bounds_b)
            self.assertEqual(len(calls), 1)
            west, south, east, north = calls[0]
            self.assertAlmostEqual(east - west, 500.0, places=5)
            self.assertAlmostEqual(north - south, 500.0, places=5)


class SharedRasterSampleTests(unittest.TestCase):
    def test_shared_dataset_matches_reopening_the_file(self):
        import rasterio
        from rasterio.transform import from_origin

        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 20, 40
        grid = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            grid[:, col] = 10.0 + col
        transform = from_origin(west, north, res, res)
        lat = north - (height / 2) * res
        lon1 = west + 5 * res
        lon2 = west + (width - 5) * res
        line = LineString([(lon1, lat), (lon2, lat)])
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
            ) as dst:
                dst.write(grid, 1)
            opened = sample_dem_along_line(str(path), line, spacing_m=5.0)
            with rasterio.open(path) as src:
                shared = sample_dem_along_line(str(path), line, spacing_m=5.0, src=src)
        np.testing.assert_allclose(opened[0], shared[0])
        np.testing.assert_allclose(opened[1], shared[1])


class PlotReuseTests(unittest.TestCase):
    def test_writes_a_png_without_leaking_figures(self):
        dists = np.array([0.0, 10.0, 20.0])
        elevs = np.array([4.0, 1.0, 4.0])
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            plot_cross_section(dists, elevs, str(first), water_level=2.0)
            plot_cross_section(dists, elevs, str(second), water_level=2.5)
            self.assertGreater(first.stat().st_size, 100)
            self.assertGreater(second.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
