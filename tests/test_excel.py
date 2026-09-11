"""Tests for summary.xlsx DATA sheet of transect sample points."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from hydroscreen import DATA_SHEET_COLUMNS, run_screening, write_screening_workbook


class ScreeningWorkbookTests(unittest.TestCase):
    def test_data_sheet_has_id_transect_distance_elevation(self):
        rows = [
            {"ID": 1, "Transect ID": 1, "Distance_m": 0.0, "Elevation_m": 10.5},
            {"ID": 2, "Transect ID": 1, "Distance_m": 5.0, "Elevation_m": 9.2},
            {"ID": 3, "Transect ID": 2, "Distance_m": 0.0, "Elevation_m": 11.0},
        ]
        summary = [{"transect": 1, "n_samples": 2}, {"transect": 2, "n_samples": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.xlsx"
            write_screening_workbook(path, summary, rows)
            with pd.ExcelFile(path) as workbook:
                self.assertEqual(workbook.sheet_names, ["SUMMARY", "DATA"])
            data = pd.read_excel(path, sheet_name="DATA")
            self.assertEqual(list(data.columns), DATA_SHEET_COLUMNS)
            self.assertEqual(data["ID"].tolist(), [1, 2, 3])
            self.assertEqual(data["Transect ID"].tolist(), [1, 1, 2])
            self.assertEqual(data["Distance_m"].tolist(), [0.0, 5.0, 0.0])
            self.assertAlmostEqual(float(data["Elevation_m"].iloc[0]), 10.5)

    def test_screening_writes_every_sample_point(self):
        west, north = 174.770, -41.280
        res = 0.0001
        height, width = 40, 80
        grid = np.zeros((height, width), dtype=np.float32)
        for col in range(width):
            grid[:, col] = 30.0 - 0.1 * col
        mid = height // 2
        grid[mid, :] -= 4.0
        transform = from_origin(west, north, res, res)
        lon = west + (width / 2) * res
        lat = north - (height / 2) * res
        centerline = [
            [west + 5 * res, lat],
            [west + (width - 5) * res, lat],
        ]
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
                interval=40.0,
                along_m=80.0,
                length=40.0,
                sample_spacing=10.0,
                centerline_coords=centerline,
                flow_m3_s=1.0,
                mannings_n=0.035,
            )
            xlsx = Path(result["outdir"]) / result["summary_xlsx"]
            data = pd.read_excel(xlsx, sheet_name="DATA")
            expected = sum(row["n_samples"] for row in result["summary"])
            self.assertGreater(expected, 0)
            self.assertEqual(len(data), expected)
            self.assertEqual(data["ID"].tolist(), list(range(1, expected + 1)))
            self.assertTrue(set(data["Transect ID"]).issubset({row["transect"] for row in result["summary"]}))
            self.assertTrue((data["Distance_m"] >= 0).all())


if __name__ == "__main__":
    unittest.main()
