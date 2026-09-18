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
  the specified flow. Only the wet interval connected to the main channel
  counts; isolated depressions below the water surface are ignored.

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
from shapely.geometry import Point, LineString, Polygon, box, mapping
from shapely.ops import split, nearest_points, substring
from pyproj import Transformer

try:
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.merge import merge as raster_merge
    from rasterio.transform import from_origin, from_bounds
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.io import MemoryFile
except Exception as e:
    print("rasterio required. Install with: pip install rasterio")
    raise

from matplotlib.colors import LightSource
from PIL import Image
from excel_report import write_screening_workbook

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
AOI_DEFAULT_M = DEM_CLIP_SIZE_M / 2.0
AOI_MIN_M = 10.0
AOI_MAX_M = 5000.0
CENTERLINE_END_BUFFER_M = 50.0
MAX_DEM_RADIUS_M = DEM_CLIP_SIZE_M / 2.0
DEM_PREVIEW_MAX_PX = 360
DEM_MAX_PIXELS = 20_000_000
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


def parse_aoi_extent(value, default=None, name="extent"):
    """Parse an upstream, downstream, or lateral distance in metres."""
    if value is None or value == "":
        return float(AOI_DEFAULT_M if default is None else default)
    try:
        extent_m = float(value)
    except (TypeError, ValueError) as exc:
        raise HydroScreenError(f"{name} must be a number.") from exc
    if not math.isfinite(extent_m) or extent_m < AOI_MIN_M or extent_m > AOI_MAX_M:
        raise HydroScreenError(
            f"{name} must be between {int(AOI_MIN_M)} and {int(AOI_MAX_M)} m."
        )
    return extent_m


def aoi_native_pixels(along_m, width_m, resolution=1.0):
    """How many cells a window uses at the given ground resolution."""
    step = max(float(resolution or 1.0), 1e-6)
    cols = max(1, int(math.ceil(float(width_m) / step)))
    rows = max(1, int(math.ceil(float(along_m) / step)))
    return rows * cols


def preview_resolution_m(along_m, width_m, max_px=DEM_PREVIEW_MAX_PX):
    """Ground sample distance so a map preview stays around max_px on a side."""
    longest = max(float(along_m), float(width_m), 1.0)
    return max(1.0, longest / float(max_px))


def _unit_vector(dx, dy, fallback=(0.0, -1.0)):
    mag = math.hypot(float(dx), float(dy))
    if mag < 1e-9:
        return (float(fallback[0]), float(fallback[1]))
    return (float(dx) / mag, float(dy) / mag)


def aoi_downstream_unit_2193(lat, lon, centerline_coords=None):
    """NZTM unit vector pointing downstream.

    Centreline forward (increasing station) is downstream. With no line, downstream
    is NZTM south so the default 250/250/250 extents make a north-up 500 m square.
    """
    if not centerline_coords:
        return (0.0, -1.0)
    to_2193 = _transformer("EPSG:4326", "EPSG:2193")
    bx, by = to_2193.transform(float(lon), float(lat))
    pts = []
    for pair in centerline_coords:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            pts.append(to_2193.transform(float(pair[0]), float(pair[1])))
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        return (0.0, -1.0)
    best = None
    for index in range(len(pts) - 1):
        x1, y1 = pts[index]
        x2, y2 = pts[index + 1]
        dx, dy = x2 - x1, y2 - y1
        seg_len2 = dx * dx + dy * dy
        if seg_len2 < 1e-12:
            continue
        t = ((bx - x1) * dx + (by - y1) * dy) / seg_len2
        t = max(0.0, min(1.0, t))
        px, py = x1 + t * dx, y1 + t * dy
        dist2 = (px - bx) ** 2 + (py - by) ** 2
        if best is None or dist2 < best[0]:
            best = (dist2, dx, dy)
    if best is None:
        return (0.0, -1.0)
    return _unit_vector(best[1], best[2])


def _lonlat_from_2193(x, y):
    lon, lat = _transformer("EPSG:2193", "EPSG:4326").transform(float(x), float(y))
    return [float(lon), float(lat)]


def _centerline_pairs_2193(coords):
    """Parse [[lon, lat], ...] to parallel lon/lat and NZTM vertex lists."""
    to_2193 = _transformer("EPSG:4326", "EPSG:2193")
    lonlats = []
    pts = []
    for pair in coords or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            lon = float(pair[0])
            lat = float(pair[1])
        except (TypeError, ValueError):
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        lonlats.append((lon, lat))
        pts.append(to_2193.transform(lon, lat))
    return lonlats, pts


def _segment_unit_2193(pts, reverse=False):
    """Unit vector of the first (or last) non-zero NZTM segment."""
    if reverse:
        indices = range(len(pts) - 2, -1, -1)
    else:
        indices = range(len(pts) - 1)
    for index in indices:
        dx = pts[index + 1][0] - pts[index][0]
        dy = pts[index + 1][1] - pts[index][1]
        if math.hypot(dx, dy) >= 1e-6:
            return _unit_vector(dx, dy)
    return None


