"""HydroBridge web UI — pick a bridge site on a map and run screening."""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import tempfile
import threading
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, stream_with_context

from hydroscreen import (
    HydroScreenCancelled,
    HydroScreenError,
    preview_dem_overlay,
    run_screening,
    sample_drawn_cross_section,
)

ROOT = Path(__file__).resolve().parent
SAMPLE_DEM = ROOT / "tests" / "fixtures" / "sample_dem.tif"
OUTPUTS = ROOT / "outputs"
NOMINATIM = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "HydroBridge/0.1 (hydraulic screening)"}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_active_jobs = {}
_active_jobs_lock = threading.Lock()


def _new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _parse_job_id():
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        payload = request.get_json(silent=True) or {}
        job_id = str(payload.get("job_id") or "").strip()
    if JOB_ID_RE.match(job_id):
        return job_id
    return None


@app.post("/api/stop")
def stop_run():
    job_id = _parse_job_id()
    if not job_id:
        return jsonify({"error": "Missing screening job."}), 400
    with _active_jobs_lock:
        event = _active_jobs.get(job_id)
    if event is not None:
        event.set()
        logging.info("Stop requested for screening job %s", job_id)
    return jsonify({"cancelled": True})


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


def _preview_payload(result):
    west, south, east, north = result["bounds"]
    return {
        "png": b64encode(result["png"]).decode("ascii"),
        "bounds": [[south, west], [north, east]],
        "source": result["source"],
        "radius_m": result["radius_m"],
        "clip_size_m": result.get("clip_size_m"),
    }


@app.route("/api/dem-preview", methods=["GET", "POST"])
def dem_preview():
    src = request.form if request.method == "POST" else request.args
    try:
        lat = float(src.get("lat"))
        lon = float(src.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid latitude and longitude, or click the map."}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Latitude must be between -90 and 90, longitude between -180 and 180."}), 400
    dem_source = (src.get("dem_source") or "linz").strip()
    try:
        along_m = float(src.get("along") or 300)
        length = float(src.get("length") or 200)
    except (TypeError, ValueError):
        return jsonify({"error": "Screening options must be numbers."}), 400
    if along_m < 0 or length <= 0:
        return jsonify({"error": "Transect length and length along the river must be greater than 0."}), 400

    dem_path = None
    if dem_source == "sample":
        if not SAMPLE_DEM.exists():
            return jsonify({"error": "Bundled sample DEM is missing."}), 500
        dem_path = str(SAMPLE_DEM)
    elif dem_source == "upload":
        uploaded = request.files.get("dem")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Choose a GeoTIFF DEM to overlay."}), 400
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in {".tif", ".tiff"}:
            return jsonify({"error": "DEM must be a GeoTIFF (.tif or .tiff)."}), 400
        tmp = Path(tempfile.gettempdir()) / f"hydroscreen-preview-{secrets.token_hex(4)}{suffix}"
        uploaded.save(tmp)
        dem_path = str(tmp)
    elif dem_source != "linz":
        return jsonify({"error": "Choose the New Zealand LiDAR DEM, the sample DEM, or upload a GeoTIFF."}), 400

    stream = (src.get("stream") or request.args.get("stream") or "").strip() == "1"
    if stream:
        events = queue.Queue()

        def on_progress(fraction, message=None):
            events.put({
                "percent": int(round(max(0.0, min(1.0, float(fraction))) * 100)),
                "message": message or "Downloading DEM…",
            })

        def work():
            try:
                result = preview_dem_overlay(
                    lat,
                    lon,
                    dem_path=dem_path,
                    along_m=along_m,
                    length=length,
                    progress=on_progress,
                )
                payload = _preview_payload(result)
                payload["percent"] = 100
                payload["message"] = "DEM ready"
                payload["done"] = True
                events.put(payload)
            except HydroScreenError as exc:
                events.put({"error": str(exc), "percent": 0, "done": True})
            except Exception as exc:
                logging.exception("DEM preview failed")
                events.put({
                    "error": f"Could not overlay the DEM: {exc}",
                    "percent": 0,
                    "done": True,
                })
            finally:
                if dem_source == "upload" and dem_path:
                    Path(dem_path).unlink(missing_ok=True)
                events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def generate():
            yield json.dumps({"percent": 0, "message": "Downloading DEM…"}) + "\n"
            while True:
                item = events.get()
                if item is None:
                    break
                yield json.dumps(item) + "\n"

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
        )

    try:
        result = preview_dem_overlay(
            lat, lon, dem_path=dem_path, along_m=along_m, length=length
        )
    except HydroScreenError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("DEM preview failed")
        return jsonify({"error": f"Could not overlay the DEM: {exc}"}), 500
    finally:
        if dem_source == "upload" and dem_path:
            Path(dem_path).unlink(missing_ok=True)

    payload = _preview_payload(result)
    payload["opacity"] = 0.5
    return jsonify(payload)


