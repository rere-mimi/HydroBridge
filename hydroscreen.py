"""
Hydraulic screening MVP
- Accepts lat/lon or bridge ID (lat/lon for MVP)
- Clips the New Zealand LiDAR 1m DEM (LINZ layer 121859) around the site
- Falls back to a local DEM if provided with --dem
- Queries OSM (Overpass) for nearby waterway centreline
- Generates transects and samples DEM elevations
- Exports CSV/Excel and plots
- Estimates bed slope from the river centreline, then raises water level on
  each transect (trapezoidal area / hydraulic radius) until Manning Q matches
  the specified flow

Usage examples are in README.md
"""

import argparse
import functools
import io
import json
import os
import sys
import math
import tempfile
import shutil
import logging
import threading
from pathlib import Path

import requests
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import Point, LineString, box
from shapely.ops import split, nearest_points, substring
from pyproj import Transformer

try:
    import rasterio
    from rasterio.merge import merge as raster_merge
    from rasterio.transform import from_origin, from_bounds
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.io import MemoryFile
except Exception as e:
    print("rasterio required. Install with: pip install rasterio")
    raise

from matplotlib.colors import LightSource
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ROOT = Path(__file__).resolve().parent
LINZ_LIDAR_1M_LAYER = "https://data.linz.govt.nz/layer/121859-new-zealand-lidar-1m-dem/"
LINZ_LIDAR_1M_BASE = (
    "https://nz-elevation.s3-ap-southeast-2.amazonaws.com/new-zealand/new-zealand/dem_1m/2193/"
)
LINZ_LIDAR_1M_INDEX = ROOT / "data" / "linz_dem_1m_index.json"
LINZ_TILE_CHUNK = 1024 * 1024
LINZ_HTTP_HEADERS = {"User-Agent": "HydroBridge/0.1"}
LINZ_HTTP_TIMEOUT = (20, 120)
DEM_CLIP_SIZE_M = 500.0
DEM_CLIP_SNAP_M = 20.0
MAX_DEM_RADIUS_M = DEM_CLIP_SIZE_M / 2.0
DEM_PREVIEW_MAX_PX = 640
DEM_PREVIEW_MIN_RADIUS_M = DEM_CLIP_SIZE_M / 2.0
DEM_CACHE_MAX_FILES = 12
_CACHE_LOCK = threading.Lock()
_PLOT_LOCK = threading.Lock()
_PLOT_FIG = None
_PLOT_AX = None


class HydroScreenError(Exception):
    """Raised when screening cannot run (missing DEM, invalid inputs, etc.)."""


class HydroScreenCancelled(HydroScreenError):
    """Raised when the user stops a screening run."""


def check_cancelled(cancel_event):
    """Raise HydroScreenCancelled if the caller asked to stop."""
    if cancel_event is not None and cancel_event.is_set():
        raise HydroScreenCancelled("Screening stopped.")


def _run_interruptibly(fn, cancel_event):
    """Run a blocking HTTP call so Stop can return before it finishes.

    Do not use this for GDAL/vsicurl DEM clips. Those must stay on the
    Flask request thread; a daemon worker can hang forever on local networks.
    """
    if cancel_event is None:
        return fn()
    check_cancelled(cancel_event)
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    while worker_thread.is_alive():
        check_cancelled(cancel_event)
        worker_thread.join(0.2)
    if "error" in box:
        raise box["error"]
    return box.get("value")


@functools.lru_cache(maxsize=16)
def _transformer(src_crs, dst_crs):
    """Cached pyproj transformer. CRS arguments must be strings."""
    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def dem_cache_dir():
    override = os.environ.get("HYDROBRIDGE_DEM_CACHE")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "hydrobridge-dem-cache"


def linz_tile_dir():
    """Folder for full LINZ Topo50 GeoTIFFs (outputs/linz-tiles by default)."""
    override = os.environ.get("HYDROBRIDGE_LINZ_TILES")
    if override:
        return Path(override)
    return ROOT / "outputs" / "linz-tiles"


def linz_tile_url(code):
    return f"{LINZ_LIDAR_1M_BASE}{code}.tiff"


def linz_tile_path(code):
    return linz_tile_dir() / f"{code}.tiff"


def expand_bbox_for_cache(bbox, step=0.005):
    """Snap a lon/lat bbox outward so nearby requests share one LiDAR clip."""
    minx, miny, maxx, maxy = (float(v) for v in bbox)
    inv = round(1.0 / float(step))

    def snap_down(value):
        return math.floor(value * inv + 1e-9) / inv

    def snap_up(value):
        return math.ceil(value * inv - 1e-9) / inv

    return (snap_down(minx), snap_down(miny), snap_up(maxx), snap_up(maxy))


def _cache_file_for(bbox, resolution, bounds_2193=None):
    res = 0 if resolution is None else round(float(resolution), 2)
    if bounds_2193 is not None:
        west, south, east, north = (round(float(v), 1) for v in bounds_2193)
        name = f"nztm_{west:.1f}_{south:.1f}_{east:.1f}_{north:.1f}_{res:g}.tif"
        return dem_cache_dir() / name
    minx, miny, maxx, maxy = expand_bbox_for_cache(bbox)
    name = f"{minx:.5f}_{miny:.5f}_{maxx:.5f}_{maxy:.5f}_{res:g}.tif"
    return dem_cache_dir() / name


