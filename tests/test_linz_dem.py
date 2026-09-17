"""Tests for New Zealand LiDAR 1m DEM tile selection and clipping."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from shapely.geometry import LineString

from hydroscreen import (
    HydroScreenError,
    centerline_reach,
    clip_dem_tiles,
    linz_tiles_for_bbox,
    load_linz_dem_1m_index,
    render_dem_overlay_png,
    screening_dem_radius_m,
    square_clip_2193,
    square_clip_bbox_4326,
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

    def test_dem_clip_is_a_fixed_500m_square(self):
        self.assertEqual(screening_dem_radius_m(200, 300, 200), 250.0)
        self.assertEqual(screening_dem_radius_m(200, 8000, 200), 250.0)

    def test_site_clip_is_500m_square_in_nztm(self):
        west, south, east, north = square_clip_2193(-41.2865, 174.7762)
        self.assertAlmostEqual(east - west, 500.0, places=6)
        self.assertAlmostEqual(north - south, 500.0, places=6)

    def test_nearby_pins_share_the_same_snapped_square(self):
        a = square_clip_2193(-41.2865, 174.7762)
        b = square_clip_2193(-41.28655, 174.77625)
        self.assertEqual(a, b)

    def test_geographic_envelope_covers_the_nztm_square(self):
        bbox = square_clip_bbox_4326(-41.2865, 174.7762)
        self.assertEqual(len(bbox), 4)
        minx, miny, maxx, maxy = bbox
        self.assertLess(minx, maxx)
        self.assertLess(miny, maxy)
        self.assertLess(maxx - minx, 0.01)
        self.assertLess(maxy - miny, 0.01)

    def test_centerline_reach_is_centred_on_the_pin(self):
        line = LineString([(174.770, -41.290), (174.7762, -41.2865), (174.782, -41.283)])
        reach = centerline_reach(line, 174.7762, -41.2865, along_m=200)
        self.assertGreaterEqual(len(list(reach.coords)), 2)


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

    def test_reports_clip_progress(self):
        transform = from_origin(1748000.0, 5429000.0, 1.0, 1.0)
        data = np.full((80, 80), 12.5, dtype=np.float32)
        seen = []

        def on_progress(fraction, message=None):
            seen.append((float(fraction), message))

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
                progress=on_progress,
            )
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(seen[-1][0], 1.0)
        percents = [item[0] for item in seen]
        self.assertEqual(percents, sorted(percents))

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


class DemOverlayTests(unittest.TestCase):
    def test_renders_transparent_png_with_wgs84_bounds(self):
        west, north = 174.770, -41.280
        res = 0.0002
        height, width = 40, 50
        data = np.zeros((height, width), dtype=np.float32)
        for row in range(height):
            data[row, :] = 20.0 + 0.4 * row
        transform = from_origin(west, north, res, res)
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
                dst.write(data, 1)
            png, bounds = render_dem_overlay_png(
                str(path),
                (west + res, north - (height - 2) * res, west + (width - 2) * res, north - res),
            )
        self.assertTrue(png.startswith(b"\x89PNG"))
        out_west, out_south, out_east, out_north = bounds
        self.assertLess(out_west, out_east)
        self.assertLess(out_south, out_north)


if __name__ == "__main__":
    unittest.main()