@app.post("/api/cross-section")
def cross_section():
    try:
        lon1 = float(request.form.get("lon1"))
        lat1 = float(request.form.get("lat1"))
        lon2 = float(request.form.get("lon2"))
        lat2 = float(request.form.get("lat2"))
    except (TypeError, ValueError):
        return jsonify({"error": "Click two points on the map to draw a cross-section."}), 400
    if not (
        -90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180
    ):
        return jsonify({"error": "Cross-section coordinates are out of range."}), 400
    try:
        spacing = float(request.form.get("sample_spacing") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "Sample spacing must be a number."}), 400
    if spacing <= 0:
        return jsonify({"error": "Sample spacing must be greater than 0."}), 400

    site_lat = site_lon = None
    try:
        if request.form.get("lat") not in (None, "") and request.form.get("lon") not in (None, ""):
            site_lat = float(request.form.get("lat"))
            site_lon = float(request.form.get("lon"))
            if not (-90 <= site_lat <= 90 and -180 <= site_lon <= 180):
                site_lat = site_lon = None
    except (TypeError, ValueError):
        site_lat = site_lon = None

    dem_source = (request.form.get("dem_source") or "linz").strip()
    dem_path = None
    cleanup = None
    if dem_source == "sample":
        if not SAMPLE_DEM.exists():
            return jsonify({"error": "Bundled sample DEM is missing."}), 500
        dem_path = str(SAMPLE_DEM)
    elif dem_source == "upload":
        uploaded = request.files.get("dem")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Choose a GeoTIFF DEM to sample, or use the New Zealand LiDAR 1m DEM."}), 400
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in {".tif", ".tiff"}:
            return jsonify({"error": "DEM must be a GeoTIFF (.tif or .tiff)."}), 400
        cleanup = Path(tempfile.gettempdir()) / f"hydroscreen-xs-{secrets.token_hex(4)}{suffix}"
        uploaded.save(cleanup)
        dem_path = str(cleanup)
    elif dem_source != "linz":
        return jsonify({"error": "Choose the New Zealand LiDAR DEM, the sample DEM, or upload a GeoTIFF."}), 400

    try:
        profile = sample_drawn_cross_section(
            lon1,
            lat1,
            lon2,
            lat2,
            dem_path=dem_path,
            spacing_m=spacing,
            site_lat=site_lat,
            site_lon=site_lon,
        )
    except HydroScreenError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("Cross-section sampling failed")
        return jsonify({"error": f"Could not sample the DEM: {exc}"}), 500
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)
    return jsonify(profile)


