"""Methodology: pick LINZ 1 m COG tiles for the chosen bridge pin."""

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

        def fake_clip(uris, bounds, out_tif, nodata=-9999.0, resolution=None, progress=None):
            seen["uris"] = list(uris)
            seen["bounds"] = bounds
            Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
            Path(out_tif).write_bytes(b"LINZ-CLIP" * 40)
            return out_tif

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge.tif"
            with patch.dict(os.environ, {"HYDROBRIDGE_DEM_CACHE": str(Path(tmp) / "cache")}):
                with patch("hydroscreen.clip_dem_tiles", side_effect=fake_clip):
                    path, used = extract_linz_dem_for_bridge(-41.2865, 174.7762, str(out))
            self.assertEqual(path, str(out))
            self.assertEqual(used["tiles"], plan["tiles"])
            self.assertEqual(seen["uris"], plan["uris"])
            self.assertEqual(seen["bounds"], plan["bounds_2193"])
            self.assertGreater(Path(path).stat().st_size, 256)

    def test_extract_rejects_a_pin_outside_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HydroScreenError) as ctx:
                extract_linz_dem_for_bridge(-20.0, 160.0, str(Path(tmp) / "out.tif"))
        self.assertIn("outside", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
