# HydroBridge

Hydraulic screening MVP for rapidly extracting DEM cross-sections at bridge sites.

The tool accepts coordinates, obtains a DEM (public LINZ WCS if available, otherwise a local GeoTIFF), identifies a stream centreline from OpenStreetMap, generates transects, samples elevations, plots cross-sections, exports CSV/Excel, and runs simple hydraulic checks.

## Requirements

- Python 3.9+
- Install dependencies into a virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Debian/Ubuntu, install GDAL and spatialindex first if wheels are unavailable:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends gdal-bin libgdal-dev libspatialindex-dev
```

## Web interface

The easiest way to choose a bridge site is the map UI: click the map, search for a place, or type latitude/longitude.

```bash
.venv/bin/python app.py
```

Then open http://127.0.0.1:5050. Place the bridge pin, then click **Draw river** and sketch the channel. In **Transects and sampling**, set:

- **Transect length** — width of each cross-section, centred on the river
- **Length along river** and **Transect spacing** — how far along the centreline to cover and how often to cut a cross-section
- **Sample spacing** — distance between DEM sample points on each transect

HydroBridge then samples those points automatically. The bundled sample DEM covers Wellington Harbour; upload a GeoTIFF or use LINZ/WCS for other sites.

## Command line

If LINZ public WCS is available for your dataset, supply the WCS base URL and layer name. Otherwise provide a local GeoTIFF DEM with `--dem`.

Local DEM:

```bash
.venv/bin/python hydroscreen.py --lat -41.2865 --lon 174.7762 --dem tests/fixtures/sample_dem.tif --outdir outputs
```

Attempt LINZ WCS:

```bash
.venv/bin/python hydroscreen.py --lat -41.2865 --lon 174.7762 --wcs_base "https://example-linz-wcs.service/wcs" --wcs_layer "nzdem" --outdir outputs
```

## What the script does (MVP)

1. Query OSM (Overpass) for nearby waterways to get a centreline. If none is found, fall back to a synthetic east-west line through the point.
2. Generate transects perpendicular to the centreline at the bridge location and upstream/downstream intervals.
3. Sample DEM elevations along transects.
4. Produce cross-section plots and CSV/Excel outputs.
5. Perform basic hydraulic checks: channel width, bank height, cross-sectional area, and a rough Manning velocity/capacity estimate (user-supplied slope and Manning's n).

## Notes about LINZ

LINZ hosts NZ DEM products (NZDEM, regional LiDAR) and some services are available via WCS/WFS without credentials. This script attempts a conservative WCS GetCoverage request; if it fails, consult the LINZ data service for the correct WCS endpoint and layer name.

## Limitations

- Centreline extraction from DEM (hydrologic processing) is not implemented — OSM is used first. TauDEM or richDEM would be needed for production-grade channel extraction.
- LINZ WCS endpoints vary; the WCS helper may need tuning for specific LINZ services.
- Advanced hydraulic checks (HEC-RAS/TUFLOW model prep) are out of scope, but CSV output is compatible with those tools.

## Files

- `app.py` — map-based web UI for choosing a bridge site
- `hydroscreen.py` — screening engine and CLI
- `requirements.txt` — Python dependencies
- `tests/fixtures/sample_dem.tif` — synthetic DEM covering Wellington Harbour for local smoke tests
