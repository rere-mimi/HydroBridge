const WELLINGTON = { lat: -41.2865, lon: 174.7762, zoom: 14 };
const placeLabel = document.getElementById("place-label");
const lineLabel = document.getElementById("line-label");
const mapHint = document.getElementById("map-hint");
const latInput = document.getElementById("lat");
const lonInput = document.getElementById("lon");
const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-q");
const searchResults = document.getElementById("search-results");
const statusEl = document.getElementById("status");
const runForm = document.getElementById("run-form");
const runBtn = document.getElementById("run-btn");
const resultsEl = document.getElementById("results");
const resultsBody = document.getElementById("results-body");
const resultsNote = document.getElementById("results-note");
const plotsEl = document.getElementById("plots");
const summaryLink = document.getElementById("summary-link");
const demFileWrap = document.getElementById("dem-file-wrap");
const modePinBtn = document.getElementById("mode-pin");
const modeDrawBtn = document.getElementById("mode-draw");
const modeXsBtn = document.getElementById("mode-xs");
const undoBtn = document.getElementById("undo-vertex");
const clearBtn = document.getElementById("clear-line");
const snapBtn = document.getElementById("snap-pin");
const clearXsBtn = document.getElementById("clear-xs");
const layoutPreview = document.getElementById("layout-preview");
const xsLabel = document.getElementById("xs-label");
const xsViewer = document.getElementById("xs-viewer");
const xsMeta = document.getElementById("xs-meta");
const xsCanvas = document.getElementById("xs-canvas");

const map = L.map("map").setView([WELLINGTON.lat, WELLINGTON.lon], WELLINGTON.zoom);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);
map.createPane("demPane");
map.getPane("demPane").style.zIndex = 350;
map.getPane("demPane").style.pointerEvents = "none";

const overlay = L.layerGroup().addTo(map);
const draftLine = L.polyline([], { color: "#1f6f8b", weight: 5, opacity: 0.95 }).addTo(map);
const vertexLayer = L.layerGroup().addTo(map);
const xsLayer = L.layerGroup().addTo(map);
const xsDraftLine = L.polyline([], {
  color: "#c45c26",
  weight: 3,
  dashArray: "6 6",
  opacity: 0.9,
}).addTo(map);
let marker = L.marker([WELLINGTON.lat, WELLINGTON.lon], { draggable: true }).addTo(map);
marker.bindPopup("Bridge site").openPopup();

let mode = "pin";
let drawnLatLngs = [];
let drawingStroke = false;
let demOverlay = null;
let demOverlayUrl = null;
let demPreviewTimer = null;
let demPreviewSeq = 0;
let xsPoints = [];
let xsProfile = null;
let xsRequestSeq = 0;

function setStatus(message, kind) {
  statusEl.textContent = message || "";
  statusEl.className = "status" + (kind ? " " + kind : "");
}

function fmt(value, digits) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function currentLatLon() {
  return {
    lat: Number(latInput.value),
    lon: Number(lonInput.value),
  };
}

function currentDemSource() {
  const picked = runForm.querySelector('input[name="dem_source"]:checked');
  return picked ? picked.value : "linz";
}

function clearDemOverlay() {
  if (demOverlay) {
    map.removeLayer(demOverlay);
    demOverlay = null;
  }
  if (demOverlayUrl) {
    URL.revokeObjectURL(demOverlayUrl);
    demOverlayUrl = null;
  }
}

function scheduleDemPreview() {
  clearTimeout(demPreviewTimer);
  demPreviewTimer = setTimeout(refreshDemOverlay, 400);
}