def extend_centerline_ends(coords, extra_m=CENTERLINE_END_BUFFER_M):
    """Return centreline vertices with extra_m beyond the first and last points.

    The first drawn point is treated as upstream and is extended against the
    first NZTM segment. The last point is downstream and is extended along the
    last segment. Extensions follow the channel, not the map axes.
    """
    extra_m = float(extra_m) if extra_m is not None else CENTERLINE_END_BUFFER_M
    lonlats, pts = _centerline_pairs_2193(coords)
    copied = [[lon, lat] for lon, lat in lonlats]
    if extra_m <= 0 or len(pts) < 2:
        return copied
    start_unit = _segment_unit_2193(pts, reverse=False)
    end_unit = _segment_unit_2193(pts, reverse=True)
    if start_unit is None or end_unit is None:
        return copied
    x0, y0 = pts[0]
    x1, y1 = pts[-1]
    upstream = _lonlat_from_2193(x0 - start_unit[0] * extra_m, y0 - start_unit[1] * extra_m)
    downstream = _lonlat_from_2193(x1 + end_unit[0] * extra_m, y1 + end_unit[1] * extra_m)
    return [upstream] + copied + [downstream]


def apply_centerline_end_buffers(
    lat,
    lon,
    coords,
    upstream_m=None,
    downstream_m=None,
    extra_m=CENTERLINE_END_BUFFER_M,
    snap_m=None,
):
    """Extend the drawn centreline and grow along-channel AOI limits to cover it.

    Returns (extended_coords, upstream_m, downstream_m). User extents are kept
    when they already cover the buffered line; each side is capped at AOI_MAX_M.
    Distances are measured from the same snapped NZTM origin used by bridge_aoi.
    """
    upstream_m = parse_aoi_extent(upstream_m, name="Upstream limit")
    downstream_m = parse_aoi_extent(downstream_m, name="Downstream limit")
    extended = extend_centerline_ends(coords, extra_m=extra_m)
    if len(extended) < 2:
        return extended, upstream_m, downstream_m
    snap_m = DEM_CLIP_SNAP_M if snap_m is None else float(snap_m)
    to_2193 = _transformer("EPSG:4326", "EPSG:2193")
    origin_x, origin_y = to_2193.transform(float(lon), float(lat))
    if snap_m > 0:
        origin_x = round(origin_x / snap_m) * snap_m
        origin_y = round(origin_y / snap_m) * snap_m
    down_x, down_y = aoi_downstream_unit_2193(lat, lon, coords)
    signed = []
    for pair in extended:
        px, py = to_2193.transform(float(pair[0]), float(pair[1]))
        signed.append((px - origin_x) * down_x + (py - origin_y) * down_y)
    if not signed:
        return extended, upstream_m, downstream_m
    # Keep the extra vertices inside the LiDAR crop (snap + 1 m cells).
    pad_m = 2.0
    need_up = max(0.0, -min(signed)) + pad_m
    need_down = max(0.0, max(signed)) + pad_m
    upstream_m = min(AOI_MAX_M, max(upstream_m, need_up))
    downstream_m = min(AOI_MAX_M, max(downstream_m, need_down))
    return extended, upstream_m, downstream_m


def bridge_aoi(
    lat,
    lon,
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
    snap_m=None,
):
    """Rectangle around the bridge pin from upstream, downstream, and lateral extents.

    The pin is the centre. Edge midpoints (upstream, downstream, left, right looking
    downstream) define a rectangle whose corners are connected as the area of interest.
    """
    upstream_m = parse_aoi_extent(upstream_m, name="Upstream limit")
    downstream_m = parse_aoi_extent(downstream_m, name="Downstream limit")
    lateral_m = parse_aoi_extent(lateral_m, name="Lateral extent")
    snap_m = DEM_CLIP_SNAP_M if snap_m is None else float(snap_m)
    x, y = _transformer("EPSG:4326", "EPSG:2193").transform(float(lon), float(lat))
    if snap_m > 0:
        x = round(x / snap_m) * snap_m
        y = round(y / snap_m) * snap_m
    down_x, down_y = aoi_downstream_unit_2193(lat, lon, centerline_coords)
    right_x, right_y = down_y, -down_x
    up_pt = (x - down_x * upstream_m, y - down_y * upstream_m)
    down_pt = (x + down_x * downstream_m, y + down_y * downstream_m)
    left_pt = (x - right_x * lateral_m, y - right_y * lateral_m)
    right_pt = (x + right_x * lateral_m, y + right_y * lateral_m)
    ul = (up_pt[0] - right_x * lateral_m, up_pt[1] - right_y * lateral_m)
    ur = (up_pt[0] + right_x * lateral_m, up_pt[1] + right_y * lateral_m)
    dr = (down_pt[0] + right_x * lateral_m, down_pt[1] + right_y * lateral_m)
    dl = (down_pt[0] - right_x * lateral_m, down_pt[1] - right_y * lateral_m)
    ring_2193 = [ul, ur, dr, dl, ul]
    xs = [pt[0] for pt in ring_2193[:-1]]
    ys = [pt[1] for pt in ring_2193[:-1]]
    bounds_2193 = (min(xs), min(ys), max(xs), max(ys))
    ring_4326 = [_lonlat_from_2193(px, py) for px, py in ring_2193]
    along_m = upstream_m + downstream_m
    width_m = 2.0 * lateral_m
    return {
        "lat": float(lat),
        "lon": float(lon),
        "origin_2193": (float(x), float(y)),
        "downstream_unit_2193": (float(down_x), float(down_y)),
        "upstream_m": float(upstream_m),
        "downstream_m": float(downstream_m),
        "lateral_m": float(lateral_m),
        "along_m": float(along_m),
        "width_m": float(width_m),
        "clip_size_m": float(max(along_m, width_m)),
        "points_2193": {
            "upstream": (float(up_pt[0]), float(up_pt[1])),
            "downstream": (float(down_pt[0]), float(down_pt[1])),
            "left": (float(left_pt[0]), float(left_pt[1])),
            "right": (float(right_pt[0]), float(right_pt[1])),
        },
        "corners_2193": {
            "ul": (float(ul[0]), float(ul[1])),
            "ur": (float(ur[0]), float(ur[1])),
            "dr": (float(dr[0]), float(dr[1])),
            "dl": (float(dl[0]), float(dl[1])),
        },
        "ring_2193": [(float(px), float(py)) for px, py in ring_2193],
        "ring_4326": ring_4326,
        "points_4326": {
            "upstream": _lonlat_from_2193(*up_pt),
            "downstream": _lonlat_from_2193(*down_pt),
            "left": _lonlat_from_2193(*left_pt),
            "right": _lonlat_from_2193(*right_pt),
        },
        "bounds_2193": tuple(float(v) for v in bounds_2193),
        "bbox_4326": bbox_4326_from_2193(bounds_2193),
    }


