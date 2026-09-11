"""
Hydraulic screening MVP
- Accepts lat/lon or bridge ID (lat/lon for MVP)
- Attempts to download DEM from LINZ WCS (public) if wcs_base and wcs_layer supplied
- Falls back to local DEM if provided with --dem
- Queries OSM (Overpass) for nearby waterway centreline
- Generates transects and samples DEM elevations
- Exports CSV/Excel and plots
- Estimates bed slope from the river centreline, then raises water level on
  each transect (trapezoidal area / hydraulic radius) until Manning Q matches
  the specified flow

Usage examples are in README.md
"""

import argparse
import os
import sys
import math
import tempfile
import shutil
import logging
from pathlib import Path

import requests
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import Point, LineString
from shapely.ops import split, nearest_points
from pyproj import Transformer

try:
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.io import MemoryFile
except Exception as e:
    print("rasterio required. Install with: pip install rasterio")
    raise

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


class HydroScreenError(Exception):
    """Raised when screening cannot run (missing DEM, invalid inputs, etc.)."""

# Utilities

def bbox_from_point(lat, lon, buffer_m):
    """Return bbox (minLon, minLat, maxLon, maxLat) in EPSG:4326 by buffering in meters using WebMercator."""
    # Project to WebMercator for metric buffer
    transformer_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    transformer_to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    x, y = transformer_to_3857.transform(lon, lat)
    minx = x - buffer_m
    maxx = x + buffer_m
    miny = y - buffer_m
    maxy = y + buffer_m
    lon_min, lat_min = transformer_to_4326.transform(minx, miny)
    lon_max, lat_max = transformer_to_4326.transform(maxx, maxy)
    return min(lon_min, lon_max), min(lat_min, lat_max), max(lon_min, lon_max), max(lat_min, lat_max)

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
            transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
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