async function refreshDemOverlay() {
  const { lat, lon } = currentLatLon();
  if (Number.isNaN(lat) || Number.isNaN(lon)) return;
  const source = currentDemSource();
  if (source === "upload") {
    const file = document.getElementById("dem-file");
    if (!file || !file.files || !file.files[0]) {
      clearDemOverlay();
      return;
    }
  }
  const seq = (demPreviewSeq += 1);
  const body = new FormData();
  body.set("lat", String(lat));
  body.set("lon", String(lon));
  body.set("dem_source", source);
  body.set("along", runForm.along.value);
  body.set("length", runForm.length.value);
  if (source === "upload") {
    body.set("dem", document.getElementById("dem-file").files[0]);
  }
  try {
    const res = await fetch("/api/dem-preview", { method: "POST", body });
    const data = await res.json();
    if (seq !== demPreviewSeq) return;
    if (!res.ok || !data.png || !data.bounds) {
      clearDemOverlay();
      return;
    }
    const bytes = Uint8Array.from(atob(data.png), (ch) => ch.charCodeAt(0));
    clearDemOverlay();
    demOverlayUrl = URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    demOverlay = L.imageOverlay(demOverlayUrl, data.bounds, {
      opacity: 0.5,
      pane: "demPane",
      interactive: false,
      className: "dem-overlay",
    }).addTo(map);
    if (mode === "draw") {
      mapHint.textContent = "Click or drag along the river. Double-click when the line is done.";
    } else if (mode === "xs") {
      mapHint.textContent = "Left-click two points on the DEM to draw a cross-section.";
    }
  } catch (_err) {
    if (seq !== demPreviewSeq) return;
    clearDemOverlay();
  }
}

function updateLayoutPreview() {
  if (!layoutPreview) return;
  const along = Number(runForm.along.value);
  const interval = Number(runForm.interval.value);
  const length = Number(runForm.length.value);
  const spacing = Number(runForm.sample_spacing.value);
  if (!(along >= 0) || !(interval > 0) || !(length > 0) || !(spacing > 0)) {
    layoutPreview.textContent = "Enter transect length, spacing along the river, and sample spacing.";
    return;
  }
  const nEach = Math.min(50, Math.floor(along / 2 / interval + 1e-9));
  const nTransects = 2 * nEach + 1;
  const nSamples = Math.min(2001, Math.floor(length / spacing) + 1);
  layoutPreview.textContent = `${nTransects} transects along the river · ${nSamples} DEM points on each transect`;
}

function centerlinePayload() {
  if (drawnLatLngs.length < 2) return null;
  return drawnLatLngs.map((ll) => [ll.lng, ll.lat]);
}

function refreshLineLabel() {
  const n = drawnLatLngs.length;
  undoBtn.disabled = n === 0;
  clearBtn.disabled = n === 0;
  snapBtn.disabled = n < 2;
  if (n === 0) {
    lineLabel.textContent = "No river line drawn yet — OSM will be used if available";
  } else if (n === 1) {
    lineLabel.textContent = "1 point placed — click again to start the river line";
  } else {
    lineLabel.textContent = `Drawn river centreline · ${n} points`;
  }
}

function redrawDraft() {
  draftLine.setLatLngs(drawnLatLngs);
  vertexLayer.clearLayers();
  drawnLatLngs.forEach((ll) => {
    L.circleMarker(ll, {
      radius: 5,
      color: "#13485c",
      fillColor: "#f4d2b0",
      fillOpacity: 1,
      weight: 2,
    }).addTo(vertexLayer);
  });
  refreshLineLabel();
}

function addVertex(latlng) {
  if (drawnLatLngs.length) {
    const last = drawnLatLngs[drawnLatLngs.length - 1];
    if (map.distance(last, latlng) < 4) return;
  }
  if (drawnLatLngs.length >= 500) {
    setStatus("The river line has enough points. Switch back to Place bridge, or undo.", "error");
    return;
  }
  drawnLatLngs.push(L.latLng(latlng.lat, latlng.lng));
  redrawDraft();
}

function xsVertexStyle() {
  return {
    radius: 6,
    color: "#7a2e12",
    fillColor: "#f4d2b0",
    fillOpacity: 1,
    weight: 2,
  };
}

