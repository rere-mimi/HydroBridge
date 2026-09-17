"""Drawn centreline length is the analysis reach, plus 50 m at each end."""

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString
from pyproj import Transformer

from hydroscreen import (
    CENTERLINE_END_BUFFER_M,
    apply_centerline_end_buffers,
    extend_centerline_ends,
    generate_transects,
    projected_length_m,
    run_screening,
)


WELLINGTON = (-41.2865, 174.7762)


class ExtendCenterlineEndsTests(unittest.TestCase):
    def test_extends_50m_along_a_diagonal_channel_not_map_axes(self):
        lat, lon = WELLINGTON
        coords = [[lon - 0.002, lat - 0.001], [lon + 0.002, lat + 0.001]]
        extended = extend_centerline_ends(coords)
        self.assertEqual(len(extended), 4)
        to_2193 = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
        p0 = to_2193.transform(coords[0][0], coords[0][1])
        p1 = to_2193.transform(coords[1][0], coords[1][1])
        up = to_2193.transform(extended[0][0], extended[0][1])
        down = to_2193.transform(extended[-1][0], extended[-1][1])
        self.assertAlmostEqual(math.hypot(up[0] - p0[0], up[1] - p0[1]), CENTERLINE_END_BUFFER_M, delta=0.05)
        self.assertAlmostEqual(math.hypot(down[0] - p1[0], down[1] - p1[1]), CENTERLINE_END_BUFFER_M, delta=0.05)
        seg = (p1[0] - p0[0], p1[1] - p0[1])
        ext_up = (up[0] - p0[0], up[1] - p0[1])
        mag_s = math.hypot(*seg)
        mag_e = math.hypot(*ext_up)
        self.assertAlmostEqual((ext_up[0] * seg[0] + ext_up[1] * seg[1]) / (mag_s * mag_e), -1.0, places=5)
        self.assertGreater(abs(up[0] - p0[0]), 15)
        self.assertGreater(abs(up[1] - p0[1]), 15)

    def test_northbound_line_extends_south_of_the_first_point(self):
        lat, lon = WELLINGTON
        to_2193 = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
        to_4326 = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
        ox, oy = to_2193.transform(lon, lat)
        coords = [
            list(to_4326.transform(ox, oy - 100.0)),
            list(to_4326.transform(ox, oy + 100.0)),
        ]
        extended = extend_centerline_ends(coords)
        p0 = to_2193.transform(coords[0][0], coords[0][1])
        p1 = to_2193.transform(coords[1][0], coords[1][1])
        up = to_2193.transform(extended[0][0], extended[0][1])
        down = to_2193.transform(extended[-1][0], extended[-1][1])
        self.assertAlmostEqual(up[0], p0[0], delta=0.05)
        self.assertLess(up[1], p0[1])
        self.assertAlmostEqual(abs(up[1] - p0[1]), CENTERLINE_END_BUFFER_M, delta=0.05)
        self.assertAlmostEqual(down[0], p1[0], delta=0.05)
        self.assertGreater(down[1], p1[1])
        self.assertAlmostEqual(abs(down[1] - p1[1]), CENTERLINE_END_BUFFER_M, delta=0.05)

    def test_grows_short_aoi_limits_and_keeps_larger_user_limits(self):
        lat, lon = WELLINGTON
        coords = [[lon - 0.004, lat], [lon + 0.004, lat]]
        _extended, upstream_m, downstream_m = apply_centerline_end_buffers(
            lat, lon, coords, upstream_m=80, downstream_m=80
        )
        self.assertGreater(upstream_m, 300)
        self.assertGreater(downstream_m, 300)
        _extended, upstream_m, downstream_m = apply_centerline_end_buffers(
            lat, lon, coords, upstream_m=2000, downstream_m=1800
        )
        self.assertAlmostEqual(upstream_m, 2000.0)
        self.assertAlmostEqual(downstream_m, 1800.0)


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
        height, width = 60, 160
        grid = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            grid[:, col] = 30.0 - 0.1 * col
        grid[height // 2, :] -= 4.0
        transform = from_origin(west, north, res, res)
        lon = west + (width / 2) * res
        lat = north - (height / 2) * res
        centerline = [
            [west + 20 * res, lat],
            [west + (width - 20) * res, lat],
        ]
        expected_drawn = projected_length_m(LineString(centerline))
        expected = projected_length_m(LineString(extend_centerline_ends(centerline)))
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
        self.assertAlmostEqual(result["layout"]["along_m"], expected, delta=2.0)
        self.assertGreater(result["layout"]["along_m"], expected_drawn + 80)
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
        self.assertIn("slope", profile)
        self.assertGreater(profile["slope"], 0)
        self.assertIn("aris", first)
        self.assertIn("100y", first["aris"])


if __name__ == "__main__":
    unittest.main()