@app.post("/api/run")
def run():
    try:
        lat = float(request.form.get("lat"))
        lon = float(request.form.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid latitude and longitude, or click the map."}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Latitude must be between -90 and 90, longitude between -180 and 180."}), 400

    dem_source = (request.form.get("dem_source") or "linz").strip()
    dem_path = None
    uploaded = request.files.get("dem")
    run_id = _new_run_id()
    outdir = OUTPUTS / run_id
    outdir.mkdir(parents=True, exist_ok=True)

    if dem_source == "upload":
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Choose a GeoTIFF DEM to upload, or use the New Zealand LiDAR 1m DEM."}), 400
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
    elif dem_source == "linz":
        dem_path = None
    else:
        return jsonify({"error": "Choose the New Zealand LiDAR DEM, the sample DEM, or upload a GeoTIFF."}), 400

    try:
        interval = float(request.form.get("interval") or 50)
        along_raw = request.form.get("along")
        along_m = float(along_raw) if along_raw not in (None, "") else 300.0
        length = float(request.form.get("length") or 200)
        sample_spacing = float(request.form.get("sample_spacing") or 1)
        mannings_n = float(request.form.get("mannings_n") or 0.035)
        flow_m3_s = float(request.form.get("flow") or 10)
    except (TypeError, ValueError):
        return jsonify({"error": "Screening options must be numbers."}), 400
    if interval <= 0 or along_m < 0 or length <= 0 or sample_spacing <= 0:
        return jsonify({"error": "Transect length and spacing must be greater than 0."}), 400
    if mannings_n <= 0 or flow_m3_s < 0:
        return jsonify({"error": "Flow rate must be ≥ 0 and Manning's n must be greater than 0."}), 400

    wcs_base = (request.form.get("wcs_base") or "").strip() or None
    wcs_layer = (request.form.get("wcs_layer") or "").strip() or None

    centerline_coords = None
    raw_centerline = (request.form.get("centerline") or "").strip()
    if raw_centerline:
        try:
            parsed = json.loads(raw_centerline)
        except json.JSONDecodeError:
            return jsonify({"error": "The drawn river line could not be read. Clear it and draw again."}), 400
        if not isinstance(parsed, list):
            return jsonify({"error": "The river centreline must be a list of points."}), 400
        if len(parsed) > 500:
            return jsonify({"error": "The drawn river line has too many points. Clear it and draw a simpler line."}), 400
        centerline_coords = parsed

    job_id = _parse_job_id() or secrets.token_hex(8)
    cancel_event = threading.Event()
    with _active_jobs_lock:
        _active_jobs[job_id] = cancel_event
    try:
        result = run_screening(
            lat=lat,
            lon=lon,
            dem=dem_path,
            wcs_base=wcs_base,
            wcs_layer=wcs_layer,
            outdir=str(outdir),
            interval=interval,
            along_m=along_m,
            length=length,
            sample_spacing=sample_spacing,
            mannings_n=mannings_n,
            flow_m3_s=flow_m3_s,
            centerline_coords=centerline_coords,
            cancel_event=cancel_event,
        )
    except HydroScreenCancelled:
        return jsonify({"cancelled": True, "error": "Screening stopped."}), 409
    except HydroScreenError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("Screening failed")
        return jsonify({"error": f"Screening failed: {exc}"}), 500
    finally:
        with _active_jobs_lock:
            if _active_jobs.get(job_id) is cancel_event:
                _active_jobs.pop(job_id, None)

    public_summary = []
    for row in result["summary"]:
        public_summary.append({
            "transect": row.get("transect"),
            "offset_m": row.get("offset_m"),
            "width_m": row.get("width_m"),
            "area_m2": row.get("area_m2"),
            "depth_mean_m": row.get("depth_mean_m"),
            "max_depth_m": row.get("max_depth_m"),
            "water_level_m": row.get("water_level_m"),
            "hydraulic_radius_m": row.get("hydraulic_radius_m"),
            "velocity_m_s": row.get("velocity_m_s"),
            "discharge_m3_s": row.get("discharge_m3_s"),
            "target_discharge_m3_s": row.get("target_discharge_m3_s"),
            "slope": row.get("slope"),
            "conveys": row.get("conveys"),
            "overtopped": row.get("overtopped"),
            "n_samples": row.get("n_samples"),
            "plot": f"/results/{run_id}/{Path(row['plot']).name}",
            "csv": f"/results/{run_id}/{Path(row['csv']).name}",
        })

    return jsonify({
        "run_id": run_id,
        "lat": lat,
        "lon": lon,
        "centerline_source": result["centerline_source"],
        "used_synthetic_centerline": result["used_synthetic_centerline"],
        "layout": result.get("layout"),
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
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