function refreshXsLabel() {
  if (xsPoints.length === 0) {
    xsLabel.textContent = "No cross-section yet";
    clearXsBtn.disabled = true;
    return;
  }
  clearXsBtn.disabled = false;
  if (xsPoints.length === 1) {
    xsLabel.textContent = "First point placed — left-click a second point";
    return;
  }
  const metres = map.distance(xsPoints[0], xsPoints[1]);
  const samples = xsProfile ? xsProfile.n_samples : null;
  xsLabel.textContent = samples
    ? `Cross-section · ${metres.toFixed(0)} m · ${samples} DEM samples`
    : `Cross-section · ${metres.toFixed(0)} m`;
}

function drawXsLine() {
  xsLayer.clearLayers();
  xsDraftLine.setLatLngs(xsPoints.length === 1 ? xsPoints : []);
  if (!xsPoints.length) return;
  xsPoints.forEach((ll, index) => {
    L.circleMarker(ll, xsVertexStyle())
      .bindTooltip(index === 0 ? "A" : "B", { permanent: true, direction: "top", offset: [0, -8] })
      .addTo(xsLayer);
  });
  if (xsPoints.length === 2) {
    L.polyline(xsPoints, { color: "#c45c26", weight: 4, opacity: 0.95 }).addTo(xsLayer);
  }
}

function clearXsChart() {
  if (!xsCanvas) return;
  const ctx = xsCanvas.getContext("2d");
  const width = xsCanvas.clientWidth || 640;
  const height = xsCanvas.clientHeight || 200;
  xsCanvas.width = width;
  xsCanvas.height = height;
  ctx.clearRect(0, 0, width, height);
}