def aoi_polygon_2193(aoi):
    """Shapely polygon of an AOI ring in NZTM."""
    ring = aoi.get("ring_2193") if isinstance(aoi, dict) else aoi
    return Polygon(ring)


def aoi_leaflet(aoi):
    """Leaflet-friendly lat/lng ring and labelled edge points."""
    def latlng(pair):
        lon, lat = pair
        return [float(lat), float(lon)]

    return {
        "ring": [latlng(pt) for pt in aoi["ring_4326"]],
        "points": {name: latlng(pt) for name, pt in aoi["points_4326"].items()},
        "upstream_m": aoi["upstream_m"],
        "downstream_m": aoi["downstream_m"],
        "lateral_m": aoi["lateral_m"],
    }


def _cache_file_for_aoi(aoi, resolution=None):
    res = 0 if resolution is None else round(float(resolution), 2)
    ox, oy = (round(float(v), 1) for v in aoi["origin_2193"])
    dx, dy = (round(float(v), 4) for v in aoi["downstream_unit_2193"])
    up = round(float(aoi["upstream_m"]), 1)
    down = round(float(aoi["downstream_m"]), 1)
    lateral = round(float(aoi["lateral_m"]), 1)
    name = f"aoi_{ox:.1f}_{oy:.1f}_{dx:.4f}_{dy:.4f}_{up:.1f}_{down:.1f}_{lateral:.1f}_{res:g}.tif"
    return dem_cache_dir() / name


def _vsicurl_uri(url):
    if url.startswith("/vsicurl/") or url.startswith("/vsi"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return f"/vsicurl/{url}"
    return url


def linz_window_uris(plan):
    """Windowed HTTPS COGs, or a complete local sheet if one is already on disk."""
    uris = []
    for url, path in zip(plan.get("urls") or [], plan.get("paths") or []):
        local = Path(path)
        if local.exists() and local.stat().st_size > 256:
            uris.append(str(local))
        else:
            uris.append(_vsicurl_uri(url))
    return uris

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
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "120")
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


