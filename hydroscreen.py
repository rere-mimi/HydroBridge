"""
Hydraulic screening MVP
- Accepts lat/lon or bridge ID (lat/lon for MVP)
- Attempts to download DEM from LINZ WCS (public) if wcs_base and wcs_layer supplied
- Falls back to local DEM if provided with --dem
- Queries OSM (Overpass) for nearby waterway centreline
- Generates transects and samples DEM elevations
- Exports CSV/Excel and plots

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

def generate_transects(centerline: LineString, distances_m=[0], interval=50, n_each_side=3, length_m=200):
    """Generate transects perpendicular to the centreline.
    distances_m: distances from bridge point along the line (negative = upstream)
    For MVP: given centerline, pick a point at its midpoint as bridge point if necessary.
    Returns list of (transect LineString, station_m)
    """
    # Work in WebMercator for metric distances
    transformer_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    transformer_to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    # Project centerline to 3857
    proj_coords = [transformer_to_3857.transform(x, y) for (x, y) in centerline.coords]
    proj_line = LineString(proj_coords)
    total_len = proj_line.length
    # Define station positions along the line: center at midpoint
    mid_pos = proj_line.project(LineString(proj_coords).interpolate(total_len / 2))
    stations = []
    # default distances: generate -n..+n intervals
    if distances_m == [0]:
        for i in range(-n_each_side, n_each_side + 1):
            stations.append(mid_pos + i * interval)
    else:
        stations = [mid_pos + d for d in distances_m]

    transects = []
    half_len = length_m / 2.0
    for s in stations:
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
        transects.append((transect_line, s - mid_pos))
    return transects

# Sample DEM along a LineString

def sample_dem_along_line(dem_path, line: LineString, n_points=201):
    """Return distances (m from left end) and elevations along the line. Assumes DEM in geographic coords or projected; rasterio handles reprojection with sample using coords in raster CRS.
    """
    with rasterio.open(dem_path) as src:
        # get transformer from lonlat to raster CRS
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        xs = np.linspace(line.coords[0][0], line.coords[-1][0], n_points)
        ys = np.linspace(line.coords[0][1], line.coords[-1][1], n_points)
        coords = list(zip(xs, ys))
        # transform coords to raster CRS
        coords_raster = [transformer.transform(x, y) for x, y in coords]
        elevations = []
        for val in src.sample(coords_raster):
            v = val[0]
            if src.nodata is not None and (v == src.nodata or np.isnan(v)):
                elevations.append(np.nan)
            else:
                elevations.append(float(v))
        # distances in meters assuming geographic coords: compute great-circle between successive points
        dists = [0.0]
        for i in range(1, len(coords)):
            # approximate by haversine
            lon1, lat1 = coords[i - 1]
            lon2, lat2 = coords[i]
            d = haversine(lon1, lat1, lon2, lat2)
            dists.append(dists[-1] + d)
        return np.array(dists), np.array(elevations)

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

def basic_hydraulic_checks(dists, elevs, mannings_n=0.035, slope=0.001):
    """Compute width (approx where elevation below bank), area (trapezoid), and simple Manning velocity & capacity.
    This is a heuristic: find channel by taking min elevation and banks where elevation rises by a threshold.
    """
    # Simple channel detection: find min elevation and define channel as points within (min + delta)
    valid = ~np.isnan(elevs)
    if valid.sum() == 0:
        return {}
    min_elev = np.nanmin(elevs)
    delta = 0.5  # m above minimum to detect banks (heuristic)
    channel_idx = np.where((elevs <= min_elev + delta) & valid)[0]
    if len(channel_idx) == 0:
        # fallback: take whole transect width
        width = dists[-1] - dists[0]
        area = 0.0
    else:
        width = dists[channel_idx[-1]] - dists[channel_idx[0]]
        # approximate area by integrating depth from a heuristic bank elevation down to the bed
        bank_elev = min_elev + delta
        depths = np.maximum(0.0, (bank_elev - elevs[channel_idx[0]:channel_idx[-1]+1]))
        # use trapezoidal approx across channel points
        dx = np.diff(dists[channel_idx[0]:channel_idx[-1]+1])
        if len(dx) == 0:
            area = 0.0
        else:
            trapz = np.trapezoid if hasattr(np, "trapezoid") else np.trapz
            area = trapz(depths, dists[channel_idx[0]:channel_idx[-1]+1])
    # wetted perimeter approx: assume rectangular channel: P = width + 2 * depth_mean
    depth_mean = area / width if width > 0 else 0.0
    P = width + 2 * depth_mean
    R = (area / P) if P > 0 else 0.0
    # Manning velocity and capacity
    if R > 0 and slope > 0:
        velocity = (1.0 / mannings_n) * (R ** (2.0 / 3.0)) * (slope ** 0.5)
        discharge = velocity * area
    else:
        velocity = 0.0
        discharge = 0.0
    return {
        "width_m": float(width),
        "area_m2": float(area),
        "depth_mean_m": float(depth_mean),
        "mannings_n": float(mannings_n),
        "slope": float(slope),
        "hydraulic_radius_m": float(R),
        "velocity_m_s": float(velocity),
        "discharge_m3_s": float(discharge)
    }

# Plot and export

def plot_cross_section(dists, elevs, out_png):
    plt.figure(figsize=(8, 4))
    plt.plot(dists, elevs, '-k')
    plt.fill_between(dists, elevs, elevs.min() - 1, color='lightblue')
    plt.xlabel('Distance (m)')
    plt.ylabel('Elevation (m)')
    plt.title('Cross-section')
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_png)
    plt.close()

# Main CLI

def main():
    parser = argparse.ArgumentParser(description='Hydraulic screening MVP')
    parser.add_argument('--lat', type=float, required=True, help='Bridge latitude in decimal degrees')
    parser.add_argument('--lon', type=float, required=True, help='Bridge longitude in decimal degrees')
    parser.add_argument('--dem', type=str, default=None, help='Path to local DEM GeoTIFF (optional)')
    parser.add_argument('--wcs_base', type=str, default=None, help='LINZ WCS base URL (optional)')
    parser.add_argument('--wcs_layer', type=str, default=None, help='WCS layer/coverage id (optional)')
    parser.add_argument('--outdir', type=str, default='outputs', help='Output directory')
    parser.add_argument('--buffer', type=float, default=200.0, help='Buffer around point for DEM download (m) (min 200 m enforced)')
    parser.add_argument('--interval', type=float, default=50.0, help='Transect interval (m)')
    parser.add_argument('--n_each_side', type=int, default=3, help='Number of transects each side of bridge')
    parser.add_argument('--length', type=float, default=200.0, help='Transect length (m)')
    parser.add_argument('--mannings_n', type=float, default=0.035, help="Manning's n for velocity estimate")
    parser.add_argument('--slope', type=float, default=0.001, help='Channel slope for Manning estimate')
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    dem_path = args.dem
    temp_dir = None
    # enforce a minimum 200 m buffer around the point to ensure bridge area coverage
    buffer_m = max(args.buffer, 200.0)

    if dem_path is None:
        temp_dir = tempfile.mkdtemp(prefix="hydroscreen_")
        out_tif = os.path.join(temp_dir, 'dem.tif')
        bbox = bbox_from_point(args.lat, args.lon, buffer_m)
        # 1) If user supplied a WCS base/layer, try that first
        if args.wcs_base and args.wcs_layer:
            logging.info('Attempting user-supplied WCS for bbox (buffer %sm)', buffer_m)
            ok = download_wcs_getcoverage(args.wcs_base, args.wcs_layer, bbox, out_tif)
            if ok and _raster_covers_bbox(out_tif, bbox):
                dem_path = out_tif
            elif ok:
                logging.info('WCS returned a raster but it does not fully cover the %sm buffer; continuing LINZ discovery.', buffer_m)
        # 2) Attempt automatic LINZ discovery & download
        if dem_path is None:
            logging.info('Searching LINZ data catalog for DEM covering the location (buffer %sm)...', buffer_m)
            linz_out = os.path.join(temp_dir, 'linz_dem.tif')
            found = find_linz_dem(args.lat, args.lon, buffer_m, linz_out)
            if found:
                dem_path = found

    if dem_path is None:
        logging.info('No DEM available. The tool requires a DEM (local GeoTIFF), a valid WCS, or a LINZ resource. Exiting.')
        sys.exit(1)

    # Query OSM for centreline
    logging.info('Querying OSM for waterway near lat=%s lon=%s', args.lat, args.lon)
    centerline = query_osm_waterway(args.lat, args.lon, radius_m=500)
    if centerline is None:
        logging.warning('No OSM waterway found within 500 m. Using a synthetic centreline (line through point).')
        # synthetic small centreline along E-W of length 1 km
        centerline = LineString([(args.lon - 0.005, args.lat), (args.lon + 0.005, args.lat)])

    transects = generate_transects(centerline, distances_m=[0], interval=args.interval, n_each_side=args.n_each_side, length_m=args.length)
    logging.info('Generated %d transects', len(transects))

    summary = []
    for i, (tran, offset) in enumerate(transects):
        dists, elevs = sample_dem_along_line(dem_path, tran, n_points=201)
        # drop nan segments and basic stats
        stats = basic_hydraulic_checks(dists, elevs, mannings_n=args.mannings_n, slope=args.slope)
        # save CSV
        df = pd.DataFrame({"distance_m": dists, "elevation_m": elevs})
        csv_path = outdir / f"transect_{i+1}.csv"
        df.to_csv(csv_path, index=False)
        # plot
        png_path = outdir / f"transect_{i+1}.png"
        plot_cross_section(dists, elevs, png_path)
        stats.update({"transect": i + 1, "offset_m": float(offset), "csv": str(csv_path), "plot": str(png_path)})
        summary.append(stats)

    # save summary excel
    summary_df = pd.DataFrame(summary)
    summary_path = outdir / "summary.xlsx"
    summary_df.to_excel(summary_path, index=False)
    logging.info('Wrote summary to %s', summary_path)

    if temp_dir:
        # leave temp_dir for inspection by user but print location
        logging.info('Temporary DEM stored in %s', temp_dir)
    logging.info('Done')

if __name__ == '__main__':
    main()