def _prune_dem_cache():
    folder = dem_cache_dir()
    if not folder.exists():
        return
    files = sorted(folder.glob("*.tif"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[DEM_CACHE_MAX_FILES:]:
        try:
            stale.unlink()
        except OSError:
            pass


# Utilities

def bbox_from_point(lat, lon, buffer_m):
    """Return bbox (minLon, minLat, maxLon, maxLat) in EPSG:4326 by buffering in meters using WebMercator."""
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")
    x, y = transformer_to_3857.transform(lon, lat)
    minx = x - buffer_m
    maxx = x + buffer_m
    miny = y - buffer_m
    maxy = y + buffer_m
    lon_min, lat_min = transformer_to_4326.transform(minx, miny)
    lon_max, lat_max = transformer_to_4326.transform(maxx, maxy)
    return min(lon_min, lon_max), min(lat_min, lat_max), max(lon_min, lon_max), max(lat_min, lat_max)


def square_clip_2193(lat, lon, size_m=None, snap_m=None):
    """Axis-aligned size_m × size_m window in NZTM (EPSG:2193), centred on the pin.

    The pin is snapped to snap_m so nearby clicks reuse one cached LiDAR clip.
    """
    size_m = DEM_CLIP_SIZE_M if size_m is None else float(size_m)
    snap_m = DEM_CLIP_SNAP_M if snap_m is None else float(snap_m)
    if size_m <= 0:
        raise HydroScreenError("DEM clip size must be greater than 0.")
    x, y = _transformer("EPSG:4326", "EPSG:2193").transform(float(lon), float(lat))
    if snap_m > 0:
        x = round(x / snap_m) * snap_m
        y = round(y / snap_m) * snap_m
    half = size_m / 2.0
    return (x - half, y - half, x + half, y + half)


def bbox_4326_from_2193(bounds_2193):
    """Geographic envelope of an NZTM window (minLon, minLat, maxLon, maxLat)."""
    west, south, east, north = (float(v) for v in bounds_2193)
    to_4326 = _transformer("EPSG:2193", "EPSG:4326")
    corners = [
        to_4326.transform(west, south),
        to_4326.transform(west, north),
        to_4326.transform(east, south),
        to_4326.transform(east, north),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    return (min(lons), min(lats), max(lons), max(lats))


def square_clip_bbox_4326(lat, lon, size_m=None, snap_m=None):
    """WGS84 envelope of the NZTM square clip around the pin."""
    return bbox_4326_from_2193(square_clip_2193(lat, lon, size_m=size_m, snap_m=snap_m))

# DEM acquisition

def download_wcs_getcoverage(wcs_base, layer, bbox, out_tif, crs="EPSG:4326", width=1024, height=1024):
    """Try a conservative WCS GetCoverage request. This is generic and may need tuning per server.
    bbox is (minLon,minLat,maxLon,maxLat) in the WCS CRS (assumed 4326 here).
    Returns True on success and writes out_tif.
    """
    params = {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "coverageId": layer,
        # format and subsets: many WCS servers accept subset param syntax
        # This request attempts both common patterns
    }

    minx, miny, maxx, maxy = bbox
    # Try common subset style
    # Build URL variants
    variants = []
    # 1) subset=Long(,,) style and format=image/tiff
    variants.append(({
        **params,
        "format": "image/tiff",
        "subset": [f"Long({minx},{maxx})", f"Lat({miny},{maxy})"],
        "size": [f"Long({width})", f"Lat({height})"]
    }, None))
    # 2) bounding box style using bbox & crs
    variants.append(({
        **params,
        "format": "image/tiff",
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "bbox-crs": crs,
        "size": f"{width},{height}"
    }, None))

    headers = {"User-Agent": "hydroscreen/0.1 (+https://example)"}

    for p, _ in variants:
        try:
            logging.info("Trying WCS GetCoverage with params: %s", {k: v for k, v in p.items() if k != 'subset'})
            # Overly generic attempt: use GET
            r = requests.get(wcs_base, params=p, headers=headers, stream=True, timeout=60)
            if r.status_code == 200 and r.headers.get("Content-Type", "").lower().startswith("image/tiff"):
                with open(out_tif, "wb") as fh:
                    for chunk in r.iter_content(8192):
                        fh.write(chunk)
                logging.info("WCS GetCoverage saved to %s", out_tif)
                return True
            else:
                logging.warning("WCS attempt returned status %s content-type=%s", r.status_code, r.headers.get("Content-Type"))
        except Exception as e:
            logging.warning("WCS attempt failed: %s", e)
    return False


def _raster_covers_bbox(dem_path, bbox):
    """Check if the raster at dem_path fully covers bbox (minLon,minLat,maxLon,maxLat)"""
    try:
        with rasterio.open(dem_path) as src:
            transformer = _transformer("EPSG:4326", str(src.crs))
            minx, miny, maxx, maxy = bbox
            x1, y1 = transformer.transform(minx, miny)
            x2, y2 = transformer.transform(maxx, maxy)
            bx_min, bx_max = min(x1, x2), max(x1, x2)
            by_min, by_max = min(y1, y2), max(y1, y2)
            rminx, rminy, rmaxx, rmaxy = src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top
            # Note: rasterio bounds are (left, bottom, right, top)
            covers = (bx_min >= rminx and bx_max <= rmaxx and by_min >= rminy and by_max <= rmaxy)
            return covers
    except Exception as e:
        logging.warning("Failed to check raster coverage: %s", e)
        return False


def _configure_gdal_http():
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
    os.environ.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "20")
    os.environ.setdefault("GDAL_HTTP_VERSION", "1.1")
    os.environ.setdefault("GDAL_CACHEMAX", "256")
    os.environ.setdefault("GDAL_INGESTED_BYTES_AT_OPEN", "65536")
    os.environ.setdefault("GDAL_HTTP_USERAGENT", "HydroBridge/0.1")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.TIF,.TIFF")
    os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("VSI_CACHE_SIZE", "67108864")


def load_linz_dem_1m_index():
    """Sheet id → geographic bbox for the national 1 m LiDAR DEM tiles."""
    if not LINZ_LIDAR_1M_INDEX.exists():
        raise HydroScreenError("Bundled LINZ 1m DEM tile index is missing.")
    data = json.loads(LINZ_LIDAR_1M_INDEX.read_text())
    tiles = data.get("tiles") or {}
    if not tiles:
        raise HydroScreenError("LINZ 1m DEM tile index is empty.")
    return tiles


def linz_tiles_for_bbox(bbox, tiles=None):
    """Return Topo50 sheet ids whose geographic bbox intersects bbox (minLon,minLat,maxLon,maxLat)."""
    minx, miny, maxx, maxy = bbox
    tiles = tiles if tiles is not None else load_linz_dem_1m_index()
    hits = []
    for code, tbbox in tiles.items():
        if len(tbbox) != 4:
            continue
        tminx, tminy, tmaxx, tmaxy = tbbox
        if tmaxx < minx or tminx > maxx or tmaxy < miny or tminy > maxy:
            continue
        hits.append(code)
    return sorted(hits)


def plan_linz_clip(lat, lon, size_m=None, snap_m=None):
    """Choose LINZ 1 m tiles for the 500 m square around a bridge pin.

    Methodology:
    1. Project the pin to NZTM (EPSG:2193) and snap it so nearby clicks share a window.
    2. Build a fixed 500 m × 500 m square in NZTM centred on that snap.
    3. Convert the square to a WGS84 envelope.
    4. Intersect that envelope with the bundled Topo50 index for LINZ layer 121859.
    5. Return those sheet codes, public HTTPS URLs, and local download paths.

    Full intersecting GeoTIFFs are downloaded into outputs/linz-tiles/. The 500 m
    bridge window is clipped from those local files, not cropped over HTTP.
    """
    bounds_2193 = square_clip_2193(lat, lon, size_m=size_m, snap_m=snap_m)
    bbox_4326 = bbox_4326_from_2193(bounds_2193)
    tiles = linz_tiles_for_bbox(bbox_4326)
    urls = [linz_tile_url(code) for code in tiles]
    paths = [str(linz_tile_path(code)) for code in tiles]
    clip_size = float(DEM_CLIP_SIZE_M if size_m is None else size_m)
    return {
        "lat": float(lat),
        "lon": float(lon),
        "clip_size_m": clip_size,
        "bounds_2193": tuple(float(v) for v in bounds_2193),
        "bbox_4326": tuple(float(v) for v in bbox_4326),
        "tiles": tiles,
        "urls": urls,
        "uris": urls,
        "paths": paths,
        "tile_dir": str(linz_tile_dir()),
        "layer": LINZ_LIDAR_1M_LAYER,
        "base_url": LINZ_LIDAR_1M_BASE,
    }


def _head_linz_tile_size(url):
    try:
        response = requests.head(
            url,
            timeout=LINZ_HTTP_TIMEOUT,
            allow_redirects=True,
            headers=LINZ_HTTP_HEADERS,
        )
        if not response.ok:
            return None
        length = response.headers.get("Content-Length")
        return int(length) if length else None
    except (TypeError, ValueError, requests.RequestException):
        return None


def iter_download_linz_tile(code):
    """Download one full Topo50 GeoTIFF into linz_tile_dir(). Yields (fraction, message)."""
    dest = linz_tile_path(code)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = linz_tile_url(code)
    part = Path(str(dest) + ".part")
    expected = _head_linz_tile_size(url)
    if dest.exists() and dest.stat().st_size > 256:
        if expected is None or dest.stat().st_size == expected:
            yield 1.0, f"Using downloaded {code}"
            return
        logging.info("Replacing incomplete LINZ tile %s", dest.name)
        try:
            dest.unlink()
        except OSError:
            pass

    existing = part.stat().st_size if part.exists() else 0
    headers = dict(LINZ_HTTP_HEADERS)
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        logging.info("Resuming LINZ tile %s from byte %s", code, existing)
    try:
        with requests.get(
            url,
            stream=True,
            timeout=LINZ_HTTP_TIMEOUT,
            headers=headers,
        ) as response:
            if existing > 0 and response.status_code == 200:
                existing = 0
            elif existing > 0 and response.status_code != 206:
                response.raise_for_status()
                existing = 0
            else:
                response.raise_for_status()
            append = existing > 0 and response.status_code == 206
            total = expected
            if total is None:
                length = response.headers.get("Content-Length")
                if length:
                    total = existing + int(length) if append else int(length)
            with open(part, "ab" if append else "wb") as handle:
                got = existing
                last_pct = -1
                for chunk in response.iter_content(LINZ_TILE_CHUNK):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    got += len(chunk)
                    if total:
                        frac = min(1.0, got / total)
                        pct = int(frac * 100)
                        if pct != last_pct:
                            last_pct = pct
                            got_mb = got // (1024 * 1024)
                            total_mb = max(1, total // (1024 * 1024))
                            yield frac, f"Downloading {code} ({got_mb} / {total_mb} MB)"
                    else:
                        yield 0.5, f"Downloading {code} ({got // (1024 * 1024)} MB)"
    except requests.RequestException as exc:
        raise HydroScreenError(
            f"Could not download LINZ tile {code}. "
            f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
        ) from exc

    size = part.stat().st_size if part.exists() else 0
    if expected is not None and size != expected:
        if size < 1:
            try:
                part.unlink()
            except OSError:
                pass
        raise HydroScreenError(
            f"LINZ tile {code} download is incomplete ({size} of {expected} bytes). "
            "Run again to resume."
        )
    if size < 256:
        try:
            part.unlink()
        except OSError:
            pass
        raise HydroScreenError(f"LINZ tile {code} download was empty.")
    part.replace(dest)
    logging.info("Saved LINZ tile %s (%s bytes) to %s", code, dest.stat().st_size, dest)
    yield 1.0, f"Saved {code}"


def ensure_linz_tiles(codes, progress=None):
    """Download every intersecting sheet into outputs/linz-tiles/. Returns local paths."""
    paths = []
    n = max(1, len(codes))
    for index, code in enumerate(codes):
        for frac, message in iter_download_linz_tile(code):
            _emit_progress(progress, (index + float(frac)) / n, message)
        paths.append(str(linz_tile_path(code)))
    return paths


def iter_extract_linz_dem_for_bridge(lat, lon, out_tif):
    """Identify tiles, download full sheets, then clip the 500 m bridge window locally."""
    yield {"percent": 2, "message": "Finding DEM tiles"}
    plan = plan_linz_clip(lat, lon)
    if not plan["tiles"]:
        raise HydroScreenError(
            "This site is outside the New Zealand LiDAR 1m DEM coverage "
            f"({LINZ_LIDAR_1M_LAYER})."
        )
    logging.info(
        "Bridge %.5f, %.5f uses LINZ 1m sheets %s (%.0f m local clip)",
        plan["lat"],
        plan["lon"],
        ", ".join(plan["tiles"]),
        plan["clip_size_m"],
    )
    n = len(plan["tiles"])
    yield {
        "percent": 5,
        "message": f"Downloading {', '.join(plan['tiles'])}",
        "plan": plan,
    }
    for index, code in enumerate(plan["tiles"]):
        for frac, message in iter_download_linz_tile(code):
            overall = 5 + ((index + max(0.0, min(1.0, float(frac)))) / n) * 80
            yield {"percent": int(round(overall)), "message": message, "plan": plan}
    paths = [str(linz_tile_path(code)) for code in plan["tiles"]]
    yield {"percent": 86, "message": "Clipping DEM to the bridge area", "plan": plan}

    cache_file = _cache_file_for(plan["bbox_4326"], None, bounds_2193=plan["bounds_2193"])
    out_path = Path(out_tif)
    with _CACHE_LOCK:
        if cache_file.exists() and cache_file.stat().st_size > 256:
            logging.info("Reusing cached New Zealand LiDAR clip %s", cache_file.name)
            if out_path.resolve() != cache_file.resolve():
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cache_file, out_path)
            yield {
                "percent": 100,
                "message": "DEM ready",
                "path": str(out_path),
                "plan": plan,
            }
            return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    clip_dem_tiles(paths, plan["bounds_2193"], str(out_path))
    with _CACHE_LOCK:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            if out_path.resolve() != cache_file.resolve():
                shutil.copyfile(out_path, cache_file)
            _prune_dem_cache()
        except OSError as exc:
            logging.warning("Could not cache LiDAR clip: %s", exc)
    yield {"percent": 100, "message": "DEM ready", "path": str(out_path), "plan": plan}


def extract_linz_dem_for_bridge(lat, lon, out_tif, progress=None):
    """Download intersecting LINZ sheets, then clip 500 m locally. Returns (path, plan)."""
    path = None
    plan = None
    for event in iter_extract_linz_dem_for_bridge(lat, lon, out_tif):
        plan = event.get("plan") or plan
        if progress is not None and "percent" in event:
            _emit_progress(progress, event["percent"] / 100.0, event.get("message"))
        if event.get("path"):
            path = event["path"]
    return path, plan


def _emit_progress(progress, fraction, message=None):
    if progress is None:
        return
    progress(max(0.0, min(1.0, float(fraction))), message)


def clip_dem_tiles(tile_uris, bounds_2193, out_tif, nodata=-9999.0, resolution=None, progress=None):
    """Mosaic windowed reads from GeoTIFF URIs into an NZTM clip."""
    _configure_gdal_http()
    west, south, east, north = bounds_2193
    if east <= west or north <= south:
        raise HydroScreenError("Invalid DEM clip window.")
    datasets = []
    try:
        _emit_progress(progress, 0.04, "Opening DEM tiles")
        for uri in tile_uris:
            try:
                datasets.append(rasterio.open(uri))
            except Exception as exc:
                if "/vsicurl/" in str(uri):
                    raise HydroScreenError(
                        "Could not open the New Zealand LiDAR 1m DEM. "
                        f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                    ) from exc
                raise
        if not datasets:
            raise HydroScreenError("No DEM tiles were available to clip.")
        _emit_progress(progress, 0.12, "Downloading DEM")
        merge_kw = {
            "bounds": (west, south, east, north),
            "nodata": nodata,
        }
        if resolution is not None:
            merge_kw["res"] = (float(resolution), float(resolution))
        mosaic, transform = raster_merge(datasets, **merge_kw)
        data = mosaic[0]
        valid = np.isfinite(data) & (data != nodata)
        if not np.any(valid):
            raise HydroScreenError(
                "The New Zealand LiDAR 1m DEM has no elevation values at this site."
            )
        _emit_progress(progress, 0.9, "Writing DEM clip")
        profile = {
            "driver": "GTiff",
            "height": int(data.shape[0]),
            "width": int(data.shape[1]),
            "count": 1,
            "dtype": data.dtype,
            "crs": datasets[0].crs or "EPSG:2193",
            "transform": transform,
            "nodata": nodata,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        }
        with rasterio.open(out_tif, "w", **profile) as dst:
            dst.write(data, 1)
        _emit_progress(progress, 1.0, "DEM ready")
    finally:
        for src in datasets:
            src.close()
    return out_tif


def download_linz_lidar_1m(bbox, out_tif, resolution=None, bounds_2193=None, progress=None):
    """Download intersecting LINZ sheets, then clip locally around bbox.

    When bounds_2193 is set, the GeoTIFF is clipped to that exact NZTM window
    (used for the 500 m site square) and is not grown for cache snapping.
    Full sheets are stored under outputs/linz-tiles/; only the clip is cached.
    """
    if bounds_2193 is None:
        bbox = expand_bbox_for_cache(bbox)
    cache_file = _cache_file_for(bbox, resolution, bounds_2193=bounds_2193)
    out_path = Path(out_tif)

    _emit_progress(progress, 0.04, "Finding DEM tiles")
    codes = linz_tiles_for_bbox(bbox)
    if not codes:
        raise HydroScreenError(
            "This site is outside the New Zealand LiDAR 1m DEM coverage "
            f"({LINZ_LIDAR_1M_LAYER})."
        )
    logging.info(
        "Downloading New Zealand LiDAR 1m DEM sheets %s, then clipping locally",
        ", ".join(codes),
    )

    def on_download(frac, message=None):
        _emit_progress(progress, 0.05 + 0.75 * float(frac), message)

    local_paths = ensure_linz_tiles(codes, progress=on_download)

    with _CACHE_LOCK:
        if cache_file.exists() and cache_file.stat().st_size > 256:
            logging.info("Reusing cached New Zealand LiDAR clip %s", cache_file.name)
            _emit_progress(progress, 0.9, "Reusing cached DEM")
            if out_path.resolve() != cache_file.resolve():
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cache_file, out_path)
            _emit_progress(progress, 1.0, "DEM ready")
            return str(out_path)

    if bounds_2193 is not None:
        west, south, east, north = (float(v) for v in bounds_2193)
    else:
        transformer = _transformer("EPSG:4326", "EPSG:2193")
        minx, miny, maxx, maxy = bbox
        x1, y1 = transformer.transform(minx, miny)
        x2, y2 = transformer.transform(maxx, maxy)
        west, east = min(x1, x2) - 2.0, max(x1, x2) + 2.0
        south, north = min(y1, y2) - 2.0, max(y1, y2) + 2.0

    def on_clip(frac, message=None):
        _emit_progress(progress, 0.82 + 0.18 * float(frac), message)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    clip_dem_tiles(
        local_paths,
        (west, south, east, north),
        str(out_path),
        resolution=resolution,
        progress=on_clip,
    )
    with _CACHE_LOCK:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            if out_path.resolve() != cache_file.resolve():
                shutil.copyfile(out_path, cache_file)
            _prune_dem_cache()
        except OSError as exc:
            logging.warning("Could not cache LiDAR clip: %s", exc)
    return str(out_path)


def screening_dem_radius_m(buffer, along_m, length):
    """Half-side of the site DEM clip. Window size is fixed; args kept for callers."""
    return DEM_CLIP_SIZE_M / 2.0


def _colorize_elevation(z):
    """Hillshaded terrain RGBA; nodata / NaN pixels are transparent."""
    valid = np.isfinite(z)
    if not np.any(valid):
        raise HydroScreenError("No elevation values in this preview window.")
    vmin, vmax = np.nanpercentile(z, [2, 98])
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        vmin = float(np.nanmin(z[valid]))
        vmax = vmin + 1.0
    filled = np.where(valid, z, vmin)
    rgb = LightSource(azdeg=315, altdeg=45).shade(
        filled,
        cmap=matplotlib.colormaps["terrain"],
        vert_exag=2.0,
        blend_mode="overlay",
        vmin=vmin,
        vmax=vmax,
    )
    rgba = np.zeros((z.shape[0], z.shape[1], 4), dtype=np.uint8)
    rgba[..., :3] = (np.clip(rgb[..., :3], 0, 1) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    return rgba


def render_dem_overlay_png(dem_path, bbox_4326, max_px=DEM_PREVIEW_MAX_PX):
    """Warp a DEM window to WGS84 and return (png_bytes, (west, south, east, north))."""
    west, south, east, north = [float(v) for v in bbox_4326]
    with rasterio.open(dem_path) as src:
        src_west, src_south, src_east, src_north = transform_bounds(
            src.crs, "EPSG:4326", *src.bounds, densify_pts=21
        )
        west = max(west, src_west)
        east = min(east, src_east)
        south = max(south, src_south)
        north = min(north, src_north)
        if east <= west or north <= south:
            raise HydroScreenError("The DEM does not overlap this location.")
        width = max_px
        height = max(1, int(round(max_px * (north - south) / max(east - west, 1e-12))))
        if height > max_px:
            height = max_px
            width = max(1, int(round(max_px * (east - west) / max(north - south, 1e-12))))
        dst_transform = from_bounds(west, south, east, north, width, height)
        dest = np.full((height, width), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=dest,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.bilinear,
            src_nodata=src.nodata,
            dst_nodata=np.nan,
        )
    rgba = _colorize_elevation(dest)
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue(), (west, south, east, north)


def iter_dem_preview(lat, lon, dem_path=None, along_m=300.0, length=200.0, buffer=200.0):
    """Yield progress events, then a final overlay result with png/bounds."""
    _ = (along_m, length, buffer)
    yield {"percent": 0, "message": "Finding DEM tiles"}
    bounds_2193 = square_clip_2193(lat, lon)
    bbox = bbox_4326_from_2193(bounds_2193)
    radius = DEM_CLIP_SIZE_M / 2.0
    temp_dir = None
    src_path = dem_path
    source = "local"
    linz_tiles = []
    try:
        if src_path is None:
            temp_dir = tempfile.mkdtemp(prefix="hydroscreen_preview_")
            src_path = os.path.join(temp_dir, "dem.tif")
            plan = None
            try:
                for event in iter_extract_linz_dem_for_bridge(lat, lon, src_path):
                    yield {
                        "percent": int(event.get("percent") or 0),
                        "message": event.get("message") or "Downloading DEM…",
                    }
                    if event.get("path"):
                        src_path = event["path"]
                    if event.get("plan"):
                        plan = event["plan"]
            except HydroScreenError:
                raise
            except Exception as exc:
                logging.exception("LINZ 1m DEM download failed")
                raise HydroScreenError(
                    "Could not download the New Zealand LiDAR 1m DEM. "
                    f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                ) from exc
            source = "linz-lidar-1m"
            linz_tiles = list((plan or {}).get("tiles") or [])
            yield {"percent": 92, "message": "Drawing DEM overlay"}
        else:
            yield {"percent": 40, "message": "Reading DEM"}
        png, bounds = render_dem_overlay_png(src_path, bbox)
        yield {
            "percent": 100,
            "message": "DEM ready",
            "done": True,
            "png": png,
            "bounds": bounds,
            "source": source,
            "radius_m": float(radius),
            "clip_size_m": DEM_CLIP_SIZE_M,
            "tiles": list(linz_tiles),
        }
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def preview_dem_overlay(
    lat,
    lon,
    dem_path=None,
    along_m=300.0,
    length=200.0,
    buffer=200.0,
    progress=None,
):
    """Build a map overlay PNG of the DEM HydroBridge will sample at this site."""
    result = None
    for event in iter_dem_preview(
        lat, lon, dem_path=dem_path, along_m=along_m, length=length, buffer=buffer
    ):
        if progress is not None and "percent" in event:
            _emit_progress(progress, event["percent"] / 100.0, event.get("message"))
        if event.get("done"):
            result = {
                "png": event["png"],
                "bounds": event["bounds"],
                "source": event["source"],
                "radius_m": event["radius_m"],
                "clip_size_m": event["clip_size_m"],
                "tiles": event.get("tiles") or [],
            }
    return result


def _line_parts(geom):
    """Flatten a shapely geometry into LineString pieces with length."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > 0 else []
    if geom.geom_type == "MultiLineString":
        return [part for part in geom.geoms if part.length > 0]
    if geom.geom_type == "GeometryCollection":
        parts = []
        for item in geom.geoms:
            parts.extend(_line_parts(item))
        return parts
    return []


def clip_line_to_raster(line, dem_path, lon=None, lat=None):
    """Keep the centreline segment that sits on the DEM window, or None."""
    if line is None or line.is_empty or len(line.coords) < 2:
        return None
    with rasterio.open(dem_path) as src:
        to_src = _transformer("EPSG:4326", str(src.crs))
        to_ll = _transformer(str(src.crs), "EPSG:4326")
        west, south, east, north = (
            float(src.bounds.left),
            float(src.bounds.bottom),
            float(src.bounds.right),
            float(src.bounds.top),
        )
    proj_line = LineString([to_src.transform(x, y) for x, y in line.coords])
    if proj_line.length <= 0:
        return None
    parts = _line_parts(proj_line.intersection(box(west, south, east, north)))
    if not parts:
        return None
    if lon is not None and lat is not None:
        pin = Point(*to_src.transform(float(lon), float(lat)))
        part = min(parts, key=lambda geom: (geom.distance(pin), -geom.length))
    else:
        part = max(parts, key=lambda geom: geom.length)
    coords = [to_ll.transform(x, y) for x, y in part.coords]
    if len(coords) < 2:
        return None
    return LineString(coords)


def centerline_reach(centerline, lon, lat, along_m):
    """Return the centreline segment around the bridge, length along_m."""
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")
    proj_line = LineString([transformer_to_3857.transform(x, y) for x, y in centerline.coords])
    if proj_line.length <= 0:
        return centerline
    origin = proj_line.project(Point(*transformer_to_3857.transform(lon, lat)))
    half = max(float(along_m), 50.0) / 2.0
    start = max(0.0, origin - half)
    end = min(proj_line.length, origin + half)
    if end - start < 1.0:
        start = max(0.0, origin - 25.0)
        end = min(proj_line.length, origin + 25.0)
    part = substring(proj_line, start, end)
    if part.is_empty or part.length <= 0:
        return centerline
    coords = [transformer_to_4326.transform(x, y) for x, y in part.coords]
    if len(coords) < 2:
        return centerline
    return LineString(coords)


def bbox_from_transects(lon, lat, transects, pad_m=50.0):
    """Geographic bbox covering the pin and transects, capped around the pin."""
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")
    px, py = transformer_to_3857.transform(lon, lat)
    xs = [px]
    ys = [py]
    for line, _offset in transects:
        for x, y in line.coords:
            xx, yy = transformer_to_3857.transform(x, y)
            xs.append(xx)
            ys.append(yy)
    minx = max(min(xs) - pad_m, px - MAX_DEM_RADIUS_M)
    maxx = min(max(xs) + pad_m, px + MAX_DEM_RADIUS_M)
    miny = max(min(ys) - pad_m, py - MAX_DEM_RADIUS_M)
    maxy = min(max(ys) + pad_m, py + MAX_DEM_RADIUS_M)
    corners = [
        transformer_to_4326.transform(minx, miny),
        transformer_to_4326.transform(minx, maxy),
        transformer_to_4326.transform(maxx, miny),
        transformer_to_4326.transform(maxx, maxy),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    radius = max(maxx - px, px - minx, maxy - py, py - miny)
    return (min(lons), min(lats), max(lons), max(lats)), float(radius)


def bbox_from_lonlat_points(points, pad_m=80.0):
    """Geographic bbox covering lon/lat points, padded in metres."""
    if not points:
        raise HydroScreenError("Need coordinates to clip the DEM.")
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")
    xs = []
    ys = []
    for lon, lat in points:
        x, y = transformer_to_3857.transform(float(lon), float(lat))
        xs.append(x)
        ys.append(y)
    cx = 0.5 * (min(xs) + max(xs))
    cy = 0.5 * (min(ys) + max(ys))
    minx = max(min(xs) - pad_m, cx - MAX_DEM_RADIUS_M)
    maxx = min(max(xs) + pad_m, cx + MAX_DEM_RADIUS_M)
    miny = max(min(ys) - pad_m, cy - MAX_DEM_RADIUS_M)
    maxy = min(max(ys) + pad_m, cy + MAX_DEM_RADIUS_M)
    corners = [
        transformer_to_4326.transform(minx, miny),
        transformer_to_4326.transform(minx, maxy),
        transformer_to_4326.transform(maxx, miny),
        transformer_to_4326.transform(maxx, maxy),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    return (min(lons), min(lats), max(lons), max(lats))

# OSM centreline via Overpass

def query_osm_waterway(lat, lon, radius_m=200):
    """Query Overpass for nearby waterway ways and return the longest/nearest LineString in lat/lon coords."""
    # Overpass expects radius in meters, query nodes/ways around point
    query = f"""
[out:json][timeout:25];
(
  way(around:{radius_m},{lat},{lon})[waterway];
  relation(around:{radius_m},{lat},{lon})[waterway];
);
out geom;
"""
    headers = {
        "User-Agent": "hydroscreen/0.1 (HydroBridge)",
        "Accept": "application/json",
    }
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.warning("OSM Overpass query failed: %s", e)
        return None
    # Find ways and relations with geometry
    best = None
    best_len = 0
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        ls = LineString(coords)
        l = ls.length
        if l > best_len:
            best = ls
            best_len = l
    return best

# Transect generation

def generate_transects(centerline: LineString, distances_m=None, interval=50, n_each_side=3, length_m=200, bridge_lon=None, bridge_lat=None, along_m=None, cover_full_line=False):
    """Generate transects perpendicular to the centreline.

    Stations are measured from the nearest point on the centreline to the
    bridge coordinates when provided; otherwise the line midpoint is used.
    If cover_full_line is set, stations run from the start to the end of the
    drawn centreline at `interval`. Otherwise, if along_m is set, stations run
    from -along_m/2 to +along_m/2 at `interval`.
    Returns list of (transect LineString, station_m).
    """
    if interval <= 0:
        raise HydroScreenError("Transect spacing along the river must be greater than 0.")
    if length_m <= 0:
        raise HydroScreenError("Transect length must be greater than 0.")

    # Work in WebMercator for metric distances
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")

    # Project centerline to 3857
    proj_coords = [transformer_to_3857.transform(x, y) for (x, y) in centerline.coords]
    proj_line = LineString(proj_coords)
    total_len = proj_line.length
    if bridge_lon is not None and bridge_lat is not None:
        bx, by = transformer_to_3857.transform(bridge_lon, bridge_lat)
        origin = proj_line.project(Point(bx, by))
    else:
        origin = total_len / 2.0
    if not distances_m:
        if cover_full_line:
            n = int(math.floor(total_len / interval + 1e-9)) if interval > 0 else 0
            n = min(max(n, 0), 100)
            starts = [float(i * interval) for i in range(n + 1)]
            if not starts:
                starts = [0.0]
            if total_len - starts[-1] > 0.5:
                starts.append(float(total_len))
            if all(abs(s - origin) > 0.25 for s in starts):
                starts.append(float(min(max(origin, 0.0), total_len)))
                starts.sort()
            distances_m = [s - origin for s in starts]
        else:
            if along_m is None:
                along_m = 2.0 * n_each_side * interval
            half = max(float(along_m), 0.0) / 2.0
            n = int(math.floor(half / interval + 1e-9))
            n = min(max(n, 0), 50)
            distances_m = [i * interval for i in range(-n, n + 1)]
    stations = [origin + d for d in distances_m]

    transects = []
    half_len = length_m / 2.0
    for s in stations:
        offset = s - origin
        s = min(max(s, 0.0), total_len)
        pt = proj_line.interpolate(s)
        # get line direction (tangent) at this location by sampling nearby
        delta = 1.0
        before = max(0, s - delta)
        after = min(total_len, s + delta)
        p0 = proj_line.interpolate(before)
        p1 = proj_line.interpolate(after)
        # tangent vector
        dx = p1.x - p0.x
        dy = p1.y - p0.y
        # normal vector
        nx = -dy
        ny = dx
        norm = math.hypot(nx, ny)
        if norm == 0:
            continue
        nx /= norm
        ny /= norm
        a = (pt.x + nx * half_len, pt.y + ny * half_len)
        b = (pt.x - nx * half_len, pt.y - ny * half_len)
        # transform back to lon/lat
        a_ll = transformer_to_4326.transform(a[0], a[1])
        b_ll = transformer_to_4326.transform(b[0], b[1])
        transect_line = LineString([a_ll, b_ll])
        transects.append((transect_line, offset))
    return transects

# Sample DEM along a LineString

def sample_dem_along_line(dem_path, line: LineString, n_points=None, spacing_m=None, src=None):
    """Sample DEM elevations at regular metric spacing along a lon/lat line.

    Returns (distances_m, elevations, sample_lonlat). Pass an open rasterio
    dataset as `src` to avoid reopening the file for every transect.
    """
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    transformer_to_4326 = _transformer("EPSG:3857", "EPSG:4326")
    proj_line = LineString([transformer_to_3857.transform(x, y) for x, y in line.coords])
    length = float(proj_line.length)
    if length <= 0:
        return np.array([0.0]), np.array([np.nan]), [tuple(line.coords[0])]

    if spacing_m is not None:
        if spacing_m <= 0:
            raise HydroScreenError("Sample spacing on the transect must be greater than 0.")
        distances = list(np.arange(0.0, length, float(spacing_m)))
        if not distances or (length - distances[-1]) > 1e-6:
            distances.append(length)
        if len(distances) > 2001:
            distances = list(np.linspace(0.0, length, 2001))
    else:
        count = max(2, int(n_points or 201))
        distances = list(np.linspace(0.0, length, count))

    xs = np.array([proj_line.interpolate(dist).x for dist in distances])
    ys = np.array([proj_line.interpolate(dist).y for dist in distances])
    lons, lats = transformer_to_4326.transform(xs, ys)
    coords = list(zip(map(float, np.atleast_1d(lons)), map(float, np.atleast_1d(lats))))

    close_src = False
    if src is None:
        src = rasterio.open(dem_path)
        close_src = True
    try:
        transformer = _transformer("EPSG:4326", str(src.crs))
        rx, ry = transformer.transform(lons, lats)
        coords_raster = list(zip(np.atleast_1d(rx), np.atleast_1d(ry)))
        elevations = []
        nodata = src.nodata
        for val in src.sample(coords_raster):
            v = val[0]
            if nodata is not None and (v == nodata or np.isnan(v)):
                elevations.append(np.nan)
            else:
                elevations.append(float(v))
        return np.array(distances), np.array(elevations), coords
    finally:
        if close_src:
            src.close()


MAX_XS_LENGTH_M = 2000.0


def sample_drawn_cross_section(
    lon1,
    lat1,
    lon2,
    lat2,
    dem_path=None,
    spacing_m=1.0,
    site_lat=None,
    site_lon=None,
):
    """Sample DEM elevations along a two-point line and return a JSON-ready profile."""
    try:
        lon1, lat1, lon2, lat2 = float(lon1), float(lat1), float(lon2), float(lat2)
    except (TypeError, ValueError) as exc:
        raise HydroScreenError("Cross-section points must be numbers.") from exc
    if not (-180 <= lon1 <= 180 and -180 <= lon2 <= 180 and -90 <= lat1 <= 90 and -90 <= lat2 <= 90):
        raise HydroScreenError("Cross-section coordinates are out of range.")
    if spacing_m is None or float(spacing_m) <= 0:
        raise HydroScreenError("Sample spacing on the transect must be greater than 0.")
    spacing_m = float(spacing_m)
    length = haversine(lon1, lat1, lon2, lat2)
    if length < 1.0:
        raise HydroScreenError("The cross-section line is too short. Click two points farther apart.")
    if length > MAX_XS_LENGTH_M:
        raise HydroScreenError(
            f"The cross-section can be at most {int(MAX_XS_LENGTH_M)} m long. Draw a shorter line near the bridge."
        )

    line = LineString([(lon1, lat1), (lon2, lat2)])
    temp_dir = None
    source = "local"
    try:
        if dem_path is None:
            try:
                clip_lat = float(site_lat) if site_lat is not None else 0.5 * (lat1 + lat2)
                clip_lon = float(site_lon) if site_lon is not None else 0.5 * (lon1 + lon2)
            except (TypeError, ValueError):
                clip_lat = 0.5 * (lat1 + lat2)
                clip_lon = 0.5 * (lon1 + lon2)
            temp_dir = tempfile.mkdtemp(prefix="hydroscreen_xs_")
            dem_path = os.path.join(temp_dir, "dem.tif")
            dem_path, _plan = extract_linz_dem_for_bridge(clip_lat, clip_lon, dem_path)
            source = "linz-lidar-1m"
        dists, elevs, _coords = sample_dem_along_line(dem_path, line, spacing_m=spacing_m)
        finite = elevs[np.isfinite(elevs)]
        if len(finite) == 0:
            raise HydroScreenError(
                "No DEM elevations along this line. Draw it over the LiDAR coverage around the bridge."
            )
        return {
            "distance_m": [float(x) for x in dists],
            "elevation_m": [None if not np.isfinite(z) else float(z) for z in elevs],
            "length_m": float(dists[-1]) if len(dists) else float(length),
            "n_samples": int(len(dists)),
            "sample_spacing_m": spacing_m,
            "zmin": float(np.min(finite)),
            "zmax": float(np.max(finite)),
            "source": source,
            "start": [lon1, lat1],
            "end": [lon2, lat2],
        }
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

# simple haversine

def haversine(lon1, lat1, lon2, lat2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# Basic hydraulic checks

def hydraulics_at_stage(dists, elevs, water_level):
    """Trapezoidal area and wetted perimeter at a water-surface elevation."""
    if len(dists) < 2:
        return {"area_m2": 0.0, "wetted_perimeter_m": 0.0, "top_width_m": 0.0, "hydraulic_radius_m": 0.0}
    area = 0.0
    perimeter = 0.0
    top_width = 0.0
    for i in range(len(dists) - 1):
        x1, x2 = float(dists[i]), float(dists[i + 1])
        z1, z2 = float(elevs[i]), float(elevs[i + 1])
        if not (np.isfinite(x1) and np.isfinite(x2) and np.isfinite(z1) and np.isfinite(z2)):
            continue
        dx = x2 - x1
        if dx <= 0:
            continue
        d1 = water_level - z1
        d2 = water_level - z2
        if d1 <= 0 and d2 <= 0:
            continue
        if d1 > 0 and d2 > 0:
            area += 0.5 * (d1 + d2) * dx
            perimeter += math.hypot(dx, z2 - z1)
            top_width += dx
            continue
        # Waterline intersects this segment.
        if z2 == z1:
            continue
        t = (water_level - z1) / (z2 - z1)
        t = min(max(t, 0.0), 1.0)
        xi = x1 + t * dx
        if d1 > 0:
            area += 0.5 * d1 * (xi - x1)
            perimeter += math.hypot(xi - x1, water_level - z1)
            top_width += xi - x1
        else:
            area += 0.5 * d2 * (x2 - xi)
            perimeter += math.hypot(x2 - xi, water_level - z2)
            top_width += x2 - xi
    radius = (area / perimeter) if perimeter > 0 else 0.0
    return {
        "area_m2": float(area),
        "wetted_perimeter_m": float(perimeter),
        "top_width_m": float(top_width),
        "hydraulic_radius_m": float(radius),
    }


def manning_discharge(area, radius, mannings_n, slope):
    if area <= 0 or radius <= 0 or mannings_n <= 0 or slope <= 0:
        return 0.0, 0.0
    velocity = (1.0 / mannings_n) * (radius ** (2.0 / 3.0)) * (slope ** 0.5)
    return float(velocity), float(velocity * area)


ARI_YEARS = (10, 25, 50, 100, 1000)
ARI_COLORS = {
    10: "#0284c7",
    25: "#059669",
    50: "#d97706",
    100: "#7c3aed",
    1000: "#be123c",
}
ARI_STAT_KEYS = (
    "water_level_m",
    "max_depth_m",
    "width_m",
    "area_m2",
    "depth_mean_m",
    "wetted_perimeter_m",
    "hydraulic_radius_m",
    "velocity_m_s",
    "discharge_m3_s",
    "target_discharge_m3_s",
    "conveys",
    "overtopped",
)


def ari_key(years):
    return f"{int(years)}y"


def ari_label(years):
    years = int(years)
    return f"{years:,}-year ARI" if years >= 1000 else f"{years}-year ARI"


def describe_flow_scenario(years, flow_m3_s):
    years = int(years)
    return {
        "years": years,
        "key": ari_key(years),
        "label": ari_label(years),
        "flow_m3_s": float(flow_m3_s),
        "color": ARI_COLORS[years],
    }


def normalize_flow_scenarios(flow_m3_s=10.0, scenarios=None):
    """Return ordered unique ARI design-flow scenarios.

    When *scenarios* is omitted, a single 100-year ARI uses *flow_m3_s* so the
    CLI and older callers keep working. An explicit empty list is an error.
    """
    if flow_m3_s is None:
        flow_m3_s = 10.0
    try:
        fallback = float(flow_m3_s)
    except (TypeError, ValueError) as exc:
        raise HydroScreenError("Flow rate must be a number.") from exc
    if fallback < 0:
        raise HydroScreenError("Flow rate cannot be negative.")

    if scenarios is None:
        return [describe_flow_scenario(100, fallback)]

    if isinstance(scenarios, dict):
        raw_items = [{"years": key, "flow_m3_s": value} for key, value in scenarios.items()]
    else:
        try:
            raw_items = list(scenarios)
        except TypeError as exc:
            raise HydroScreenError("Return periods must be a list of year and flow pairs.") from exc

    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise HydroScreenError("Each return period needs a year and a flow rate.")
        years = item.get("years", item.get("ari", item.get("year")))
        quantity = item.get("flow_m3_s", item.get("flow", item.get("q")))
        if years is None:
            raise HydroScreenError("Each return period needs a year and a flow rate.")
        try:
            years = int(years)
        except (TypeError, ValueError) as exc:
            raise HydroScreenError("Return period years must be a number.") from exc
        if years not in ARI_YEARS:
            allowed = ", ".join(str(year) for year in ARI_YEARS)
            raise HydroScreenError(f"Unsupported return period {years}. Choose from {allowed}.")
        if quantity is None:
            quantity = fallback
        try:
            quantity = float(quantity)
        except (TypeError, ValueError) as exc:
            raise HydroScreenError("Flow rate must be a number.") from exc
        if quantity < 0:
            raise HydroScreenError("Flow rate cannot be negative.")
        items.append(describe_flow_scenario(years, quantity))

    if not items:
        raise HydroScreenError("Select at least one return period.")

    seen = set()
    ordered = []
    for item in items:
        if item["key"] in seen:
            raise HydroScreenError(f"{item['label']} is selected more than once.")
        seen.add(item["key"])
        ordered.append(item)
    return ordered


def _ari_hydraulics(stats, scenario):
    slim = {key: stats.get(key) for key in ARI_STAT_KEYS}
    slim.update({
        "years": scenario["years"],
        "key": scenario["key"],
        "label": scenario["label"],
        "color": scenario["color"],
        "flow_m3_s": scenario["flow_m3_s"],
    })
    return slim


def solve_water_level(dists, elevs, flow_m3_s, mannings_n, slope, step_m=0.02, cancel_event=None):
    """Raise water level along the transect until Manning Q matches the specified flow."""
    finite = elevs[np.isfinite(elevs)]
    empty = {
        "water_level_m": None,
        "max_depth_m": 0.0,
        "width_m": 0.0,
        "area_m2": 0.0,
        "depth_mean_m": 0.0,
        "wetted_perimeter_m": 0.0,
        "hydraulic_radius_m": 0.0,
        "velocity_m_s": 0.0,
        "discharge_m3_s": 0.0,
        "target_discharge_m3_s": float(flow_m3_s),
        "mannings_n": float(mannings_n),
        "slope": float(slope),
        "conveys": False,
        "overtopped": False,
    }
    if len(finite) == 0:
        return empty
    zmin = float(np.min(finite))
    zmax = float(np.max(finite))
    if flow_m3_s <= 0:
        empty.update({"water_level_m": zmin, "conveys": True})
        return empty

    max_wse = zmax + 2.0
    wse = zmin + step_m
    hyd = hydraulics_at_stage(dists, elevs, wse)
    velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
    # Increase stage until conveyance meets the target flow.
    while discharge < flow_m3_s and wse < max_wse:
        check_cancelled(cancel_event)
        wse += step_m
        hyd = hydraulics_at_stage(dists, elevs, wse)
        velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)

    # Refine between the last two steps.
    lo = max(zmin, wse - step_m)
    hi = wse
    for _ in range(24):
        check_cancelled(cancel_event)
        mid = 0.5 * (lo + hi)
        hyd = hydraulics_at_stage(dists, elevs, mid)
        velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
        if discharge < flow_m3_s:
            lo = mid
        else:
            hi = mid
    wse = hi
    hyd = hydraulics_at_stage(dists, elevs, wse)
    velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
    width = hyd["top_width_m"]
    area = hyd["area_m2"]
    overtopped = wse >= (zmax - 1e-6)
    conveys = discharge + 1e-6 >= flow_m3_s
    return {
        "water_level_m": float(wse),
        "max_depth_m": float(max(0.0, wse - zmin)),
        "width_m": float(width),
        "area_m2": float(area),
        "depth_mean_m": float(area / width) if width > 0 else 0.0,
        "wetted_perimeter_m": hyd["wetted_perimeter_m"],
        "hydraulic_radius_m": hyd["hydraulic_radius_m"],
        "velocity_m_s": float(velocity),
        "discharge_m3_s": float(discharge),
        "target_discharge_m3_s": float(flow_m3_s),
        "mannings_n": float(mannings_n),
        "slope": float(slope),
        "conveys": bool(conveys),
        "overtopped": bool(overtopped),
    }


def estimate_centerline_slope(dem_path, centerline, spacing_m=10.0, src=None):
    """Bed slope along the river centreline from DEM samples, as |dz/ds|."""
    slope, _dists, _elevs, _signed, _intercept = centerline_slope_and_profile(
        dem_path, centerline, spacing_m=spacing_m, src=src
    )
    return slope


def centerline_slope_and_profile(dem_path, centerline, spacing_m=10.0, src=None):
    """Return (|dz/ds|, distances_m, elevations, signed slope, intercept)."""
    dists, elevs, _coords = sample_dem_along_line(
        dem_path, centerline, spacing_m=spacing_m, src=src
    )
    mask = np.isfinite(elevs)
    if mask.sum() < 2:
        return None, dists, elevs, None, None
    distance = dists[mask]
    elevation = elevs[mask]
    if float(distance[-1] - distance[0]) < 1.0:
        return None, dists, elevs, None, None
    signed, intercept = np.polyfit(distance.astype(float), elevation.astype(float), 1)
    abs_slope = max(abs(float(signed)), 1e-6)
    return abs_slope, dists, elevs, float(signed), float(intercept)


def centerline_bridge_station_m(centerline, lon, lat):
    """Metres along the centreline from its start to the bridge pin."""
    transformer_to_3857 = _transformer("EPSG:4326", "EPSG:3857")
    proj_line = LineString(
        [transformer_to_3857.transform(x, y) for x, y in centerline.coords]
    )
    bx, by = transformer_to_3857.transform(lon, lat)
    return float(proj_line.project(Point(bx, by))), float(proj_line.length)


def _profile_json(dists, elevs):
    return {
        "distance_m": [float(x) for x in dists],
        "elevation_m": [None if not np.isfinite(z) else float(z) for z in elevs],
    }


def plot_cross_section(dists, elevs, out_png, water_level=None, water_levels=None):
    global _PLOT_FIG, _PLOT_AX
    levels = list(water_levels or [])
    if not levels and water_level is not None and np.isfinite(water_level):
        levels = [{"water_level_m": water_level, "color": "#1f6f8b", "label": "Water level"}]
    with _PLOT_LOCK:
        if _PLOT_FIG is None or _PLOT_AX is None:
            _PLOT_FIG, _PLOT_AX = plt.subplots(figsize=(7.2, 3.4), dpi=90)
        ax = _PLOT_AX
        ax.clear()
        ax.plot(dists, elevs, "-k", label="Ground")
        finite = elevs[np.isfinite(elevs)]
        y_floor = (np.min(finite) - 1) if len(finite) else -1
        if levels:
            fill_one = len(levels) == 1
            for item in levels:
                level = item.get("water_level_m")
                if level is None or not np.isfinite(level):
                    continue
                color = item.get("color") or "#1f6f8b"
                label = item.get("label") or "Water level"
                if fill_one:
                    wet = np.isfinite(elevs) & (elevs < level)
                    ax.fill_between(dists, elevs, level, where=wet, color=color, alpha=0.35, interpolate=True)
                ax.axhline(level, color=color, linestyle="--", linewidth=1.2, label=label)
            ax.legend(loc="best", frameon=False)
        else:
            ax.fill_between(dists, elevs, y_floor, color="lightblue")
        ax.set_xlabel("Distance (m)")
        ax.set_ylabel("Elevation (m)")
        ax.set_title("Cross-section")
        ax.grid(True)
        _PLOT_FIG.tight_layout()
        _PLOT_FIG.savefig(out_png, dpi=90)


DATA_SHEET_COLUMNS = ["ID", "Transect ID", "Distance_m", "Elevation_m"]


def write_screening_workbook(path, summary_rows, data_rows):
    """Write summary.xlsx with a SUMMARY sheet and a DATA sheet of sample points."""
    path = Path(path)
    summary_df = pd.DataFrame(summary_rows)
    data_df = pd.DataFrame(data_rows, columns=DATA_SHEET_COLUMNS)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="SUMMARY", index=False)
        data_df.to_excel(writer, sheet_name="DATA", index=False)
    return path


def centerline_from_coords(coords):
    """Build a river centreline from [[lon, lat], ...] vertices."""
    if not coords or len(coords) < 2:
        raise HydroScreenError("Draw at least two points along the river centreline.")
    points = []
    for item in coords:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise HydroScreenError("Each centreline point needs a longitude and latitude.")
        try:
            lon = float(item[0])
            lat = float(item[1])
        except (TypeError, ValueError) as exc:
            raise HydroScreenError("Centreline coordinates must be numbers.") from exc
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise HydroScreenError("Centreline coordinates are out of range.")
        if points and points[-1] == (lon, lat):
            continue
        points.append((lon, lat))
    if len(points) < 2:
        raise HydroScreenError("The drawn river line is too short. Add more points along the channel.")
    line = LineString(points)
    if line.length <= 0:
        raise HydroScreenError("The drawn river line has no length. Draw along the channel.")
    return line


def projected_length_m(line: LineString) -> float:
    """Length of a lon/lat line in metres (Web Mercator)."""
    transformer = _transformer("EPSG:4326", "EPSG:3857")
    proj = LineString([transformer.transform(x, y) for x, y in line.coords])
    return float(proj.length)


def run_screening(
    lat,
    lon,
    dem=None,
    wcs_base=None,
    wcs_layer=None,
    outdir="outputs",
    buffer=200.0,
    interval=50.0,
    n_each_side=3,
    length=200.0,
    mannings_n=0.035,
    flow_m3_s=10.0,
    flow_scenarios=None,
    along_m=None,
    sample_spacing=1.0,
    centerline_coords=None,
    cancel_event=None,
):
    """Run hydraulic screening at a bridge coordinate. Returns a result dict."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    check_cancelled(cancel_event)

    if along_m is None:
        along_m = 2.0 * n_each_side * interval
    if mannings_n <= 0:
        raise HydroScreenError("Manning's n must be greater than 0.")
    scenarios = normalize_flow_scenarios(flow_m3_s, flow_scenarios)
    primary_flow = scenarios[0]["flow_m3_s"]

    cover_full_line = False
    if centerline_coords:
        logging.info("Using user-drawn river centreline (%d vertices)", len(centerline_coords))
        centerline = centerline_from_coords(centerline_coords)
        centerline_source = "drawn"
        cover_full_line = True
        along_m = projected_length_m(centerline)
    else:
        logging.info("Querying OSM for waterway near lat=%s lon=%s", lat, lon)
        centerline = _run_interruptibly(
            lambda: query_osm_waterway(lat, lon, radius_m=500),
            cancel_event,
        )
        if centerline is None:
            logging.warning("No OSM waterway found within 500 m. Using a synthetic centreline (line through point).")
            centerline = LineString([(lon - 0.005, lat), (lon + 0.005, lat)])
            centerline_source = "synthetic"
        else:
            centerline_source = "osm"
    check_cancelled(cancel_event)

    dem_path = dem
    temp_dir = None
    dem_source_used = "local"
    kept_dem = None
    linz_tiles = []
    bounds_2193 = square_clip_2193(lat, lon)
    bbox = bbox_4326_from_2193(bounds_2193)
    buffer_m = DEM_CLIP_SIZE_M / 2.0

    if dem_path is None:
        check_cancelled(cancel_event)
        temp_dir = tempfile.mkdtemp(prefix="hydroscreen_")
        out_tif = os.path.join(temp_dir, "dem.tif")
        if wcs_base and wcs_layer:
            logging.info("Attempting user-supplied WCS for bbox (buffer %sm)", buffer_m)
            ok = _run_interruptibly(
                lambda: download_wcs_getcoverage(wcs_base, wcs_layer, bbox, out_tif),
                cancel_event,
            )
            if ok and _raster_covers_bbox(out_tif, bbox):
                dem_path = out_tif
                dem_source_used = "wcs"
            elif ok:
                logging.info(
                    "WCS returned a raster but it does not fully cover the site; using the LINZ 1m LiDAR DEM.",
                )
        if dem_path is None:
            logging.info(
                "Downloading New Zealand LiDAR 1m DEM around the site, then clipping locally "
                "(%sm × %sm)",
                int(DEM_CLIP_SIZE_M),
                int(DEM_CLIP_SIZE_M),
            )
            # Full-tile download and the local 500 m clip stay on this request
            # thread so Stop can still interrupt between sheets.
            cache_file = _cache_file_for(bbox, None, bounds_2193=bounds_2193)
            try:
                check_cancelled(cancel_event)
                dem_path, plan = extract_linz_dem_for_bridge(lat, lon, str(cache_file))
                linz_tiles = plan["tiles"]
                dem_source_used = "linz-lidar-1m"
                check_cancelled(cancel_event)
            except HydroScreenError:
                raise
            except Exception as exc:
                logging.exception("LINZ 1m DEM download failed")
                raise HydroScreenError(
                    "Could not download the New Zealand LiDAR 1m DEM. "
                    f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                ) from exc
            kept_dem = Path(outdir) / "dem_500m.tif"
            try:
                if Path(dem_path).resolve() != kept_dem.resolve():
                    shutil.copyfile(dem_path, kept_dem)
            except OSError as exc:
                logging.warning("Could not copy DEM clip into outputs: %s", exc)
                kept_dem = None

    if dem_path is None:
        raise HydroScreenError(
            "Could not clip the New Zealand LiDAR 1m DEM for this bridge."
        )

    if dem_source_used == "local" and not _raster_covers_bbox(dem_path, bbox_from_point(lat, lon, 50)):
        raise HydroScreenError(
            "The selected DEM does not cover this bridge location. Choose a point inside the DEM or upload a different file."
        )

    check_cancelled(cancel_event)
    clipped = clip_line_to_raster(centerline, dem_path, lon=lon, lat=lat)
    if clipped is None:
        raise HydroScreenError(
            "The river centreline does not overlap the LiDAR window. "
            "Draw it through the DEM overlay around the bridge pin."
        )
    original_len = projected_length_m(centerline)
    clipped_len = projected_length_m(clipped)
    if clipped_len + 1.0 < original_len:
        logging.info(
            "Clipped centreline to the DEM window (%.0f m → %.0f m)",
            original_len,
            clipped_len,
        )
    centerline = clipped
    if cover_full_line:
        along_m = clipped_len

    if cover_full_line:
        reach = centerline
    else:
        reach = centerline_reach(centerline, lon, lat, along_m)
        reach_clipped = clip_line_to_raster(reach, dem_path, lon=lon, lat=lat)
        if reach_clipped is not None:
            reach = reach_clipped

    transects = generate_transects(
        centerline,
        interval=interval,
        n_each_side=n_each_side,
        length_m=length,
        bridge_lon=lon,
        bridge_lat=lat,
        along_m=along_m,
        cover_full_line=cover_full_line,
    )
    if not transects:
        raise HydroScreenError("No transects could be generated along the river centreline.")
    logging.info("Generated %d transects", len(transects))
    check_cancelled(cancel_event)

    summary = []
    excel_rows = []
    data_rows = []
    transect_features = []
    point_id = 1
    with rasterio.open(dem_path) as dem_src:
        slope, cl_dists, cl_elevs, fit_slope, fit_intercept = centerline_slope_and_profile(
            dem_path, reach, spacing_m=max(sample_spacing, 5.0), src=dem_src
        )
        if slope is None:
            raise HydroScreenError(
                "Could not estimate river slope from the centreline DEM samples. "
                "Draw the river through the bridge pin so it sits on the LiDAR window."
            )
        logging.info("Estimated centreline slope S=%.6f", slope)
        origin_m, line_len_m = centerline_bridge_station_m(reach, lon, lat)
        cl_json = _profile_json(cl_dists, cl_elevs)
        cl_json["origin_m"] = float(origin_m)
        cl_json["length_m"] = float(cl_dists[-1]) if len(cl_dists) else float(line_len_m)
        cl_json["slope"] = float(slope)
        cl_json["fit_slope"] = float(fit_slope) if fit_slope is not None else None
        cl_json["fit_intercept"] = float(fit_intercept) if fit_intercept is not None else None

        for i, (tran, offset) in enumerate(transects):
            check_cancelled(cancel_event)
            dists, elevs, sample_coords = sample_dem_along_line(
                dem_path, tran, spacing_m=sample_spacing, src=dem_src
            )
            aris = {}
            plot_levels = []
            primary_stats = None
            for scenario in scenarios:
                stats = solve_water_level(
                    dists,
                    elevs,
                    flow_m3_s=scenario["flow_m3_s"],
                    mannings_n=mannings_n,
                    slope=slope,
                    cancel_event=cancel_event,
                )
                slim = _ari_hydraulics(stats, scenario)
                aris[scenario["key"]] = slim
                plot_levels.append({
                    "water_level_m": stats.get("water_level_m"),
                    "color": scenario["color"],
                    "label": scenario["label"],
                })
                if primary_stats is None:
                    primary_stats = stats
            df = pd.DataFrame({
                "distance_m": dists,
                "longitude": [xy[0] for xy in sample_coords],
                "latitude": [xy[1] for xy in sample_coords],
                "elevation_m": elevs,
            })
            csv_path = outdir / f"transect_{i+1}.csv"
            df.to_csv(csv_path, index=False)
            png_path = outdir / f"transect_{i+1}.png"
            plot_cross_section(
                dists,
                elevs,
                png_path,
                water_level=primary_stats.get("water_level_m"),
                water_levels=plot_levels,
            )
            stats = dict(primary_stats)
            stats.update({
                "transect": i + 1,
                "offset_m": float(offset),
                "n_samples": int(len(dists)),
                "sample_spacing_m": float(sample_spacing),
                "csv": str(csv_path),
                "plot": str(png_path),
                "aris": aris,
            })
            summary.append(stats)
            excel_row = {key: value for key, value in stats.items() if key != "aris"}
            if len(scenarios) > 1:
                for key, slim in aris.items():
                    excel_row[f"{key}_water_level_m"] = slim.get("water_level_m")
                    excel_row[f"{key}_velocity_m_s"] = slim.get("velocity_m_s")
                    excel_row[f"{key}_discharge_m3_s"] = slim.get("discharge_m3_s")
            excel_rows.append(excel_row)
            for dist, elev in zip(dists, elevs):
                data_rows.append({
                    "ID": point_id,
                    "Transect ID": i + 1,
                    "Distance_m": float(dist),
                    "Elevation_m": float(elev) if np.isfinite(elev) else np.nan,
                })
                point_id += 1
            sample_preview = sample_coords
            if len(sample_preview) > 80:
                step = max(1, len(sample_preview) // 80)
                sample_preview = sample_preview[::step]
                if sample_preview[-1] != sample_coords[-1]:
                    sample_preview.append(sample_coords[-1])
            profile = _profile_json(dists, elevs)
            mid = tran.interpolate(0.5, normalized=True)
            station_m, _ = centerline_bridge_station_m(reach, mid.x, mid.y)
            transect_features.append({
                "transect": i + 1,
                "offset_m": float(offset),
                "station_m": float(station_m),
                "coords": [[x, y] for x, y in tran.coords],
                "samples": [[x, y] for x, y in sample_preview],
                "n_samples": int(len(dists)),
                "distance_m": profile["distance_m"],
                "elevation_m": profile["elevation_m"],
                "water_level_m": primary_stats.get("water_level_m"),
                "velocity_m_s": primary_stats.get("velocity_m_s"),
                "aris": aris,
                "csv": csv_path.name,
                "plot": png_path.name,
            })

    check_cancelled(cancel_event)
    summary_path = outdir / "summary.xlsx"
    write_screening_workbook(summary_path, excel_rows, data_rows)
    logging.info("Wrote summary workbook to %s (%d sample points)", summary_path, len(data_rows))

    if temp_dir:
        logging.info("Temporary DEM stored in %s", temp_dir)
    logging.info("Done")

    return {
        "lat": lat,
        "lon": lon,
        "outdir": str(outdir),
        "summary": summary,
        "summary_xlsx": summary_path.name,
        "centerline": [[x, y] for x, y in centerline.coords],
        "centerline_profile": cl_json,
        "transects": transect_features,
        "centerline_source": centerline_source,
        "used_synthetic_centerline": centerline_source == "synthetic",
        "layout": {
            "along_m": float(along_m) if along_m is not None else None,
            "interval_m": float(interval),
            "transect_length_m": float(length),
            "sample_spacing_m": float(sample_spacing),
            "n_transects": len(transects),
            "flow_m3_s": float(primary_flow),
            "flow_scenarios": scenarios,
            "mannings_n": float(mannings_n),
            "slope": float(slope),
            "fit_slope": float(fit_slope) if fit_slope is not None else None,
            "fit_intercept": float(fit_intercept) if fit_intercept is not None else None,
            "dem_source": dem_source_used,
            "dem_layer": LINZ_LIDAR_1M_LAYER if dem_source_used == "linz-lidar-1m" else None,
            "dem_buffer_m": float(buffer_m),
            "dem_file": kept_dem.name if kept_dem else None,
            "clip_size_m": float(DEM_CLIP_SIZE_M),
            "tiles": list(linz_tiles),
        },
        "temp_dem": temp_dir,
    }


def main():
    parser = argparse.ArgumentParser(description='Hydraulic screening MVP')
    parser.add_argument('--lat', type=float, required=True, help='Bridge latitude in decimal degrees')
    parser.add_argument('--lon', type=float, required=True, help='Bridge longitude in decimal degrees')
    parser.add_argument('--dem', type=str, default=None, help='Path to local DEM GeoTIFF. If omitted, clips the New Zealand LiDAR 1m DEM (LINZ layer 121859).')
    parser.add_argument('--wcs_base', type=str, default=None, help='LINZ WCS base URL (optional)')
    parser.add_argument('--wcs_layer', type=str, default=None, help='WCS layer/coverage id (optional)')
    parser.add_argument('--outdir', type=str, default='outputs', help='Output directory')
    parser.add_argument('--buffer', type=float, default=200.0, help='Buffer around point for DEM download (m) (min 200 m enforced)')
    parser.add_argument('--interval', type=float, default=50.0, help='Spacing between transects along the river (m)')
    parser.add_argument('--along', type=float, default=300.0, help='Total length along the river to cover, centred on the bridge (m)')
    parser.add_argument('--n_each_side', type=int, default=None, help='Deprecated: number of transects each side of the bridge')
    parser.add_argument('--length', type=float, default=200.0, help='Length of each transect across the river (m)')
    parser.add_argument('--sample_spacing', type=float, default=1.0, help='Spacing of DEM sample points along each transect (m)')
    parser.add_argument('--flow', type=float, default=10.0, help='Design flow rate Q (m^3/s)')
    parser.add_argument('--mannings_n', type=float, default=0.035, help="Manning's n")
    args = parser.parse_args()
    along_m = args.along
    if args.n_each_side is not None:
        along_m = 2.0 * args.n_each_side * args.interval
    try:
        run_screening(
            lat=args.lat,
            lon=args.lon,
            dem=args.dem,
            wcs_base=args.wcs_base,
            wcs_layer=args.wcs_layer,
            outdir=args.outdir,
            buffer=args.buffer,
            interval=args.interval,
            n_each_side=args.n_each_side if args.n_each_side is not None else 3,
            length=args.length,
            along_m=along_m,
            sample_spacing=args.sample_spacing,
            mannings_n=args.mannings_n,
            flow_m3_s=args.flow,
        )
    except HydroScreenError as exc:
        logging.error("%s", exc)
        sys.exit(1)

if __name__ == '__main__':
    main()
