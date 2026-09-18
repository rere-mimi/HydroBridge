"""Unit tests for trapezoidal stage geometry and Manning water-level solve."""

import math
import tempfile
import unittest
from pathlib import Path

import rasterio
import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import LineString

from hydroscreen import (
    connected_wet_mask,
    estimate_centerline_slope,
    hydraulics_at_stage,
    manning_discharge,
    solve_water_level,
)


class TrapezoidalHydraulicsTests(unittest.TestCase):
    def setUp(self):
        # 1:1 side slopes, 10 m bed, banks 2 m above the bed.
        self.dists = np.array([0.0, 2.0, 12.0, 14.0])
        self.elevs = np.array([2.0, 0.0, 0.0, 2.0])

    def test_area_and_radius_at_one_metre_stage(self):
        hyd = hydraulics_at_stage(self.dists, self.elevs, water_level=1.0)
        self.assertAlmostEqual(hyd["area_m2"], 11.0, places=6)
        self.assertAlmostEqual(hyd["top_width_m"], 12.0, places=6)
        expected_p = 10.0 + 2.0 * math.sqrt(2.0)
        self.assertAlmostEqual(hyd["wetted_perimeter_m"], expected_p, places=6)
        self.assertAlmostEqual(hyd["hydraulic_radius_m"], 11.0 / expected_p, places=6)

    def test_dry_when_stage_is_at_the_bed(self):
        hyd = hydraulics_at_stage(self.dists, self.elevs, water_level=0.0)
        self.assertEqual(hyd["area_m2"], 0.0)
        self.assertEqual(hyd["hydraulic_radius_m"], 0.0)

    def test_manning_si_formula(self):
        velocity, discharge = manning_discharge(11.0, 0.857, 0.03, 0.001)
        expected_v = (1.0 / 0.03) * (0.857 ** (2.0 / 3.0)) * math.sqrt(0.001)
        self.assertAlmostEqual(velocity, expected_v, places=8)
        self.assertAlmostEqual(discharge, expected_v * 11.0, places=8)

    def test_solve_raises_water_level_until_target_q(self):
        n = 0.035
        slope = 0.002
        target = 8.0
        result = solve_water_level(self.dists, self.elevs, target, n, slope)
        self.assertTrue(result["conveys"])
        self.assertGreater(result["water_level_m"], 0.0)
        self.assertLess(result["water_level_m"], 2.0)
        self.assertGreaterEqual(result["discharge_m3_s"] + 1e-4, target)
        self.assertLess(abs(result["discharge_m3_s"] - target), 0.15)

        hyd = hydraulics_at_stage(self.dists, self.elevs, result["water_level_m"])
        _, q_check = manning_discharge(
            hyd["area_m2"], hyd["hydraulic_radius_m"], n, slope
        )
        self.assertAlmostEqual(q_check, result["discharge_m3_s"], places=6)

    def test_cannot_convey_when_flow_is_too_large(self):
        result = solve_water_level(self.dists, self.elevs, 1e6, 0.035, 0.001)
        self.assertFalse(result["conveys"])
        self.assertTrue(result["overtopped"])


class CenterlineSlopeTests(unittest.TestCase):
    def test_slope_from_tilted_dem(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 20, 60
        data = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            data[:, col] = 30.0 - 0.1 * col  # 6 m drop west → east
        transform = from_origin(west, north, res, res)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slope.tif"
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
            line = LineString([
                (west + 2 * res, north - 5 * res),
                (west + (width - 3) * res, north - 5 * res),
            ])
            slope = estimate_centerline_slope(str(path), line, spacing_m=10.0)
        self.assertIsNotNone(slope)
        # ~6 m over ~470 m of ground distance around this latitude.
        self.assertGreater(slope, 0.008)
        self.assertLess(slope, 0.02)


class ConnectedWettedAreaTests(unittest.TestCase):
    def setUp(self):
        # 200 m transect centred on the river, with an isolated pit on the right.
        self.dists = np.array([0.0, 20.0, 80.0, 90.0, 100.0, 110.0, 120.0, 160.0, 180.0, 200.0])
        self.elevs = np.array([5.0, 4.0, 3.0, 0.0, 0.0, 0.0, 3.0, 4.0, 1.0, 5.0])
        self.channel_dists = np.array([80.0, 90.0, 100.0, 110.0, 120.0])
        self.channel_elevs = np.array([3.0, 0.0, 0.0, 0.0, 3.0])
        self.pit_dists = np.array([160.0, 180.0, 200.0])
        self.pit_elevs = np.array([4.0, 1.0, 5.0])

    def test_disconnected_pit_is_excluded_at_low_stage(self):
        hyd = hydraulics_at_stage(self.dists, self.elevs, 2.0)
        channel = hydraulics_at_stage(self.channel_dists, self.channel_elevs, 2.0)
        pit = hydraulics_at_stage(self.pit_dists, self.pit_elevs, 2.0)
        self.assertGreater(pit["area_m2"], 1.0)
        self.assertAlmostEqual(hyd["area_m2"], channel["area_m2"], places=5)
        self.assertAlmostEqual(hyd["wetted_perimeter_m"], channel["wetted_perimeter_m"], places=5)
        self.assertAlmostEqual(hyd["top_width_m"], channel["top_width_m"], places=5)
        self.assertAlmostEqual(hyd["hydraulic_radius_m"], channel["hydraulic_radius_m"], places=5)
        self.assertEqual(len(hyd["connected_spans"]), 1)
        self.assertLess(hyd["connected_spans"][0][1], 160.0)
        mask = connected_wet_mask(self.dists, self.elevs, 2.0)
        self.assertTrue(bool(mask[4]))
        self.assertFalse(bool(mask[8]))

    def test_pit_joins_once_the_ridge_is_overtopped(self):
        hyd = hydraulics_at_stage(self.dists, self.elevs, 4.5)
        channel = hydraulics_at_stage(self.channel_dists, self.channel_elevs, 4.5)
        self.assertGreater(hyd["area_m2"], channel["area_m2"] + 1.0)
        self.assertGreater(hyd["connected_spans"][0][1], 160.0)
        mask = connected_wet_mask(self.dists, self.elevs, 4.5)
        self.assertTrue(bool(mask[8]))

    def test_max_depth_uses_the_channel_bed_not_a_deeper_pit(self):
        elevs = self.elevs.copy()
        elevs[8] = -3.0
        result = solve_water_level(self.dists, elevs, 5.0, 0.035, 0.002)
        self.assertTrue(result["conveys"])
        self.assertAlmostEqual(result["max_depth_m"], result["water_level_m"], delta=0.15)
        self.assertLess(result["connected_spans"][0][1], 160.0)

    def test_solve_matches_the_channel_only_section_while_the_pit_is_dry(self):
        n = 0.035
        slope = 0.002
        target = 8.0
        full = solve_water_level(self.dists, self.elevs, target, n, slope)
        channel = solve_water_level(self.channel_dists, self.channel_elevs, target, n, slope)
        self.assertLess(full["water_level_m"], 4.0)
        self.assertAlmostEqual(full["water_level_m"], channel["water_level_m"], places=3)
        self.assertAlmostEqual(full["area_m2"], channel["area_m2"], places=3)
        self.assertAlmostEqual(full["discharge_m3_s"], channel["discharge_m3_s"], places=3)


if __name__ == "__main__":
    unittest.main()
