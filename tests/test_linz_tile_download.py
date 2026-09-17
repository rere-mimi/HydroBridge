"""Full LINZ sheets are downloaded locally, then clipped to the bridge square."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from hydroscreen import (
    HydroScreenError,
    ensure_linz_tiles,
    iter_download_linz_tile,
    linz_tile_path,
    linz_tile_url,
)


class FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {"Content-Length": str(len(content))}
        self.ok = status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size):
        size = max(1, int(chunk_size) or 1)
        for start in range(0, len(self.content), size):
            yield self.content[start : start + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LinzTileDownloadTests(unittest.TestCase):
    def test_skips_a_complete_tile_already_on_disk(self):
        payload = b"COMPLETE-LINZ-TILE" * 40
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"HYDROBRIDGE_LINZ_TILES": tmp}):
                dest = linz_tile_path("BX24")
                dest.write_bytes(payload)
                with patch(
                    "hydroscreen._head_linz_tile_size", return_value=len(payload)
                ):
                    with patch("hydroscreen.requests.get") as get:
                        events = list(iter_download_linz_tile("BX24"))
        self.assertEqual(events[-1][0], 1.0)
        self.assertIn("Using downloaded", events[-1][1])
        get.assert_not_called()

    def test_resumes_a_partial_download_with_http_range(self):
        payload = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 8
        seen = {}

        def fake_get(url, stream=False, timeout=None, headers=None):
            seen["headers"] = dict(headers or {})
            start = 0
            if headers and headers.get("Range", "").startswith("bytes="):
                start = int(headers["Range"].split("=")[1].split("-")[0])
            return FakeResponse(payload[start:], status_code=206)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"HYDROBRIDGE_LINZ_TILES": tmp}):
                part = Path(str(linz_tile_path("BX24")) + ".part")
                part.write_bytes(payload[:40])
                with patch("hydroscreen._head_linz_tile_size", return_value=len(payload)):
                    with patch("hydroscreen.requests.get", side_effect=fake_get):
                        list(iter_download_linz_tile("BX24"))
                dest = linz_tile_path("BX24")
                self.assertEqual(dest.read_bytes(), payload)
                self.assertFalse(part.exists())
        self.assertEqual(seen["headers"].get("Range"), "bytes=40-")

    def test_writes_intersecting_tiles_into_the_output_folder(self):
        payload = b"SHEET" * 80

        def fake_get(url, stream=False, timeout=None, headers=None):
            return FakeResponse(payload, status_code=200)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"HYDROBRIDGE_LINZ_TILES": tmp}):
                with patch("hydroscreen._head_linz_tile_size", return_value=len(payload)):
                    with patch("hydroscreen.requests.get", side_effect=fake_get):
                        paths = ensure_linz_tiles(["BQ31", "BQ32"])
            self.assertEqual(len(paths), 2)
            for path in paths:
                self.assertTrue(Path(path).exists())
                self.assertEqual(Path(path).read_bytes(), payload)
                self.assertEqual(Path(path).parent, Path(tmp))

    def test_tile_urls_are_public_https_not_vsicurl(self):
        url = linz_tile_url("BX24")
        self.assertTrue(url.startswith("https://"))
        self.assertIn("BX24.tiff", url)
        self.assertNotIn("/vsicurl/", url)

    def test_incomplete_file_is_left_for_resume(self):
        payload = b"0123456789" * 30

        def fake_get(url, stream=False, timeout=None, headers=None):
            return FakeResponse(payload[:50], status_code=200)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"HYDROBRIDGE_LINZ_TILES": tmp}):
                with patch("hydroscreen._head_linz_tile_size", return_value=len(payload)):
                    with patch("hydroscreen.requests.get", side_effect=fake_get):
                        with self.assertRaises(HydroScreenError) as ctx:
                            list(iter_download_linz_tile("BX24"))
                dest = linz_tile_path("BX24")
                part = Path(str(dest) + ".part")
                self.assertFalse(dest.exists())
                self.assertTrue(part.exists())
        self.assertIn("incomplete", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