function drawXsProfile(profile) {
  if (!xsCanvas || !profile) return;
  const dists = profile.distance_m || [];
  const elevs = profile.elevation_m || [];
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(xsCanvas.clientWidth || 640, 320);
  const cssH = Math.max(xsCanvas.clientHeight || 200, 160);
  xsCanvas.width = Math.round(cssW * dpr);
  xsCanvas.height = Math.round(cssH * dpr);
  const ctx = xsCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#f4f1ea";
  ctx.fillRect(0, 0, cssW, cssH);

  const pad = { left: 52, right: 16, top: 14, bottom: 32 };
  const plotW = cssW - pad.left - pad.right;
  const plotH = cssH - pad.top - pad.bottom;
  const xmax = dists.length ? Number(dists[dists.length - 1]) : 1;
  const finite = elevs.filter((z) => z != null && Number.isFinite(Number(z))).map(Number);
  if (!finite.length || plotW < 10 || plotH < 10) {
    ctx.fillStyle = "#4d646e";
    ctx.font = "13px Segoe UI, system-ui, sans-serif";
    ctx.fillText("No elevations along this line.", pad.left, pad.top + 16);
    return;
  }
  let zmin = Math.min(...finite);
  let zmax = Math.max(...finite);
  if (zmax <= zmin) zmax = zmin + 1;
  const zPad = (zmax - zmin) * 0.08;
  zmin -= zPad;
  zmax += zPad;

  const xOf = (d) => pad.left + (Number(d) / Math.max(xmax, 1e-6)) * plotW;
  const yOf = (z) => pad.top + (1 - (Number(z) - zmin) / (zmax - zmin)) * plotH;

  ctx.strokeStyle = "#d7d0c4";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  ctx.fillStyle = "#4d646e";
  ctx.font = "11px Segoe UI, system-ui, sans-serif";
  ctx.fillText("Distance (m)", pad.left + plotW / 2 - 32, cssH - 8);
  ctx.save();
  ctx.translate(14, pad.top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Elevation (m)", -40, 0);
  ctx.restore();
  ctx.fillText(zmin.toFixed(1), 6, yOf(zmin) + 3);
  ctx.fillText(zmax.toFixed(1), 6, yOf(zmax) + 3);
  ctx.fillText("0", xOf(0) - 3, pad.top + plotH + 16);
  ctx.fillText(xmax.toFixed(0), xOf(xmax) - 12, pad.top + plotH + 16);

  const points = [];
  dists.forEach((dist, i) => {
    const z = elevs[i];
    if (z == null || !Number.isFinite(Number(z))) {
      points.push(null);
      return;
    }
    points.push([xOf(dist), yOf(z)]);
  });

  ctx.beginPath();
  let drawing = false;
  points.forEach((pt) => {
    if (!pt) {
      drawing = false;
      return;
    }
    if (!drawing) {
      ctx.moveTo(pt[0], pt[1]);
      drawing = true;
    } else {
      ctx.lineTo(pt[0], pt[1]);
    }
  });
  const first = points.find((pt) => pt);
  const last = [...points].reverse().find((pt) => pt);
  if (first && last) {
    ctx.lineTo(last[0], pad.top + plotH);
    ctx.lineTo(first[0], pad.top + plotH);
    ctx.closePath();
    ctx.fillStyle = "rgba(31, 111, 139, 0.22)";
    ctx.fill();
  }

  ctx.beginPath();
  drawing = false;
  points.forEach((pt) => {
    if (!pt) {
      drawing = false;
      return;
    }
    if (!drawing) {
      ctx.moveTo(pt[0], pt[1]);
      drawing = true;
    } else {
      ctx.lineTo(pt[0], pt[1]);
    }
  });
  ctx.strokeStyle = "#12232b";
  ctx.lineWidth = 2;
  ctx.stroke();
}

function showXsViewer() {
  xsViewer.hidden = false;
  requestAnimationFrame(() => {
    map.invalidateSize();
    if (xsProfile) drawXsProfile(xsProfile);
  });
}

function hideXsViewer() {
  xsViewer.hidden = true;
  requestAnimationFrame(() => map.invalidateSize());
}

function setXsMeta(text) {
  xsMeta.textContent = text;
}

function resetXsDrawing({ keepViewer = false } = {}) {
  xsPoints = [];
  xsProfile = null;
  xsDraftLine.setLatLngs([]);
  xsLayer.clearLayers();
  refreshXsLabel();
  if (keepViewer) {
    clearXsChart();
    setXsMeta("Left-click two points on the map.");
  } else {
    hideXsViewer();
  }
}

async function requestXsProfile(start, end) {
  const seq = (xsRequestSeq += 1);
  const body = new FormData(runForm);
  body.set("lon1", String(start.lng));
  body.set("lat1", String(start.lat));
  body.set("lon2", String(end.lng));
  body.set("lat2", String(end.lat));
  body.set("dem_source", currentDemSource());
  if (currentDemSource() === "upload") {
    const file = document.getElementById("dem-file");
    if (file && file.files && file.files[0]) body.set("dem", file.files[0]);
  }
  setXsMeta("Sampling the DEM…");
  showXsViewer();
  try {
    const res = await fetch("/api/cross-section", { method: "POST", body });
    const data = await res.json();
    if (seq !== xsRequestSeq) return;
    if (!res.ok) {
      setStatus(data.error || "Could not sample the DEM.", "error");
      setXsMeta(data.error || "Could not sample the DEM.");
      clearXsChart();
      return;
    }
    xsProfile = data;
    setStatus("");
    const source = data.source === "linz-lidar-1m" ? "New Zealand LiDAR 1m DEM" : "selected DEM";
    setXsMeta(
      `${fmt(data.length_m, 0)} m line · ${data.n_samples} samples every ${fmt(data.sample_spacing_m, 1)} m · ${source}`
    );
    refreshXsLabel();
    drawXsProfile(data);
  } catch (_err) {
    if (seq !== xsRequestSeq) return;
    setStatus("Could not reach the HydroBridge server.", "error");
    setXsMeta("Could not sample the DEM.");
  }
}

function handleXsClick(latlng) {
  if (xsPoints.length >= 2) {
    xsPoints = [];
    xsDraftLine.setLatLngs([]);
    xsLayer.clearLayers();
    setXsMeta("Click the second point. The profile will regenerate.");
  }
  if (xsPoints.length === 1 && map.distance(xsPoints[0], latlng) < 5) {
    return;
  }
  xsPoints.push(L.latLng(latlng.lat, latlng.lng));
  drawXsLine();
  refreshXsLabel();
  if (xsPoints.length === 1) {
    mapHint.textContent = "Click the second point on the DEM.";
    showXsViewer();
    if (!xsProfile) {
      clearXsChart();
      setXsMeta("Click the second point. The profile will appear here.");
    }
    return;
  }
  xsDraftLine.setLatLngs([]);
  mapHint.textContent = "Cross-section ready. Click two new points to replace it.";
  requestXsProfile(xsPoints[0], xsPoints[1]);
}

function setMode(next) {
  mode = next;
  document.body.classList.toggle("mode-draw", mode === "draw");
  document.body.classList.toggle("mode-xs", mode === "xs");
  modePinBtn.setAttribute("aria-pressed", String(mode === "pin"));
  modeDrawBtn.setAttribute("aria-pressed", String(mode === "draw"));
  modeXsBtn.setAttribute("aria-pressed", String(mode === "xs"));
  drawingStroke = false;
  if (mode === "draw") {
    map.dragging.disable();
    map.doubleClickZoom.disable();
    mapHint.textContent = "Click or drag along the river. Double-click when the line is done.";
  } else if (mode === "xs") {
    map.dragging.enable();
    map.doubleClickZoom.disable();
    mapHint.textContent = xsPoints.length === 1
      ? "Click the second point on the DEM."
      : "Left-click two points on the DEM to draw a cross-section.";
  } else {
    map.dragging.enable();
    map.doubleClickZoom.enable();
    mapHint.textContent = "Click the map to place the bridge pin.";
  }
}

async function refreshPlaceName(lat, lon) {
  placeLabel.textContent = `Selected site: ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  try {
    const res = await fetch(`/api/reverse?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
    const data = await res.json();
    if (data.label) {
      placeLabel.textContent = data.label;
    }
  } catch (_err) {
    /* keep coordinate fallback */
  }
}