def find_linz_dem(lat, lon, buffer_m, out_tif):
    """Attempt to discover and download a LINZ DEM that covers the point + buffer.
    Strategy (MVP): use LINZ CKAN API (package_search) to find datasets with DEM/LiDAR, try GeoTIFF resources first, then WCS resources.
    Returns path to downloaded DEM (out_tif) on success, or None.
    """
    bbox = bbox_from_point(lat, lon, buffer_m)
    search_url = "https://data.linz.govt.nz/api/3/action/package_search"
    params = {"q": "NZDEM OR DEM OR LiDAR", "rows": 50}
    headers = {"User-Agent": "hydroscreen/0.1"}
    try:
        r = requests.get(search_url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        res = r.json()
        results = res.get("result", {}).get("results", [])
    except Exception as e:
        logging.warning("LINZ package_search failed: %s", e)
        results = []

    # try GeoTIFF direct downloads first
    for pkg in results:
        for resource in pkg.get("resources", []):
            url = resource.get("url") or resource.get("link")
            fmt = (resource.get("format") or "").lower()
            if not url:
                continue
            if url.lower().endswith('.tif') or 'geotiff' in fmt or 'tif' in fmt:
                logging.info('Attempting direct GeoTIFF download from %s', url)
                try:
                    rr = requests.get(url, stream=True, timeout=60)
                    if rr.status_code == 200:
                        with open(out_tif, 'wb') as fh:
                            for chunk in rr.iter_content(8192):
                                fh.write(chunk)
                        # verify coverage
                        if _raster_covers_bbox(out_tif, bbox):
                            logging.info('Downloaded GeoTIFF covers bbox')
                            return out_tif
                        else:
                            logging.info('Downloaded GeoTIFF does not cover bbox; skipping')
                            os.remove(out_tif)
                except Exception as e:
                    logging.warning('Failed to download GeoTIFF resource: %s', e)
    # try WCS resources
    for pkg in results:
        for resource in pkg.get("resources", []):
            url = resource.get("url") or resource.get("link")
            fmt = (resource.get("format") or "").lower()
            if not url:
                continue
            if 'wcs' in url.lower() or fmt == 'wcs':
                # attempt using resource name as coverage id, then package name
                coverage_candidates = [resource.get('name'), pkg.get('name'), pkg.get('title')]
                for cov in coverage_candidates:
                    if not cov:
                        continue
                    logging.info('Attempting WCS GetCoverage: base=%s coverage=%s', url, cov)
                    try:
                        ok = download_wcs_getcoverage(url, cov, bbox, out_tif)
                        if ok and _raster_covers_bbox(out_tif, bbox):
                            return out_tif
                        elif ok:
                            logging.info('WCS returned raster but it does not fully cover bbox')
                    except Exception as e:
                        logging.warning('WCS attempt failed for coverage %s: %s', cov, e)
    return None

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

def generate_transects(centerline: LineString, distances_m=None, interval=50, n_each_side=3, length_m=200, bridge_lon=None, bridge_lat=None, along_m=None):
    """Generate transects perpendicular to the centreline.

    Stations are measured from the nearest point on the centreline to the
    bridge coordinates when provided; otherwise the line midpoint is used.
    If along_m is set, stations run from -along_m/2 to +along_m/2 at `interval`.
    Returns list of (transect LineString, station_m).
    """
    if interval <= 0:
        raise HydroScreenError("Transect spacing along the river must be greater than 0.")
    if length_m <= 0:
        raise HydroScreenError("Transect length must be greater than 0.")

    # Work in WebMercator for metric distances
    transformer_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    transformer_to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

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

def sample_dem_along_line(dem_path, line: LineString, n_points=None, spacing_m=None):
    """Sample DEM elevations at regular metric spacing along a lon/lat line.

    Returns (distances_m, elevations, sample_lonlat).
    """
    transformer_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    transformer_to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
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

    coords = []
    for dist in distances:
        pt = proj_line.interpolate(dist)
        lon, lat = transformer_to_4326.transform(pt.x, pt.y)
        coords.append((lon, lat))

    with rasterio.open(dem_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        coords_raster = [transformer.transform(x, y) for x, y in coords]
        elevations = []
        for val in src.sample(coords_raster):
            v = val[0]
            if src.nodata is not None and (v == src.nodata or np.isnan(v)):
                elevations.append(np.nan)
            else:
                elevations.append(float(v))
        return np.array(distances), np.array(elevations), coords

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


def solve_water_level(dists, elevs, flow_m3_s, mannings_n, slope, step_m=0.02):
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
        wse += step_m
        hyd = hydraulics_at_stage(dists, elevs, wse)
        velocity, discharge = manning_discharge(hyd["area_m2"], hyd["hydraulic_radius_m"], mannings_n, slope)

    # Refine between the last two steps.
    lo = max(zmin, wse - step_m)
    hi = wse
    for _ in range(24):
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


def estimate_centerline_slope(dem_path, centerline, spacing_m=10.0):
    """Bed slope along the river centreline from DEM samples, as |dz/ds|."""
    dists, elevs, _ = sample_dem_along_line(dem_path, centerline, spacing_m=spacing_m)
    mask = np.isfinite(elevs)
    if mask.sum() < 2:
        return None
    distance = dists[mask]
    elevation = elevs[mask]
    if float(distance[-1] - distance[0]) < 1.0:
        return None
    slope, _intercept = np.polyfit(distance.astype(float), elevation.astype(float), 1)
    return abs(float(slope))


def plot_cross_section(dists, elevs, out_png, water_level=None):
    plt.figure(figsize=(8, 4))
    plt.plot(dists, elevs, '-k', label='Ground')
    finite = elevs[np.isfinite(elevs)]
    y_floor = (np.min(finite) - 1) if len(finite) else -1
    if water_level is not None and np.isfinite(water_level):
        wet = np.isfinite(elevs) & (elevs < water_level)
        plt.fill_between(dists, elevs, water_level, where=wet, color='#4ea3c9', alpha=0.55, interpolate=True)
        plt.axhline(water_level, color='#1f6f8b', linestyle='--', linewidth=1.2, label='Water level')
        plt.legend(loc='best', frameon=False)
    else:
        plt.fill_between(dists, elevs, y_floor, color='lightblue')
    plt.xlabel('Distance (m)')
    plt.ylabel('Elevation (m)')
    plt.title('Cross-section')
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_png)
    plt.close()


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
    along_m=None,
    sample_spacing=1.0,
    centerline_coords=None,
):
    """Run hydraulic screening at a bridge coordinate. Returns a result dict."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    dem_path = dem
    temp_dir = None
    buffer_m = max(buffer, 200.0)
    bbox = bbox_from_point(lat, lon, buffer_m)

    if dem_path is None:
        temp_dir = tempfile.mkdtemp(prefix="hydroscreen_")
        out_tif = os.path.join(temp_dir, "dem.tif")
        if wcs_base and wcs_layer:
            logging.info("Attempting user-supplied WCS for bbox (buffer %sm)", buffer_m)
            ok = download_wcs_getcoverage(wcs_base, wcs_layer, bbox, out_tif)
            if ok and _raster_covers_bbox(out_tif, bbox):
                dem_path = out_tif
            elif ok:
                logging.info(
                    "WCS returned a raster but it does not fully cover the %sm buffer; continuing LINZ discovery.",
                    buffer_m,
                )
        if dem_path is None:
            logging.info("Searching LINZ data catalog for DEM covering the location (buffer %sm)...", buffer_m)
            linz_out = os.path.join(temp_dir, "linz_dem.tif")
            found = find_linz_dem(lat, lon, buffer_m, linz_out)
            if found:
                dem_path = found

    if dem_path is None:
        raise HydroScreenError(
            "No DEM available. Provide a local GeoTIFF, a valid WCS, or a LINZ resource covering this location."
        )

    if not _raster_covers_bbox(dem_path, bbox):
        raise HydroScreenError(
            "The selected DEM does not cover this bridge location. Choose a point inside the DEM or upload a different file."
        )

    if centerline_coords:
        logging.info("Using user-drawn river centreline (%d vertices)", len(centerline_coords))
        centerline = centerline_from_coords(centerline_coords)
        centerline_source = "drawn"
    else:
        logging.info("Querying OSM for waterway near lat=%s lon=%s", lat, lon)
        centerline = query_osm_waterway(lat, lon, radius_m=500)
        if centerline is None:
            logging.warning("No OSM waterway found within 500 m. Using a synthetic centreline (line through point).")
            centerline = LineString([(lon - 0.005, lat), (lon + 0.005, lat)])
            centerline_source = "synthetic"
        else:
            centerline_source = "osm"

    if along_m is None:
        along_m = 2.0 * n_each_side * interval

    if mannings_n <= 0:
        raise HydroScreenError("Manning's n must be greater than 0.")
    if flow_m3_s < 0:
        raise HydroScreenError("Flow rate cannot be negative.")

    slope = estimate_centerline_slope(dem_path, centerline, spacing_m=max(sample_spacing, 5.0))
    if slope is None or slope <= 0:
        raise HydroScreenError(
            "Could not estimate river slope from the centreline DEM samples. Draw a longer river line over varying ground."
        )
    if slope < 1e-6:
        slope = 1e-6
    logging.info("Estimated centreline slope S=%.6f", slope)

    transects = generate_transects(
        centerline,
        interval=interval,
        n_each_side=n_each_side,
        length_m=length,
        bridge_lon=lon,
        bridge_lat=lat,
        along_m=along_m,
    )
    logging.info("Generated %d transects", len(transects))

    summary = []
    transect_features = []
    for i, (tran, offset) in enumerate(transects):
        dists, elevs, sample_coords = sample_dem_along_line(
            dem_path, tran, spacing_m=sample_spacing
        )
        stats = solve_water_level(
            dists, elevs, flow_m3_s=flow_m3_s, mannings_n=mannings_n, slope=slope
        )
        df = pd.DataFrame({
            "distance_m": dists,
            "longitude": [xy[0] for xy in sample_coords],
            "latitude": [xy[1] for xy in sample_coords],
            "elevation_m": elevs,
        })
        csv_path = outdir / f"transect_{i+1}.csv"
        df.to_csv(csv_path, index=False)
        png_path = outdir / f"transect_{i+1}.png"
        plot_cross_section(dists, elevs, png_path, water_level=stats.get("water_level_m"))
        stats.update({
            "transect": i + 1,
            "offset_m": float(offset),
            "n_samples": int(len(dists)),
            "sample_spacing_m": float(sample_spacing),
            "csv": str(csv_path),
            "plot": str(png_path),
        })
        summary.append(stats)
        sample_preview = sample_coords
        if len(sample_preview) > 80:
            step = max(1, len(sample_preview) // 80)
            sample_preview = sample_preview[::step]
            if sample_preview[-1] != sample_coords[-1]:
                sample_preview.append(sample_coords[-1])
        transect_features.append({
            "transect": i + 1,
            "offset_m": float(offset),
            "coords": [[x, y] for x, y in tran.coords],
            "samples": [[x, y] for x, y in sample_preview],
            "n_samples": int(len(dists)),
            "csv": csv_path.name,
            "plot": png_path.name,
        })

    summary_df = pd.DataFrame(summary)
    summary_path = outdir / "summary.xlsx"
    summary_df.to_excel(summary_path, index=False)
    logging.info("Wrote summary to %s", summary_path)

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
        "transects": transect_features,
        "centerline_source": centerline_source,
        "used_synthetic_centerline": centerline_source == "synthetic",
        "layout": {
            "along_m": float(along_m) if along_m is not None else None,
            "interval_m": float(interval),
            "transect_length_m": float(length),
            "sample_spacing_m": float(sample_spacing),
            "n_transects": len(transects),
            "flow_m3_s": float(flow_m3_s),
            "mannings_n": float(mannings_n),
            "slope": float(slope),
        },
        "temp_dem": temp_dir,
    }


def main():
    parser = argparse.ArgumentParser(description='Hydraulic screening MVP')
    parser.add_argument('--lat', type=float, required=True, help='Bridge latitude in decimal degrees')
    parser.add_argument('--lon', type=float, required=True, help='Bridge longitude in decimal degrees')
    parser.add_argument('--dem', type=str, default=None, help='Path to local DEM GeoTIFF (optional)')
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
