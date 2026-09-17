# HydroBridge

Hydraulic screening MVP for rapidly extracting DEM cross-sections at bridge sites.

The tool accepts coordinates, clips the [New Zealand LiDAR 1m DEM](https://data.linz.govt.nz/layer/121859-new-zealand-lidar-1m-dem/) around the site, identifies a stream centreline from OpenStreetMap (or a line you draw), generates transects, samples elevations, plots cross-sections, exports CSV/Excel, and solves water level from Manning’s equation.

## Share with a colleague

Do not copy your `.venv` folder. Each computer needs its own environment.

1. Put the repo on GitHub (or send a zip **without** `.venv`).
2. They install [64-bit Python 3.12](https://www.python.org/downloads/) and tick **Add python.exe to PATH**.
3. On Windows they double-click `run.bat`. On macOS/Linux they run `./run.sh`.
4. A browser should open at http://127.0.0.1:5050. Leave the terminal window open while they use it.

If `run.bat` fails on NumPy or Matplotlib, install the [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) and run `run.bat` again. Conda is the fallback:

```bat
conda env create -f environment.yml
conda activate hydrobridge
python app.py
```

## Requirements

- Python 3.11 or 3.12 (3.13+ often breaks the GIS wheels on Windows)
- Install dependencies into a virtualenv, or use `run.bat` / `run.sh` which do this for you:

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

The map UI opens centred on Christchurch. Search or pan to a site, then:

```bash
.venv/bin/python app.py
```

Open http://127.0.0.1:5050.

1. Double-click a bridge and name the pin.
2. Draw the river centreline through the pin (click along the channel, double-click to finish). The drawn length is the analysis reach.
3. Set hydrology in the panel that appears. Each parameter has a **?** with its definition:

- **Transect length** — width of each cross-section, centred on the river
- **Transect spacing** — distance between cross-sections along the centreline
- **Sample spacing** — distance between DEM sample points on each transect
- **Flow rate Q** and **Manning’s n** — water level is raised on each transect until Manning’s equation matches that flow; slope is estimated from the centreline

Elevations default to the nationwide **New Zealand LiDAR 1m DEM** (LINZ layer 121859). HydroBridge downloads only a window around the pin from the public COG tiles and draws that DEM on the map at 50% opacity. You can still upload a GeoTIFF or use the bundled Wellington sample DEM offline.

## Command line

By default the CLI clips the New Zealand LiDAR 1m DEM around the coordinates:

```bash
.venv/bin/python hydroscreen.py --lat -41.2865 --lon 174.7762 --flow 10 --mannings_n 0.035 --outdir outputs
```

Local DEM:

```bash
.venv/bin/python hydroscreen.py --lat -41.2865 --lon 174.7762 --dem tests/fixtures/sample_dem.tif --flow 10 --mannings_n 0.035 --outdir outputs
```

## What the script does (MVP)

1. Clip the New Zealand LiDAR 1m DEM around the bridge (LINZ layer 121859 / nz-elevation COGs).
2. Query OSM (Overpass) for nearby waterways to get a centreline. If none is found, fall back to a synthetic east-west line through the point. The map UI can supply a drawn centreline instead.
3. Generate transects perpendicular to the centreline at the bridge location and upstream/downstream intervals.
4. Sample DEM elevations along transects.
5. Produce cross-section plots, per-transect CSV, and `summary.xlsx` with a SUMMARY sheet of transect hydraulics and a DATA sheet of every sampled point (ID, Transect ID, distance along the transect, elevation).
6. Estimate channel slope from the river centreline on the DEM, then raise water level on each transect (trapezoidal area and hydraulic radius) until Manning’s equation matches the specified flow.

## Notes about LINZ

The default elevation source is the national 1 m LiDAR DEM published as [layer 121859](https://data.linz.govt.nz/layer/121859-new-zealand-lidar-1m-dem/). Tiles are read from the [NZ Elevation](https://registry.opendata.aws/nz-elevation/) public S3 bucket (`s3://nz-elevation/new-zealand/new-zealand/dem_1m/2193/`). Some remote sites have no LiDAR yet; in that case upload a GeoTIFF.

## Limitations

- Centreline extraction from DEM (hydrologic processing) is not implemented — OSM or a drawn line is used. TauDEM or richDEM would be needed for production-grade channel extraction.
- LiDAR coverage is not complete for every offshore island or remote catchment.
- Advanced hydraulic checks (HEC-RAS/TUFLOW model prep) are out of scope, but CSV output is compatible with those tools.

## Files

- `app.py` — map-based web UI for choosing a bridge site
- `hydroscreen.py` — screening engine and CLI
- `run.bat` / `run.sh` — create the venv if needed, install packages, start the web UI
- `environment.yml` — conda-forge environment (often easier than pip on Windows)
- `data/linz_dem_1m_index.json` — Topo50 sheet bboxes for the national 1 m DEM
- `requirements.txt` — Python dependencies
- `tests/fixtures/sample_dem.tif` — synthetic DEM covering Wellington Harbour for local smoke tests