function setLocation(lat, lon, { fly = false, zoom } = {}) {
  latInput.value = Number(lat).toFixed(6);
  lonInput.value = Number(lon).toFixed(6);
  marker.setLatLng([lat, lon]);
  if (fly) {
    map.flyTo([lat, lon], zoom || Math.max(map.getZoom(), 14), { duration: 0.7 });
  } else {
    map.panTo([lat, lon]);
  }
  marker.openPopup();
  refreshPlaceName(Number(lat), Number(lon));
  scheduleDemPreview();
}

function nearestOnDrawnLine(lat, lon) {
  if (drawnLatLngs.length < 2) return null;
  const origin = L.latLng(lat, lon);
  let best = drawnLatLngs[0];
  let bestDist = map.distance(origin, best);
  for (let i = 1; i < drawnLatLngs.length; i += 1) {
    const a = drawnLatLngs[i - 1];
    const b = drawnLatLngs[i];
    const steps = 8;
    for (let s = 0; s <= steps; s += 1) {
      const t = s / steps;
      const candidate = L.latLng(a.lat + (b.lat - a.lat) * t, a.lng + (b.lng - a.lng) * t);
      const dist = map.distance(origin, candidate);
      if (dist < bestDist) {
        best = candidate;
        bestDist = dist;
      }
    }
  }
  return best;
}

function drawRunGeometry(payload) {
  overlay.clearLayers();
  if (drawnLatLngs.length < 2 && payload.centerline && payload.centerline.length) {
    const line = payload.centerline.map(([lon, lat]) => [lat, lon]);
    L.polyline(line, { color: "#1f6f8b", weight: 4, opacity: 0.9 }).addTo(overlay);
  }
  (payload.transects || []).forEach((tran) => {
    const line = (tran.coords || []).map(([lon, lat]) => [lat, lon]);
    L.polyline(line, { color: "#c45c26", weight: 2, opacity: 0.85 }).addTo(overlay);
    (tran.samples || []).forEach(([lon, lat]) => {
      L.circleMarker([lat, lon], {
        radius: 3,
        color: "#c45c26",
        fillColor: "#fff",
        fillOpacity: 1,
        weight: 1.5,
      }).addTo(overlay);
    });
  });
}

