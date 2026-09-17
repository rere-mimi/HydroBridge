"""Area-of-interest rectangle from bridge extents, then windowed LINZ crop."""

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from hydroscreen import (
    HydroScreenError,
    aoi_downstream_unit_2193,
    aoi_polygon_2193,
    bridge_aoi,
    clip_dem_tiles,
    extract_linz_dem_for_bridge,
    linz_window_uris,
    parse_aoi_extent,
    plan_linz_clip,
    square_clip_2193,
)


WELLINGTON = (-41.2865, 174.7762)


class AoiGeometryTests(unittest.TestCase):
    def test_default_north_up_matches_the_500m_square(self):
        aoi = bridge_aoi(*WELLINGTON)
        self.assertEqual(aoi["bounds_2193"], square_clip_2193(*WELLINGTON))
        self.assertAlmostEqual(aoi["along_m"], 500.0, places=5)
        self.assertAlmostEqual(aoi["width_m"], 500.0, places=5)
        self.assertAlmostEqual(aoi["downstream_unit_2193"][0], 0.0, places=6)
        self.assertAlmostEqual(aoi["downstream_unit_2193"][1], -1.0, places=6)

    def test_asymmetric_extents_shift_the_rectangle(self):
        aoi = bridge_aoi(*WELLINGTON, upstream_m=100, downstream_m=400, lateral_m=80)
        ox, oy = aoi["origin_2193"]
        up = aoi["points_2193"]["upstream"]
        down = aoi["points_2193"]["downstream"]
        self.assertAlmostEqual(math.hypot(up[0] - ox, up[1] - oy), 100.0, places=4)
        self.assertAlmostEqual(math.hypot(down[0] - ox, down[1] - oy), 400.0, places=4)
        west, south, east, north = aoi["bounds_2193"]
        self.assertAlmostEqual(east - west, 160.0, places=4)
        self.assertAlmostEqual(north - south, 500.0, places=4)
        self.assertGreater(oy - south, north - oy)

    def test_corners_form_a_rectangle_not_a_diamond(self):
        aoi = bridge_aoi(*WELLINGTON, upstream_m=120, downstream_m=280, lateral_m=90)
        poly = aoi_polygon_2193(aoi)
        self.assertAlmostEqual(poly.area, 400.0 * 180.0, delta=1.0)
        midpoints = [
            aoi["points_2193"]["upstream"],
            aoi["points_2193"]["right"],
            aoi["points_2193"]["downstream"],
            aoi["points_2193"]["left"],
            aoi["points_2193"]["upstream"],
        ]
        diamond = Polygon(midpoints)
        self.assertLess(diamond.area, poly.area * 0.6)
        ring = aoi["ring_2193"]
        ul, ur, dr, dl = ring[0], ring[1], ring[2], ring[3]
        def dist(a, b):
            return math.hypot(a[0] - b[0], a[1] - b[1])
        self.assertAlmostEqual(dist(ul, ur), 180.0, places=3)
        self.assertAlmostEqual(dist(dl, dr), 180.0, places=3)
        self.assertAlmostEqual(dist(ul, dl), 400.0, places=3)
        self.assertAlmostEqual(dist(ur, dr), 400.0, places=3)
        ux, uy = ur[0] - ul[0], ur[1] - ul[1]
        vx, vy = dl[0] - ul[0], dl[1] - ul[1]
        self.assertAlmostEqual(ux * vx + uy * vy, 0.0, places=4)

    def test_centreline_forward_is_downstream(self):
        lat, lon = WELLINGTON
        coords = [[lon - 0.003, lat], [lon + 0.003, lat]]
        unit = aoi_downstream_unit_2193(lat, lon, coords)
        self.assertGreater(unit[0], 0.9)
        self.assertAlmostEqual(unit[1], 0.0, delta=0.15)
        aoi = bridge_aoi(lat, lon, upstream_m=100, downstream_m=300, lateral_m=50, centerline_coords=coords)
        west, south, east, north = aoi["bounds_2193"]
        self.assertAlmostEqual(east - west, 400.0, delta=12.0)
        self.assertLess(north - south, 120.0)
        self.assertGreater(north - south, 90.0)
        ox, _oy = aoi["origin_2193"]
        self.assertGreater(aoi["points_2193"]["downstream"][0], ox)
        self.assertLess(aoi["points_2193"]["upstream"][0], ox)

    def test_rejects_out_of_range_extents(self):
        with self.assertRaises(HydroScreenError):
            parse_aoi_extent(0, name="Upstream limit")
        with self.assertRaises(HydroScreenError):
            parse_aoi_extent(20000, name="Lateral extent")
        with self.assertRaises(HydroScreenError):
            parse_aoi_extent("wide", name="Lateral extent")


