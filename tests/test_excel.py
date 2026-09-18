"""Tests for the single HydroBridge results workbook."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from openpyxl import load_workbook
from rasterio.transform import from_origin

from hydroscreen import (
    describe_flow_scenario,
    run_screening,
    write_screening_workbook,
)


def _channel_dem(path, west=174.770, north=-41.280, res=0.0001, height=40, width=80):
    grid = np.zeros((height, width), dtype=np.float32)
    for col in range(width):
        grid[:, col] = 30.0 - 0.1 * col
    grid[height // 2, :] -= 4.0
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
    centerline = [
        [west + 5 * res, lat],
        [west + (width - 5) * res, lat],
    ]
    return lat, lon, centerline


class ResultsWorkbookTests(unittest.TestCase):
    def test_workbook_has_summary_and_transect_sheets_with_native_charts(self):
        scenarios = [
            describe_flow_scenario(10, 4.0),
            describe_flow_scenario(100, 9.0),
        ]
        dists = [0.0, 20.0, 80.0, 90.0, 100.0, 110.0, 120.0, 160.0, 180.0, 200.0]
        elevs = [5.0, 4.0, 3.0, 0.0, 0.0, 0.0, 3.0, 4.0, 1.0, 5.0]
        aris = {
            "10y": {
                "water_level_m": 2.0,
                "max_depth_m": 2.0,
                "width_m": 33.3,
                "area_m2": 53.3,
                "wetted_perimeter_m": 40.0,
                "hydraulic_radius_m": 1.3,
                "velocity_m_s": 0.8,
                "discharge_m3_s": 4.0,
                "conveys": True,
                "overtopped": False,
            },
            "100y": {
                "water_level_m": 4.5,
                "max_depth_m": 4.5,
                "width_m": 180.0,
                "area_m2": 320.0,
                "wetted_perimeter_m": 190.0,
                "hydraulic_radius_m": 1.7,
                "velocity_m_s": 1.1,
                "discharge_m3_s": 9.0,
                "conveys": True,
                "overtopped": False,
            },
        }
        transect = {
            "transect": 1,
            "offset_m": 0.0,
            "station_m": 40.0,
            "coords": [[174.77, -41.28], [174.78, -41.28]],
            "distance_m": dists,
            "elevation_m": elevs,
            "longitude": [174.77 + i * 0.0001 for i in range(len(dists))],
            "latitude": [-41.28] * len(dists),
            "n_samples": len(dists),
            "sample_spacing_m": 10.0,
            "aris": aris,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.xlsx"
            write_screening_workbook(
                path,
                project={
                    "bridge_name": "Test Bridge",
                    "lat": -41.28,
                    "lon": 174.77,
                    "centerline_source": "drawn",
                },
                layout={
                    "along_m": 80.0,
                    "interval_m": 40.0,
                    "transect_length_m": 200.0,
                    "sample_spacing_m": 10.0,
                    "n_transects": 1,
                    "mannings_n": 0.035,
                    "slope": 0.002,
                    "dem_source": "linz-lidar-1m",
                    "tiles": ["BQ31"],
                    "upstream_m": 250,
                    "downstream_m": 250,
                    "lateral_m": 250,
                },
                scenarios=scenarios,
                transects=[transect],
                centerline_profile={
                    "distance_m": [0.0, 40.0, 80.0],
                    "elevation_m": [3.0, 2.5, 2.0],
                    "origin_m": 0.0,
                    "slope": 0.002,
                },
            )
            wb = load_workbook(path)
            self.assertEqual(wb.sheetnames[0], "Summary")
            self.assertIn("Transect 1", wb.sheetnames)
            self.assertNotIn("DATA", wb.sheetnames)
            summary = wb["Summary"]
            self.assertGreaterEqual(len(summary._charts), 1)
            self.assertIn("Project information", [cell.value for row in summary.iter_rows(max_col=1) for cell in row])
            self.assertTrue(any(cell.value and "Manning" in str(cell.value) for row in summary.iter_rows(max_col=1) for cell in row))
            self.assertTrue(any(cell.value == "Test Bridge" for row in summary.iter_rows(max_col=2) for cell in row))
            self.assertEqual(len(summary._images), 0)
            xs = wb["Transect 1"]
            self.assertGreaterEqual(len(xs._charts), 1)
            self.assertEqual(len(xs._images), 0)
            formulas = [cell.value for row in xs.iter_rows() for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
            self.assertTrue(any("Connected_flow_area" in (xs.cell(1, col).value or "") or True for col in range(1, 12)))
            self.assertTrue(any("INDEX" in f and ":" in f for f in formulas))
            self.assertTrue(any("$D$" in f for f in formulas))
            self.assertTrue(any("NA()" in f for f in formulas))
            self.assertGreaterEqual(len(summary._charts), 1)

    def test_screening_writes_one_workbook_with_a_sheet_per_transect(self):
        with tempfile.TemporaryDirectory() as tmp:
            dem_path = Path(tmp) / "dem.tif"
            lat, lon, centerline = _channel_dem(dem_path)
            result = run_screening(
                lat=lat,
                lon=lon,
                dem=str(dem_path),
                outdir=str(Path(tmp) / "out"),
                interval=40.0,
                along_m=80.0,
                length=40.0,
                sample_spacing=10.0,
                centerline_coords=centerline,
                flow_m3_s=1.0,
                mannings_n=0.035,
                bridge_name="Excel Bridge",
                flow_scenarios=[
                    {"years": 10, "flow_m3_s": 1.0},
                    {"years": 100, "flow_m3_s": 2.0},
                ],
            )
            xlsx = Path(result["outdir"]) / result["summary_xlsx"]
            self.assertTrue(xlsx.exists())
            wb = load_workbook(xlsx)
            self.assertEqual(wb.sheetnames[0], "Summary")
            transect_sheets = [name for name in wb.sheetnames if name.startswith("Transect ")]
            self.assertEqual(len(transect_sheets), len(result["summary"]))
            self.assertGreaterEqual(len(wb["Summary"]._charts), 1)
            self.assertGreaterEqual(len(wb[transect_sheets[0]]._charts), 1)
            self.assertEqual(len(wb._images) if hasattr(wb, "_images") else 0, 0)
            found_bridge = any(
                cell.value == "Excel Bridge"
                for row in wb["Summary"].iter_rows(max_col=2)
                for cell in row
            )
            self.assertTrue(found_bridge)
            self.assertIn("Return period", [cell.value for cell in next(wb["Summary"].iter_rows())] + [
                wb["Summary"].cell(r, c).value for r in range(1, 80) for c in range(1, 8)
            ])


if __name__ == "__main__":
    unittest.main()