function renderResults(payload) {
  resultsEl.hidden = false;
  resultsBody.innerHTML = "";
  plotsEl.innerHTML = "";
  if (payload.centerline_source === "drawn") {
    resultsNote.textContent = "Transects follow the river centreline you drew, centred on the bridge pin.";
  } else if (payload.used_synthetic_centerline) {
    resultsNote.textContent = "No OpenStreetMap waterway was found within 500 m, so a short east–west centreline through the pin was used. Draw the river on the map for a better result.";
  } else {
    resultsNote.textContent = "Transects follow a nearby OpenStreetMap waterway, centred on the selected bridge pin.";
  }
  if (payload.layout) {
    const extra = `${payload.layout.n_transects} transects along ${payload.layout.along_m ?? "—"} m of river, sampled every ${payload.layout.sample_spacing_m} m.`;
    const flow = payload.layout.flow_m3_s != null ? ` Target Q ${payload.layout.flow_m3_s} m³/s, n=${payload.layout.mannings_n}, slope from centreline S=${Number(payload.layout.slope).toExponential(2)}.` : "";
    const dem = payload.layout.dem_source === "linz-lidar-1m" ? " Elevations from the New Zealand LiDAR 1m DEM (LINZ layer 121859)." : "";
    resultsNote.textContent = `${resultsNote.textContent} ${extra}${flow}${dem}`;
  }
  summaryLink.hidden = !payload.summary_xlsx;
  summaryLink.href = payload.summary_xlsx || "#";

  (payload.summary || []).forEach((row) => {
    const status = row.overtopped ? "Overtops banks" : (row.conveys ? "OK" : "Cannot convey");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.transect}</td>
      <td>${fmt(row.offset_m, 1)}</td>
      <td>${row.n_samples ?? "—"}</td>
      <td>${fmt(row.water_level_m, 2)}</td>
      <td>${fmt(row.max_depth_m, 2)}</td>
      <td>${fmt(row.width_m, 1)}</td>
      <td>${fmt(row.area_m2, 1)}</td>
      <td>${fmt(row.hydraulic_radius_m, 2)}</td>
      <td>${fmt(row.velocity_m_s, 2)}</td>
      <td>${fmt(row.discharge_m3_s, 2)}</td>
      <td>${status}</td>`;
    resultsBody.appendChild(tr);

    const fig = document.createElement("figure");
    fig.innerHTML = `
      <img src="${row.plot}" alt="Cross-section for transect ${row.transect}">
      <figcaption>Transect ${row.transect} · offset ${fmt(row.offset_m, 0)} m
        · water level ${fmt(row.water_level_m, 2)} m
        · <a href="${row.csv}">CSV</a></figcaption>`;
    plotsEl.appendChild(fig);
  });
  resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
}

function hideSearchResults() {
  searchResults.hidden = true;
  searchResults.innerHTML = "";
}

map.on("click", (event) => {
  if (mode === "draw") {
    addVertex(event.latlng);
    return;
  }
  if (mode === "xs") {
    handleXsClick(event.latlng);
    return;
  }
  setLocation(event.latlng.lat, event.latlng.lng);
});

map.on("dblclick", (event) => {
  if (mode !== "draw") return;
  L.DomEvent.stop(event);
  if (drawnLatLngs.length >= 2) setMode("pin");
});

map.on("mousedown", (event) => {
  if (mode !== "draw" || event.originalEvent.button !== 0) return;
  drawingStroke = true;
  addVertex(event.latlng);
});

map.on("mousemove", (event) => {
  if (mode === "xs" && xsPoints.length === 1) {
    xsDraftLine.setLatLngs([xsPoints[0], event.latlng]);
    return;
  }
  if (mode !== "draw" || !drawingStroke) return;
  addVertex(event.latlng);
});

map.on("mouseup", () => {
  drawingStroke = false;
});

map.getContainer().addEventListener("mouseleave", () => {
  drawingStroke = false;
});

marker.on("dragend", () => {
  const pos = marker.getLatLng();
  setLocation(pos.lat, pos.lng);
});

modePinBtn.addEventListener("click", () => setMode("pin"));
modeDrawBtn.addEventListener("click", () => setMode("draw"));
modeXsBtn.addEventListener("click", () => setMode("xs"));

undoBtn.addEventListener("click", () => {
  drawnLatLngs.pop();
  redrawDraft();
});

clearBtn.addEventListener("click", () => {
  drawnLatLngs = [];
  redrawDraft();
});

clearXsBtn.addEventListener("click", () => {
  resetXsDrawing();
  if (mode === "xs") {
    mapHint.textContent = "Left-click two points on the DEM to draw a cross-section.";
  }
});

snapBtn.addEventListener("click", () => {
  const { lat, lon } = currentLatLon();
  const snapped = nearestOnDrawnLine(lat, lon);
  if (!snapped) return;
  setLocation(snapped.lat, snapped.lng, { fly: true });
});

document.getElementById("apply-coords").addEventListener("click", () => {
  const { lat, lon } = currentLatLon();
  if (Number.isNaN(lat) || Number.isNaN(lon)) {
    setStatus("Enter numeric latitude and longitude.", "error");
    return;
  }
  setLocation(lat, lon, { fly: true });
});

document.getElementById("wellington").addEventListener("click", () => {
  setLocation(WELLINGTON.lat, WELLINGTON.lon, { fly: true, zoom: WELLINGTON.zoom });
});

runForm.addEventListener("input", (event) => {
  updateLayoutPreview();
  if (event.target && (event.target.name === "along" || event.target.name === "length")) {
    scheduleDemPreview();
  }
});
runForm.addEventListener("change", (event) => {
  if (event.target.name === "dem_source") {
    demFileWrap.hidden = event.target.value !== "upload";
    scheduleDemPreview();
    return;
  }
  if (event.target.name === "dem" || event.target.name === "along" || event.target.name === "length") {
    scheduleDemPreview();
  }
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  hideSearchResults();
  if (query.length < 2) return;
  setStatus("Searching…");
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    if (!res.ok) {
      setStatus(data.error || "Search failed.", "error");
      return;
    }
    if (!data.length) {
      setStatus("No matching places. Try a river name or nearby town.", "error");
      return;
    }
    setStatus("");
    data.forEach((hit) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = hit.label;
      btn.addEventListener("click", () => {
        setLocation(hit.lat, hit.lon, { fly: true, zoom: 15 });
        hideSearchResults();
        searchInput.value = hit.label;
      });
      li.appendChild(btn);
      searchResults.appendChild(li);
    });
    searchResults.hidden = false;
  } catch (_err) {
    setStatus("Place search is unavailable.", "error");
  }
});

document.addEventListener("click", (event) => {
  if (!searchForm.contains(event.target)) hideSearchResults();
});

runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const { lat, lon } = currentLatLon();
  if (Number.isNaN(lat) || Number.isNaN(lon)) {
    setStatus("Set a bridge location first.", "error");
    return;
  }
  const body = new FormData(runForm);
  body.set("lat", String(lat));
  body.set("lon", String(lon));
  const drawn = centerlinePayload();
  if (drawn) body.set("centerline", JSON.stringify(drawn));
  runBtn.disabled = true;
  setStatus("Running screening… this can take a few seconds.");
  try {
    const res = await fetch("/api/run", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) {
      setStatus(data.error || "Screening failed.", "error");
      return;
    }
    setStatus("Screening complete.", "ok");
    setMode("pin");
    drawRunGeometry(data);
    renderResults(data);
    marker.setLatLng([data.lat, data.lon]);
  } catch (_err) {
    setStatus("Could not reach the HydroBridge server.", "error");
  } finally {
    runBtn.disabled = false;
  }
});

window.addEventListener("resize", () => {
  if (!xsViewer.hidden && xsProfile) drawXsProfile(xsProfile);
});

refreshPlaceName(WELLINGTON.lat, WELLINGTON.lon);
refreshLineLabel();
updateLayoutPreview();
scheduleDemPreview();
