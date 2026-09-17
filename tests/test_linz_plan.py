"""Methodology: pick LINZ 1 m tiles, download them, then clip locally."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hydroscreen import (
    HydroScreenError,
    extract_linz_dem_for_bridge,
    plan_linz_clip,
    square_clip_2193,
)


class PlanLinzClipTests(unittest.TestCase):
    def test_wellington_pin_selects_bq31_and_a_500m_square(self):
        plan = plan_linz_clip(-41.2865, 174.7762)
        self.assertIn("BQ31", plan["tiles"])
        self.assertEqual(plan["clip_size_m"], 500.0)
        west, south, east, north = plan["bounds_2193"]
        self.assertAlmostEqual(east - west, 500.0, places=5)
        self.assertAlmostEqual(north - south, 500.0, places=5)
        self.assertEqual(plan["bounds_2193"], square_clip_2193(-41.2865, 174.7762))
        self.assertTrue(plan["uris"])
        self.assertTrue(all(uri.endswith(".tiff") for uri in plan["uris"]))
        self.assertTrue(any("BQ31.tiff" in uri for uri in plan["uris"]))
        self.assertTrue(all(uri.startswith("https://") for uri in plan["uris"]))
        self.assertTrue(all("/vsicurl/" not in uri for uri in plan["uris"]))
        self.assertTrue(any(path.endswith("BQ31.tiff") for path in plan["paths"]))
        self.assertIn("linz-tiles", plan["tile_dir"])

    def test_christchurch_pin_selects_bx24(self):
        plan = plan_linz_clip(-43.532, 172.6362)
        self.assertIn("BX24", plan["tiles"])
        self.assertTrue(all(f"{code}.tiff" in "".join(plan["uris"]) for code in plan["tiles"]))

    def test_sheet_boundary_keeps_every_intersecting_tile(self):
        # Overlap of BQ31 (east) and BQ32 (west).
        plan = plan_linz_clip(-41.32, 174.864)
        self.assertGreaterEqual(len(plan["tiles"]), 2)
        self.assertTrue({"BQ31", "BQ32"}.issubset(set(plan["tiles"])))

    def test_ocean_pin_has_no_tiles(self):
        plan = plan_linz_clip(-20.0, 160.0)
        self.assertEqual(plan["tiles"], [])
        self.assertEqual(plan["uris"], [])

    def test_extract_opens_only_the_planned_sheets(self):
        plan = plan_linz_clip(-41.2865, 174.7762)
        seen = {}

        def fake_download(code):
            dest = Path(os.environ["HYDROBRIDGE_LINZ_TILES"]) / f"{code}.tiff"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FULL-TILE" * 40)
            yield 1.0, f"Saved {code}"

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None):
            seen["uris"] = list(uris)
            seen["bounds"] = bounds
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            Path(out_tif).write_bytes(b"LINZ-CLIP" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge.tif"
            tiles = str(Path(tmp) / "linz-tiles")
            cache = str(Path(tmp) / "cache")
            with patch.dict(
                os.environ,
                {"HYDROBRIDGE_DEM_CACHE": cache, "HYDROBRIDGE_LINZ_TILES": tiles},
            ):
                with patch("hydroscreen.iter_download_linz_tile", side_effect=fake_download):
                    with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                        path, used = extract_linz_dem_for_bridge(-41.2865, 174.7762, str(out))
            self.assertEqual(path, str(out))
            self.assertEqual(used["tiles"], plan["tiles"])
            self.assertEqual(seen["uris"], used["paths"])
            self.assertEqual(seen["bounds"], plan["bounds_2193"])
            self.assertGreater(Path(path).stat().st_size, 256)
            self.assertTrue(all(Path(p).exists() for p in used["paths"]))

    def test_extract_still_downloads_tiles_when_the_clip_is_cached(self):
        downloads = []

        def fake_download(code):
            downloads.append(code)
            dest = Path(os.environ["HYDROBRIDGE_LINZ_TILES"]) / f"{code}.tiff"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FULL-TILE" * 40)
            yield 1.0, f"Saved {code}"

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None):
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            Path(out_tif).write_bytes(b"LINZ-CLIP" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache"),
                "HYDROBRIDGE_LINZ_TILES": str(Path(tmp) / "linz-tiles"),
            }
            with patch.dict(os.environ, env):
                with patch("hydroscreen.iter_download_linz_tile", side_effect=fake_download):
                    with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                        first = Path(tmp) / "a.tif"
                        second = Path(tmp) / "b.tif"
                        extract_linz_dem_for_bridge(-41.2865, 174.7762, str(first))
                        extract_linz_dem_for_bridge(-41.2865, 174.7762, str(second))
            self.assertGreaterEqual(len(downloads), 2)
            self.assertTrue(second.exists())
            self.assertEqual(second.read_bytes(), first.read_bytes())

    def test_extract_rejects_a_pin_outside_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HydroScreenError) as ctx:
                extract_linz_dem_for_bridge(-20.0, 160.0, str(Path(tmp) / "out.tif"))
        self.assertIn("outside", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