class AoiPlanTests(unittest.TestCase):
    def test_plan_includes_the_aoi_polygon_and_https_urls(self):
        plan = plan_linz_clip(*WELLINGTON, upstream_m=200, downstream_m=200, lateral_m=150)
        self.assertIn("BQ31", plan["tiles"])
        self.assertEqual(plan["aoi"]["upstream_m"], 200.0)
        self.assertGreaterEqual(len(plan["aoi"]["ring"]), 5)
        self.assertTrue(all(uri.startswith("https://") for uri in plan["urls"]))
        self.assertTrue(all("/vsicurl/" not in uri for uri in plan["uris"]))


class WindowedExtractTests(unittest.TestCase):
    def test_extract_windows_https_cogs_and_does_not_download_sheets(self):
        plan = plan_linz_clip(*WELLINGTON)
        seen = {}
        downloads = []

        def fake_download(code):
            downloads.append(code)
            yield 1.0, f"Saved {code}"

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None, geometry=None):
            seen["uris"] = list(uris)
            seen["bounds"] = bounds
            seen["geometry"] = geometry
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            Path(out_tif).write_bytes(b"AOI-CLIP" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge.tif"
            env = {
                "HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache"),
                "HYDROBRIDGE_LINZ_TILES": str(Path(tmp) / "linz-tiles"),
            }
            with patch.dict(os.environ, env):
                with patch("hydroscreen.iter_download_linz_tile", side_effect=fake_download):
                    with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                        path, used = extract_linz_dem_for_bridge(*WELLINGTON, str(out))
        self.assertEqual(path, str(out))
        self.assertEqual(downloads, [])
        self.assertEqual(used["tiles"], plan["tiles"])
        self.assertEqual(seen["uris"], [f"/vsicurl/{url}" for url in used["urls"]])
        self.assertEqual(seen["bounds"], plan["bounds_2193"])
        self.assertIsInstance(seen["geometry"], Polygon)
        self.assertAlmostEqual(seen["geometry"].area, 500.0 * 500.0, delta=1.0)
        self.assertFalse(any(Path(p).exists() for p in used["paths"]))

    def test_extract_reuses_a_local_sheet_if_already_on_disk(self):
        seen = {}

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None, geometry=None):
            seen["uris"] = list(uris)
            Path(out_tif).write_bytes(b"LOCAL-WINDOW" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            tiles = Path(tmp) / "linz-tiles"
            tiles.mkdir()
            plan = plan_linz_clip(*WELLINGTON)
            local = tiles / f"{plan['tiles'][0]}.tiff"
            local.write_bytes(b"FULL-TILE" * 40)
            env = {
                "HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache"),
                "HYDROBRIDGE_LINZ_TILES": str(tiles),
            }
            with patch.dict(os.environ, env):
                with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                    extract_linz_dem_for_bridge(*WELLINGTON, str(Path(tmp) / "out.tif"))
            self.assertTrue(any(str(local) == uri for uri in seen["uris"]))
            self.assertTrue(any(uri.startswith("/vsicurl/") for uri in seen["uris"]) or len(seen["uris"]) == 1)

    def test_cached_aoi_clip_skips_a_second_window_read(self):
        clips = []

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None, geometry=None):
            clips.append(out_tif)
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            Path(out_tif).write_bytes(b"CACHED-AOI" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache"),
                "HYDROBRIDGE_LINZ_TILES": str(Path(tmp) / "linz-tiles"),
            }
            with patch.dict(os.environ, env):
                with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                    first = Path(tmp) / "a.tif"
                    second = Path(tmp) / "b.tif"
                    extract_linz_dem_for_bridge(*WELLINGTON, str(first))
                    extract_linz_dem_for_bridge(*WELLINGTON, str(second))
            self.assertEqual(len(clips), 1)
            self.assertEqual(second.read_bytes(), first.read_bytes())

    def test_window_uris_prefer_https_when_the_sheet_is_missing(self):
        plan = plan_linz_clip(*WELLINGTON)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HYDROBRIDGE_LINZ_TILES": tmp}):
                uris = linz_window_uris(plan_linz_clip(*WELLINGTON))
        self.assertTrue(uris)
        self.assertTrue(all(uri.startswith("/vsicurl/https://") for uri in uris))
        self.assertTrue(any(plan["tiles"][0] in uri for uri in uris))


class AoiMaskTests(unittest.TestCase):
    def test_geometry_mask_clears_pixels_outside_the_rectangle(self):
        transform = from_origin(1748000.0, 5429000.0, 1.0, 1.0)
        data = np.full((80, 80), 12.5, dtype=np.float32)
        poly = Polygon(
            [
                (1748025.0, 5428945.0),
                (1748045.0, 5428945.0),
                (1748045.0, 5428965.0),
                (1748025.0, 5428965.0),
                (1748025.0, 5428945.0),
            ]
        )
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
                geometry=poly,
            )
            with rasterio.open(out_path) as src:
                arr = src.read(1)
                inside = arr == 12.5
                outside = arr == -9999.0
                self.assertTrue(np.any(inside))
                self.assertTrue(np.any(outside))
                self.assertGreater(int(outside.sum()), int(inside.sum()))


if __name__ == "__main__":
    unittest.main()
