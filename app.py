"""HydroBridge web UI — pick a bridge site on a map and run screening."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from hydroscreen import HydroScreenError, run_screening

ROOT = Path(__file__).resolve().parent
SAMPLE_DEM = ROOT / "tests" / "fixtures" / "sample_dem.tif"
OUTPUTS = ROOT / "outputs"
NOMINATIM = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "HydroBridge/0.1 (hydraulic screening)"}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")


def _new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/search")
def search_places():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify([])
    try:
        response = requests.get(
            f"{NOMINATIM}/search",
            params={"q": query, "format": "jsonv2", "limit": 6, "addressdetails": 0},
            headers=NOMINATIM_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        hits = []
        for item in response.json():
            hits.append({
                "label": item.get("display_name"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
            })
        return jsonify(hits)
    except Exception as exc:
        logging.warning("Place search failed: %s", exc)
        return jsonify({"error": "Place search is unavailable right now."}), 502


@app.get("/api/reverse")
def reverse_geocode():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon are required numbers."}), 400
    try:
        response = requests.get(
            f"{NOMINATIM}/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 16},
            headers=NOMINATIM_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return jsonify({"label": data.get("display_name") or "Selected location"})
    except Exception as exc:
        logging.warning("Reverse geocode failed: %s", exc)
        return jsonify({"label": None})


@app.post("/api/run")
def run():
    try:
        lat = float(request.form.get("lat"))
        lon = float(request.form.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid latitude and longitude, or click the map."}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Latitude must be between -90 and 90, longitude between -180 and 180."}), 400

    dem_source = (request.form.get("dem_source") or "sample").strip()
    dem_path = None
    uploaded = request.files.get("dem")
    run_id = _new_run_id()
    outdir = OUTPUTS / run_id
    outdir.mkdir(parents=True, exist_ok=True)

    if dem_source == "upload":
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Choose a GeoTIFF DEM to upload, or use the Wellington sample DEM."}), 400
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in {".tif", ".tiff"}:
            return jsonify({"error": "DEM must be a GeoTIFF (.tif or .tiff)."}), 400
        dem_path = outdir / f"upload{suffix}"
        uploaded.save(dem_path)
        dem_path = str(dem_path)
    elif dem_source == "sample":
        if not SAMPLE_DEM.exists():
            return jsonify({"error": "Bundled sample DEM is missing."}), 500
        dem_path = str(SAMPLE_DEM)
    else:
        dem_path = None

    try:
        interval = float(request.form.get("interval") or 50)
        n_each_side = int(request.form.get("n_each_side") or 3)
        length = float(request.form.get("length") or 200)
        mannings_n = float(request.form.get("mannings_n") or 0.035)
        slope = float(request.form.get("slope") or 0.001)
    except (TypeError, ValueError):
        return jsonify({"error": "Screening options must be numbers."}), 400

    wcs_base = (request.form.get("wcs_base") or "").strip() or None
    wcs_layer = (request.form.get("wcs_layer") or "").strip() or None

    try:
        result = run_screening(
            lat=lat,
            lon=lon,
            dem=dem_path,
            wcs_base=wcs_base,
            wcs_layer=wcs_layer,
            outdir=str(outdir),
            interval=interval,
            n_each_side=n_each_side,
            length=length,
            mannings_n=mannings_n,
            slope=slope,
        )
    except HydroScreenError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("Screening failed")
        return jsonify({"error": f"Screening failed: {exc}"}), 500

    public_summary = []
    for row in result["summary"]:
        public_summary.append({
            "transect": row.get("transect"),
            "offset_m": row.get("offset_m"),
            "width_m": row.get("width_m"),
            "area_m2": row.get("area_m2"),
            "depth_mean_m": row.get("depth_mean_m"),
            "velocity_m_s": row.get("velocity_m_s"),
            "discharge_m3_s": row.get("discharge_m3_s"),
            "plot": f"/results/{run_id}/{Path(row['plot']).name}",
            "csv": f"/results/{run_id}/{Path(row['csv']).name}",
        })

    return jsonify({
        "run_id": run_id,
        "lat": lat,
        "lon": lon,
        "used_synthetic_centerline": result["used_synthetic_centerline"],
        "centerline": result["centerline"],
        "transects": [
            {
                **feat,
                "plot": f"/results/{run_id}/{feat['plot']}",
                "csv": f"/results/{run_id}/{feat['csv']}",
            }
            for feat in result["transects"]
        ],
        "summary": public_summary,
        "summary_xlsx": f"/results/{run_id}/{result['summary_xlsx']}",
    })


@app.get("/results/<run_id>/<path:filename>")
def result_file(run_id, filename):
    if not RUN_ID_RE.match(run_id):
        abort(404)
    if Path(filename).name != filename:
        abort(404)
    return send_from_directory(OUTPUTS / run_id, filename)


if __name__ == "__main__":
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5050, debug=False)