def plan_linz_clip(
    lat,
    lon,
    size_m=None,
    snap_m=None,
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
):
    """Choose LINZ 1 m tiles for the AOI rectangle around a bridge pin.

    Methodology:
    1. Project the pin to NZTM (EPSG:2193) and snap it so nearby clicks share a window.
    2. Build a rectangle from upstream, downstream, and lateral extents about the pin.
       Heading follows the centreline (forward = downstream); otherwise NZTM north-up.
    3. Convert that rectangle's envelope to a WGS84 bbox.
    4. Intersect the bbox with the bundled Topo50 index for LINZ layer 121859.
    5. Return sheet codes, public HTTPS COG URLs, and the AOI polygon used to crop.

    Intersecting COGs are windowed over HTTPS to the AOI. A complete local sheet in
    outputs/linz-tiles/ is reused when already on disk; whole tiles are not downloaded.
    """
    if size_m is not None and upstream_m is None and downstream_m is None and lateral_m is None:
        half = float(size_m) / 2.0
        upstream_m = downstream_m = lateral_m = half
    aoi = bridge_aoi(
        lat,
        lon,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
        snap_m=snap_m,
    )
    bounds_2193 = aoi["bounds_2193"]
    bbox_4326 = aoi["bbox_4326"]
    tiles = linz_tiles_for_bbox(bbox_4326)
    urls = [linz_tile_url(code) for code in tiles]
    paths = [str(linz_tile_path(code)) for code in tiles]
    return {
        "lat": float(lat),
        "lon": float(lon),
        "clip_size_m": float(aoi["clip_size_m"]),
        "upstream_m": float(aoi["upstream_m"]),
        "downstream_m": float(aoi["downstream_m"]),
        "lateral_m": float(aoi["lateral_m"]),
        "along_m": float(aoi["along_m"]),
        "width_m": float(aoi["width_m"]),
        "origin_2193": aoi["origin_2193"],
        "downstream_unit_2193": aoi["downstream_unit_2193"],
        "bounds_2193": tuple(float(v) for v in bounds_2193),
        "bbox_4326": tuple(float(v) for v in bbox_4326),
        "ring_2193": aoi["ring_2193"],
        "ring_4326": aoi["ring_4326"],
        "points_4326": aoi["points_4326"],
        "aoi": aoi_leaflet(aoi),
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


def iter_extract_linz_dem_for_bridge(
    lat,
    lon,
    out_tif,
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
    resolution=None,
):
    """Identify intersecting COGs and crop the AOI window; do not download whole tiles."""
    yield {"percent": 2, "message": "Finding DEM tiles"}
    plan = plan_linz_clip(
        lat,
        lon,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
    )
    if not plan["tiles"]:
        raise HydroScreenError(
            "This site is outside the New Zealand LiDAR 1m DEM coverage "
            f"({LINZ_LIDAR_1M_LAYER})."
        )
    step = None if resolution is None else float(resolution)
    pixels = aoi_native_pixels(plan["along_m"], plan["width_m"], step or 1.0)
    if step is None and pixels > DEM_MAX_PIXELS:
        raise HydroScreenError(
            f"This LiDAR window is about {plan['along_m']:.0f} m along × "
            f"{plan['width_m']:.0f} m across ({pixels / 1e6:.1f} million 1 m cells). "
            f"That is too large to crop. Each of upstream, downstream, and lateral "
            f"must be between {int(AOI_MIN_M)} and {int(AOI_MAX_M)} m. "
            "1,500 m × 1,000 m is 750 m up, 750 m down, and 500 m each side."
        )
    logging.info(
        "Bridge %.5f, %.5f uses LINZ 1m sheets %s (AOI %.0f m along × %.0f m wide%s)",
        plan["lat"],
        plan["lon"],
        ", ".join(plan["tiles"]),
        plan["along_m"],
        plan["width_m"],
        "" if step is None else f", {step:.1f} m preview cells",
    )
    sheets = ", ".join(plan["tiles"])
    yield {
        "percent": 8,
        "message": f"Cropping {sheets} to the area of interest",
        "plan": plan,
    }

    cache_file = _cache_file_for_aoi(plan, resolution=step)
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

    uris = linz_window_uris(plan)
    along = float(plan["along_m"])
    wide = float(plan["width_m"])
    yield {
        "percent": 12,
        "message": f"Clipping a {along:.0f} × {wide:.0f} m DEM window",
        "plan": plan,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    clip_dem_tiles(
        uris,
        plan["bounds_2193"],
        str(out_path),
        geometry=aoi_polygon_2193(plan),
        resolution=step,
    )
    with _CACHE_LOCK:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            if out_path.resolve() != cache_file.resolve():
                shutil.copyfile(out_path, cache_file)
            _prune_dem_cache()
        except OSError as exc:
            logging.warning("Could not cache LiDAR clip: %s", exc)
    yield {"percent": 100, "message": "DEM ready", "path": str(out_path), "plan": plan}


def extract_linz_dem_for_bridge(
    lat,
    lon,
    out_tif,
    progress=None,
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
    resolution=None,
):
    """Crop intersecting LINZ COGs to the AOI rectangle. Returns (path, plan)."""
    path = None
    plan = None
    for event in iter_extract_linz_dem_for_bridge(
        lat,
        lon,
        out_tif,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
        resolution=resolution,
    ):
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


def clip_dem_tiles(
    tile_uris,
    bounds_2193,
    out_tif,
    nodata=-9999.0,
    resolution=None,
    progress=None,
    geometry=None,
):
    """Mosaic windowed reads from GeoTIFF URIs into an NZTM clip.

    When geometry is set (a Shapely polygon or GeoJSON mapping in EPSG:2193),
    pixels outside that polygon are written as nodata.
    """
    _configure_gdal_http()
    west, south, east, north = bounds_2193
    if east <= west or north <= south:
        raise HydroScreenError("Invalid DEM clip window.")
    datasets = []
    try:
        _emit_progress(progress, 0.04, "Opening DEM tiles")
        for uri in tile_uris:
            open_uri = _vsicurl_uri(str(uri))
            try:
                datasets.append(rasterio.open(open_uri))
            except Exception as exc:
                if str(open_uri).startswith("/vsicurl/") or str(uri).startswith("http"):
                    raise HydroScreenError(
                        "Could not open the New Zealand LiDAR 1m DEM. "
                        f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                    ) from exc
                raise
        if not datasets:
            raise HydroScreenError("No DEM tiles were available to clip.")
        _emit_progress(progress, 0.12, "Cropping DEM")
        merge_kw = {
            "bounds": (west, south, east, north),
            "nodata": nodata,
        }
        if resolution is not None:
            merge_kw["res"] = (float(resolution), float(resolution))
        try:
            mosaic, transform = raster_merge(datasets, **merge_kw)
        except MemoryError as exc:
            raise HydroScreenError(
                "This DEM window is too large to crop in memory. "
                f"Each of upstream, downstream, and lateral can be at most {int(AOI_MAX_M)} m. "
                "A 1,500 m × 1,000 m window is 750 m up, 750 m down, and 500 m each side."
            ) from exc
        except Exception as exc:
            logging.exception("DEM tile merge failed")
            raise HydroScreenError(
                "Could not crop the New Zealand LiDAR 1m DEM for this window. "
                "Check the area of interest size (max 5,000 m per side) and try again."
            ) from exc
        data = mosaic[0]
        if geometry is not None:
            geom = mapping(geometry) if hasattr(geometry, "__geo_interface__") else geometry
            outside = geometry_mask(
                [geom],
                out_shape=data.shape,
                transform=transform,
                invert=False,
            )
            data = np.array(data, copy=True)
            data[outside] = nodata
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
    """Window intersecting LINZ COGs to bbox. Does not download whole Topo50 sheets.

    When bounds_2193 is set, the GeoTIFF is clipped to that exact NZTM window
    and is not grown for cache snapping. A complete local sheet is reused when
    already on disk.
    """
    if bounds_2193 is None:
        bbox = expand_bbox_for_cache(bbox)
    cache_file = _cache_file_for(bbox, resolution, bounds_2193=bounds_2193)
    out_path = Path(out_tif)

    _emit_progress(progress, 0.04, "Finding DEM tiles")
    with _CACHE_LOCK:
        if cache_file.exists() and cache_file.stat().st_size > 256:
            logging.info("Reusing cached New Zealand LiDAR clip %s", cache_file.name)
            _emit_progress(progress, 0.9, "Reusing cached DEM")
            if out_path.resolve() != cache_file.resolve():
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cache_file, out_path)
            _emit_progress(progress, 1.0, "DEM ready")
            return str(out_path)

    codes = linz_tiles_for_bbox(bbox)
    if not codes:
        raise HydroScreenError(
            "This site is outside the New Zealand LiDAR 1m DEM coverage "
            f"({LINZ_LIDAR_1M_LAYER})."
        )
    logging.info(
        "Cropping New Zealand LiDAR 1m DEM sheets %s to the requested window",
        ", ".join(codes),
    )
    uris = []
    for code in codes:
        local = linz_tile_path(code)
        if local.exists() and local.stat().st_size > 256:
            uris.append(str(local))
        else:
            uris.append(_vsicurl_uri(linz_tile_url(code)))

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
        _emit_progress(progress, 0.12 + 0.88 * float(frac), message)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    clip_dem_tiles(
        uris,
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


def screening_dem_radius_m(buffer, along_m, length, upstream_m=None, downstream_m=None, lateral_m=None):
    """Half-extent of the site DEM clip (largest of upstream, downstream, lateral)."""
    up = parse_aoi_extent(upstream_m)
    down = parse_aoi_extent(downstream_m)
    side = parse_aoi_extent(lateral_m)
    return max(up, down, side)


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


def iter_dem_preview(
    lat,
    lon,
    dem_path=None,
    along_m=300.0,
    length=200.0,
    buffer=200.0,
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
):
    """Yield progress events, then a final overlay result with png/bounds."""
    _ = (along_m, length, buffer)
    yield {"percent": 0, "message": "Finding DEM tiles"}
    plan = plan_linz_clip(
        lat,
        lon,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
    )
    bounds_2193 = plan["bounds_2193"]
    bbox = plan["bbox_4326"]
    radius = screening_dem_radius_m(
        buffer, along_m, length,
        upstream_m=plan["upstream_m"],
        downstream_m=plan["downstream_m"],
        lateral_m=plan["lateral_m"],
    )
    temp_dir = None
    src_path = dem_path
    source = "local"
    linz_tiles = []
    aoi = plan.get("aoi")
    try:
        if src_path is None:
            temp_dir = tempfile.mkdtemp(prefix="hydroscreen_preview_")
            src_path = os.path.join(temp_dir, "dem.tif")
            used = None
            preview_step = preview_resolution_m(plan["along_m"], plan["width_m"])
            try:
                for event in iter_extract_linz_dem_for_bridge(
                    lat,
                    lon,
                    src_path,
                    upstream_m=upstream_m,
                    downstream_m=downstream_m,
                    lateral_m=lateral_m,
                    centerline_coords=centerline_coords,
                    resolution=preview_step,
                ):
                    yield {
                        "percent": int(event.get("percent") or 0),
                        "message": event.get("message") or "Cropping DEM…",
                    }
                    if event.get("path"):
                        src_path = event["path"]
                    if event.get("plan"):
                        used = event["plan"]
            except HydroScreenError:
                raise
            except Exception as exc:
                logging.exception("LINZ 1m DEM crop failed")
                raise HydroScreenError(
                    "Could not crop the New Zealand LiDAR 1m DEM. "
                    f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                ) from exc
            source = "linz-lidar-1m"
            linz_tiles = list((used or plan).get("tiles") or [])
            aoi = (used or plan).get("aoi") or aoi
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
            "clip_size_m": float(plan["clip_size_m"]),
            "upstream_m": float(plan["upstream_m"]),
            "downstream_m": float(plan["downstream_m"]),
            "lateral_m": float(plan["lateral_m"]),
            "tiles": list(linz_tiles),
            "aoi": aoi,
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
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
):
    """Build a map overlay PNG of the DEM HydroBridge will sample at this site."""
    result = None
    for event in iter_dem_preview(
        lat,
        lon,
        dem_path=dem_path,
        along_m=along_m,
        length=length,
        buffer=buffer,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
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
                "upstream_m": event.get("upstream_m"),
                "downstream_m": event.get("downstream_m"),
                "lateral_m": event.get("lateral_m"),
                "tiles": event.get("tiles") or [],
                "aoi": event.get("aoi"),
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
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    centerline_coords=None,
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
            dem_path, _plan = extract_linz_dem_for_bridge(
                clip_lat,
                clip_lon,
                dem_path,
                upstream_m=upstream_m,
                downstream_m=downstream_m,
                lateral_m=lateral_m,
                centerline_coords=centerline_coords,
            )
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

def _empty_hydraulics():
    return {
        "area_m2": 0.0,
        "wetted_perimeter_m": 0.0,
        "top_width_m": 0.0,
        "hydraulic_radius_m": 0.0,
        "connected_spans": [],
        "connected_bed_m": None,
    }


def default_channel_station_m(dists):
    """Station of the main-channel seed: the transect midpoint (centreline crossing)."""
    finite = [float(x) for x in np.asarray(dists, dtype=float) if np.isfinite(x)]
    if not finite:
        return 0.0
    return 0.5 * (finite[0] + finite[-1])


def main_channel_bed_m(dists, elevs, channel_station_m=None):
    """Lowest bed in a window around the main-channel station, not a far floodplain pit."""
    dists = np.asarray(dists, dtype=float)
    elevs = np.asarray(elevs, dtype=float)
    if len(dists) == 0:
        return None
    seed_x = (
        float(channel_station_m)
        if channel_station_m is not None and np.isfinite(channel_station_m)
        else default_channel_station_m(dists)
    )
    finite = np.isfinite(dists) & np.isfinite(elevs)
    if not finite.any():
        return None
    span = float(np.nanmax(dists) - np.nanmin(dists)) if finite.sum() else 0.0
    window = max(5.0, 0.25 * span)
    nearby = finite & (np.abs(dists - seed_x) <= window)
    if not nearby.any():
        nearby = finite
    return float(np.min(elevs[nearby]))


def _sample_is_wet(elev, water_level):
    return np.isfinite(elev) and (float(water_level) - float(elev)) > 0.0


def _waterline_station(x1, z1, x2, z2, water_level):
    if not (np.isfinite(x1) and np.isfinite(x2) and np.isfinite(z1) and np.isfinite(z2)):
        return None
    d1 = float(water_level) - float(z1)
    d2 = float(water_level) - float(z2)
    if d1 > 0 and d2 > 0:
        return None
    if d1 <= 0 and d2 <= 0:
        return None
    if z2 == z1:
        return float(x1)
    t = (float(water_level) - float(z1)) / (float(z2) - float(z1))
    t = min(max(t, 0.0), 1.0)
    return float(x1 + t * (x2 - x1))


def connected_wetted_span(dists, elevs, water_level, channel_station_m=None):
    """Wet sample range hydraulically connected to the main channel.

    High ground at or above the water surface is a barrier, so a floodplain
    depression that the waterline intersects is ignored unless the intervening
    ridge is overtopped. The main channel is the transect midpoint (the
    centreline crossing) unless *channel_station_m* is set.

    Returns a dict with inclusive sample indices and bank stations, or None
    when the channel is dry.
    """
    dists = np.asarray(dists, dtype=float)
    elevs = np.asarray(elevs, dtype=float)
    n = len(dists)
    if n == 0 or not np.isfinite(water_level):
        return None
    seed_x = (
        float(channel_station_m)
        if channel_station_m is not None and np.isfinite(channel_station_m)
        else default_channel_station_m(dists)
    )
    wet = [_sample_is_wet(elevs[i], water_level) for i in range(n)]
    seed = None
    best = float("inf")
    for i in range(n):
        if not np.isfinite(dists[i]):
            continue
        gap = abs(float(dists[i]) - seed_x)
        if gap < best:
            best = gap
            seed = i
    if seed is None:
        return None
    if not wet[seed]:
        near = None
        near_d = float("inf")
        near_z = float("inf")
        for i in range(n):
            if not wet[i] or not np.isfinite(dists[i]):
                continue
            gap = abs(float(dists[i]) - seed_x)
            z = float(elevs[i])
            if gap < near_d - 1e-9 or (abs(gap - near_d) <= 1e-9 and z < near_z):
                near = i
                near_d = gap
                near_z = z
        if near is None:
            return None
        seed = near
    i_left = seed
    while i_left > 0 and wet[i_left - 1]:
        i_left -= 1
    i_right = seed
    while i_right < n - 1 and wet[i_right + 1]:
        i_right += 1
    x_left = float(dists[i_left])
    if i_left > 0:
        bank = _waterline_station(
            dists[i_left - 1], elevs[i_left - 1], dists[i_left], elevs[i_left], water_level
        )
        if bank is not None:
            x_left = bank
    x_right = float(dists[i_right])
    if i_right < n - 1:
        bank = _waterline_station(
            dists[i_right], elevs[i_right], dists[i_right + 1], elevs[i_right + 1], water_level
        )
        if bank is not None:
            x_right = bank
    bed = [float(elevs[i]) for i in range(i_left, i_right + 1) if np.isfinite(elevs[i])]
    return {
        "i_left": int(i_left),
        "i_right": int(i_right),
        "x_left": float(x_left),
        "x_right": float(x_right),
        "bed_m": float(min(bed)) if bed else None,
    }


def connected_wet_mask(dists, elevs, water_level, channel_station_m=None):
    """Boolean mask of samples in the channel-connected wet interval."""
    dists = np.asarray(dists, dtype=float)
    mask = np.zeros(len(dists), dtype=bool)
    span = connected_wetted_span(dists, elevs, water_level, channel_station_m=channel_station_m)
    if not span:
        return mask
    mask[span["i_left"] : span["i_right"] + 1] = True
    return mask


def hydraulics_at_stage(dists, elevs, water_level, channel_station_m=None):
    """Trapezoidal area and wetted perimeter at a water-surface elevation.

    Only the wet interval continuously connected to the main channel is
    included. Isolated depressions below the water surface do not add area,
    wetted perimeter, top width, or hydraulic radius.
    """
    empty = _empty_hydraulics()
    if len(dists) < 2:
        return empty
    span = connected_wetted_span(dists, elevs, water_level, channel_station_m=channel_station_m)
    if not span:
        return empty
    i_first = 0 if span["i_left"] == 0 else span["i_left"] - 1
    i_last = len(dists) - 2 if span["i_right"] >= len(dists) - 1 else span["i_right"]
    area = 0.0
    perimeter = 0.0
    top_width = 0.0
    for i in range(i_first, i_last + 1):
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
        "connected_spans": [[span["x_left"], span["x_right"]]],
        "connected_bed_m": span.get("bed_m"),
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
    "connected_spans",
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


def solve_water_level(
    dists,
    elevs,
    flow_m3_s,
    mannings_n,
    slope,
    step_m=0.02,
    cancel_event=None,
    channel_station_m=None,
):
    """Raise water level along the transect until Manning Q matches the specified flow.

    Conveyance uses only the wet interval connected to the main channel.
    """
    finite = elevs[np.isfinite(elevs)]
    empty = {
        "water_level_m": None,
        "max_depth_m": 0.0,
        "width_m": 0.0,
        "area_m2": 0.0,
        "depth_mean_m": 0.0,
        "wetted_perimeter_m": 0.0,
        "hydraulic_radius_m": 0.0,
        "connected_spans": [],
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
    zmin_all = float(np.min(finite))
    zmax = float(np.max(finite))
    z_channel = main_channel_bed_m(dists, elevs, channel_station_m)
    if z_channel is None:
        z_channel = zmin_all
    if flow_m3_s <= 0:
        empty.update({"water_level_m": float(z_channel), "conveys": True, "connected_spans": []})
        return empty

    max_wse = zmax + 2.0
    wse = float(z_channel) + step_m
    hyd = hydraulics_at_stage(dists, elevs, wse, channel_station_m=channel_station_m)
    velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
    # Increase stage until conveyance meets the target flow.
    while discharge < flow_m3_s and wse < max_wse:
        check_cancelled(cancel_event)
        wse += step_m
        hyd = hydraulics_at_stage(dists, elevs, wse, channel_station_m=channel_station_m)
        velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)

    # Refine between the last two steps.
    lo = max(float(z_channel), wse - step_m)
    hi = wse
    for _ in range(24):
        check_cancelled(cancel_event)
        mid = 0.5 * (lo + hi)
        hyd = hydraulics_at_stage(dists, elevs, mid, channel_station_m=channel_station_m)
        velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
        if discharge < flow_m3_s:
            lo = mid
        else:
            hi = mid
    wse = hi
    hyd = hydraulics_at_stage(dists, elevs, wse, channel_station_m=channel_station_m)
    velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)
    width = hyd["top_width_m"]
    area = hyd["area_m2"]
    bed = hyd.get("connected_bed_m")
    if bed is None:
        bed = z_channel
    overtopped = wse >= (zmax - 1e-6)
    conveys = discharge + 1e-6 >= flow_m3_s
    return {
        "water_level_m": float(wse),
        "max_depth_m": float(max(0.0, wse - float(bed))),
        "width_m": float(width),
        "area_m2": float(area),
        "depth_mean_m": float(area / width) if width > 0 else 0.0,
        "wetted_perimeter_m": hyd["wetted_perimeter_m"],
        "hydraulic_radius_m": hyd["hydraulic_radius_m"],
        "connected_spans": hyd.get("connected_spans") or [],
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
            filled = False
            for item in levels:
                level = item.get("water_level_m")
                if level is None or not np.isfinite(level):
                    continue
                color = item.get("color") or "#1f6f8b"
                label = item.get("label") or "Water level"
                wet = connected_wet_mask(dists, elevs, level)
                if wet.any():
                    fill_color = "#2563eb" if len(levels) == 1 else color
                    ax.fill_between(
                        dists,
                        elevs,
                        level,
                        where=wet,
                        color=fill_color,
                        alpha=0.35 if len(levels) == 1 else 0.18,
                        interpolate=True,
                        label="Connected flow area" if not filled else "_nolegend_",
                    )
                    filled = True
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
    upstream_m=None,
    downstream_m=None,
    lateral_m=None,
    bridge_name=None,
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
        extended_coords, upstream_m, downstream_m = apply_centerline_end_buffers(
            lat,
            lon,
            centerline_coords,
            upstream_m=upstream_m,
            downstream_m=downstream_m,
        )
        logging.info(
            "Extended centreline %.0f m upstream and downstream along the channel",
            CENTERLINE_END_BUFFER_M,
        )
        centerline = centerline_from_coords(extended_coords)
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
    plan = plan_linz_clip(
        lat,
        lon,
        upstream_m=upstream_m,
        downstream_m=downstream_m,
        lateral_m=lateral_m,
        centerline_coords=centerline_coords,
    )
    bbox = plan["bbox_4326"]
    buffer_m = screening_dem_radius_m(
        buffer,
        along_m,
        length,
        upstream_m=plan["upstream_m"],
        downstream_m=plan["downstream_m"],
        lateral_m=plan["lateral_m"],
    )

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
                "Cropping New Zealand LiDAR 1m DEM to the area of interest "
                "(%.0f m along × %.0f m wide)",
                plan["along_m"],
                plan["width_m"],
            )
            cache_file = _cache_file_for_aoi(plan)
            try:
                check_cancelled(cancel_event)
                dem_path, plan = extract_linz_dem_for_bridge(
                    lat,
                    lon,
                    str(cache_file),
                    upstream_m=upstream_m,
                    downstream_m=downstream_m,
                    lateral_m=lateral_m,
                    centerline_coords=centerline_coords,
                )
                linz_tiles = plan["tiles"]
                dem_source_used = "linz-lidar-1m"
                check_cancelled(cancel_event)
            except HydroScreenError:
                raise
            except Exception as exc:
                logging.exception("LINZ 1m DEM crop failed")
                raise HydroScreenError(
                    "Could not crop the New Zealand LiDAR 1m DEM. "
                    f"Check network access to LINZ open data ({LINZ_LIDAR_1M_LAYER})."
                ) from exc
            kept_dem = Path(outdir) / "dem_aoi.tif"
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
    workbook_transects = []
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
            sample_preview = sample_coords
            if len(sample_preview) > 80:
                step = max(1, len(sample_preview) // 80)
                sample_preview = sample_preview[::step]
                if sample_preview[-1] != sample_coords[-1]:
                    sample_preview.append(sample_coords[-1])
            profile = _profile_json(dists, elevs)
            mid = tran.interpolate(0.5, normalized=True)
            station_m, _ = centerline_bridge_station_m(reach, mid.x, mid.y)
            stats = dict(primary_stats)
            stats.update({
                "transect": i + 1,
                "offset_m": float(offset),
                "station_m": float(station_m),
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
            workbook_transects.append({
                "transect": i + 1,
                "offset_m": float(offset),
                "station_m": float(station_m),
                "coords": [[x, y] for x, y in tran.coords],
                "distance_m": profile["distance_m"],
                "elevation_m": profile["elevation_m"],
                "longitude": [xy[0] for xy in sample_coords],
                "latitude": [xy[1] for xy in sample_coords],
                "n_samples": int(len(dists)),
                "sample_spacing_m": float(sample_spacing),
                "aris": aris,
            })
            for dist, elev in zip(dists, elevs):
                data_rows.append({
                    "ID": point_id,
                    "Transect ID": i + 1,
                    "Distance_m": float(dist),
                    "Elevation_m": float(elev) if np.isfinite(elev) else np.nan,
                })
                point_id += 1
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
    write_screening_workbook(
        summary_path,
        summary_rows=excel_rows,
        data_rows=data_rows,
        project={
            "bridge_name": bridge_name,
            "lat": lat,
            "lon": lon,
            "centerline_source": centerline_source,
        },
        layout={
            "along_m": float(along_m) if along_m is not None else None,
            "interval_m": float(interval),
            "transect_length_m": float(length),
            "sample_spacing_m": float(sample_spacing),
            "n_transects": len(transects),
            "flow_m3_s": float(primary_flow),
            "mannings_n": float(mannings_n),
            "slope": float(slope),
            "dem_source": dem_source_used,
            "dem_file": kept_dem.name if kept_dem else None,
            "upstream_m": float(plan["upstream_m"]),
            "downstream_m": float(plan["downstream_m"]),
            "lateral_m": float(plan["lateral_m"]),
            "tiles": list(linz_tiles),
            "centerline_source": centerline_source,
        },
        scenarios=scenarios,
        transects=workbook_transects,
        centerline_profile=cl_json,
    )
    logging.info("Wrote summary workbook to %s", summary_path)

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
            "clip_size_m": float(plan["clip_size_m"]),
            "upstream_m": float(plan["upstream_m"]),
            "downstream_m": float(plan["downstream_m"]),
            "lateral_m": float(plan["lateral_m"]),
            "aoi": plan.get("aoi"),
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
    parser.add_argument('--upstream', type=float, default=AOI_DEFAULT_M, help='Upstream DEM extent from the bridge (m)')
    parser.add_argument('--downstream', type=float, default=AOI_DEFAULT_M, help='Downstream DEM extent from the bridge (m)')
    parser.add_argument('--lateral', type=float, default=AOI_DEFAULT_M, help='Left/right DEM extent from the bridge (m)')
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
            interval=args.interval,
            n_each_side=args.n_each_side if args.n_each_side is not None else 3,
            length=args.length,
            along_m=along_m,
            sample_spacing=args.sample_spacing,
            mannings_n=args.mannings_n,
            flow_m3_s=args.flow,
            upstream_m=args.upstream,
            downstream_m=args.downstream,
            lateral_m=args.lateral,
        )
    except HydroScreenError as exc:
        logging.error("%s", exc)
        sys.exit(1)

if __name__ == '__main__':
    main()
