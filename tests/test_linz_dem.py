"""Tests for New Zealand LiDAR 1m DEM tile selection and clipping."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from hydroscreen import (
    HydroScreenError,
    clip_dem_tiles,
    linz_tiles_for_bbox,
    load_linz_dem_1m_index,
    screening_dem_radius_m,
)


class LinzTileIndexTests(unittest.TestCase):
    def test_wellington_uses_bq31(self):
        tiles = load_linz_dem_1m_index()
        hits = linz_tiles_for_bbox((174.77, -41.29, 174.78, -41.28), tiles)
        self.assertIn("BQ31", hits)

    def test_christchurch_is_covered(self):
        tiles = load_linz_dem_1m_index()
        hits = linz_tiles_for_bbox((172.63, -43.54, 172.64, -43.53), tiles)
        self.assertTrue(hits)

    def test_pacific_ocean_is_outside_coverage(self):
        tiles = load_linz_dem_1m_index()
        hits = linz_tiles_for_bbox((160.0, -20.0, 160.1, -19.9), tiles)
        self.assertEqual(hits, [])

    def test_dem_radius_covers_transects_and_is_capped(self):
        self.assertEqual(screening_dem_radius_m(200, 300, 200), 300.0)
        self.assertEqual(screening_dem_radius_m(200, 8000, 200), 2000.0)


class ClipDemTilesTests(unittest.TestCase):
    def test_clips_window_from_local_tiles(self):
        transform = from_origin(1748000.0, 5429000.0, 1.0, 1.0)
        data = np.full((80, 80), 12.5, dtype=np.float32)
        data[0, 0] = -9999.0
        with tempfile.TemporaryDirectory() as tmp:
            src_path = Path(tmp) / "tile.tif"
            out_path = Path(tmp) / "clip.tif"
            with rasterio.open(
                src_path,
                "w",
                driver="GTiff",
                height=80,
                width=80,
                count=1,
                dtype="float32",
                crs="EPSG:2193",
                transform=transform,
                nodata=-9999.0,
            ) as dst:
                dst.write(data, 1)
            clip_dem_tiles(
                [str(src_path)],
                (1748020.0, 5428940.0, 1748060.0, 5428980.0),
                str(out_path),
            )
            with rasterio.open(out_path) as src:
                arr = src.read(1)
                self.assertEqual(src.crs.to_string(), "EPSG:2193")
                self.assertGreater(arr.size, 0)
                self.assertTrue(np.allclose(arr[arr != -9999], 12.5))

    def test_raises_when_window_is_all_nodata(self):
        transform = from_origin(1748000.0, 5429000.0, 1.0, 1.0)
        data = np.full((40, 40), -9999.0, dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            src_path = Path(tmp) / "empty.tif"
            out_path = Path(tmp) / "clip.tif"
            with rasterio.open(
                src_path,
                "w",
                driver="GTiff",
                height=40,
                width=40,
                count=1,
                dtype="float32",
                crs="EPSG:2193",
                transform=transform,
                nodata=-9999.0,
            ) as dst:
                dst.write(data, 1)
            with self.assertRaises(HydroScreenError):
                clip_dem_tiles(
                    [str(src_path)],
                    (1748010.0, 5428960.0, 1748020.0, 5428970.0),
                    str(out_path),
                )


if __name__ == "__main__":
    unittest.main()
