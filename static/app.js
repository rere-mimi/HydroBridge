const CHRISTCHURCH = { lat: -43.532, lon: 172.6362, zoom: 13 };

const mapHint = document.getElementById("map-hint");
const latInput = document.getElementById("lat");
const lonInput = document.getElementById("lon");
const alongInput = document.getElementById("along");
const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-q");
const searchResults = document.getElementById("search-results");
const statusEl = document.getElementById("status");
const runStatusEl = document.getElementById("run-status");
const runForm = document.getElementById("run-form");
const runBtn = document.getElementById("run-btn");
const stopBtn = document.getElementById("stop-btn");
const resultsEl = document.getElementById("results");
const resultsBody = document.getElementById("results-body");
const resultsNote = document.getElementById("results-note");
const plotsEl = document.getElementById("plots");
const summaryLink = document.getElementById("summary-link");
const demTilesEl = document.getElementById("dem-tiles");
const modeDrawBtn = document.getElementById("mode-draw");
const modeXsBtn = document.getElementById("mode-xs");
const undoBtn = document.getElementById("undo-vertex");
const clearBtn = document.getElementById("clear-line");
const finishBtn = document.getElementById("finish-line");
const clearXsBtn = document.getElementById("clear-xs");
const layoutPreview = document.getElementById("layout-preview");
const analysisLengthEl = document.getElementById("analysis-length");
const bridgeSummary = document.getElementById("bridge-summary");
const legendList = document.getElementById("legend-list");
const coachActions = document.getElementById("coach-actions");
const toolActions = document.getElementById("tool-actions");
const nameModal = document.getElementById("name-modal");
const nameForm = document.getElementById("name-form");
const nameInput = document.getElementById("bridge-name");
const nameCoords = document.getElementById("name-coords");
const xsViewer = document.getElementById("xs-viewer");
const xsMeta = document.getElementById("xs-meta");
const xsCanvas = document.getElementById("xs-canvas");
const busyEl = document.getElementById("busy");
const busyText = document.getElementById("busy-text");
const busyPct = document.getElementById("busy-pct");
const busyBar = document.getElementById("busy-bar");
const busyBarFill = document.getElementById("busy-bar-fill");
const legendEl = document.getElementById("legend");
const demOpacityControl = document.getElementById("dem-opacity-control");
const demOpacityInput = document.getElementById("dem-opacity");
const demOpacityValue = document.getElementById("dem-opacity-value");

const map = L.map("map", { doubleClickZoom: false }).setView(
  [CHRISTCHURCH.lat, CHRISTCHURCH.lon],
  CHRISTCHURCH.zoom
);

function googleBasemap(lyrs) {
  return L.tileLayer("https://{s}.google.com/vt/lyrs=" + lyrs + "&x={x}&y={y}&z={z}", {
    maxZoom: 21,
    subdomains: ["mt0", "mt1", "mt2", "mt3"],
    attribution: "&copy; Google",
  });
}

const BASEMAPS = {
  map: googleBasemap("m"),
  earth: googleBasemap("y"),
  topo: googleBasemap("p"),
};
const BASEMAP_IDS = ["map", "earth", "topo"];
const BASEMAP_LABELS = {
  map: "Google Map",
  earth: "Google Earth",
  topo: "Google Topo",
};
let currentBasemapId = "map";
try {
  const saved = localStorage.getItem("hydroscreen-basemap");
  if (BASEMAP_IDS.includes(saved)) currentBasemapId = saved;
} catch (_err) {
  /* ignore */
}
BASEMAPS[currentBasemapId].addTo(map);

function syncBasemapButtons() {
  document.querySelectorAll("[data-basemap]").forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.dataset.basemap === currentBasemapId ? "true" : "false");
  });
}

function setBasemap(id) {
  if (!BASEMAPS[id] || id === currentBasemapId) {
    syncBasemapButtons();
    refreshLegend();
    return;
  }
  map.removeLayer(BASEMAPS[currentBasemapId]);
  currentBasemapId = id;
  BASEMAPS[id].addTo(map);
  BASEMAPS[id].bringToBack();
  try {
    localStorage.setItem("hydroscreen-basemap", id);
  } catch (_err) {
    /* ignore */
  }
  syncBasemapButtons();
  refreshLegend();
}

document.querySelectorAll("[data-basemap]").forEach((btn) => {
  btn.addEventListener("click", () => setBasemap(btn.dataset.basemap));
});
syncBasemapButtons();

map.createPane("demPane");
map.getPane("demPane").style.zIndex = 350;
map.getPane("demPane").style.pointerEvents = "none";

const overlay = L.layerGroup().addTo(map);
const draftLine = L.polyline([], {
  color: "#0369a1",
  weight: 5,
  opacity: 0.95,
  interactive: false,
}).addTo(map);
const vertexLayer = L.layerGroup().addTo(map);
const xsLayer = L.layerGroup().addTo(map);
const xsDraftLine = L.polyline([], {
  color: "#7c3aed",
  weight: 3,
  dashArray: "6 6",
  opacity: 0.9,
  interactive: false,
}).addTo(map);

const pinIcon = L.divIcon({
  className: "",
  html: '<span style="display:block;width:16px;height:16px;border-radius:999px;background:#e11d48;border:2px solid #fff;box-shadow:0 2px 8px rgba(15,23,42,.35)"></span>',
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});

let marker = null;
let mode = "idle";
let bridgeName = "";
let pendingPin = null;
let drawnLatLngs = [];
let drawingStroke = false;
let demOverlay = null;
let demOverlayUrl = null;
let demPreviewTimer = null;
let demPreviewSeq = 0;
let xsPoints = [];
let xsProfile = null;
let xsRequestSeq = 0;
let ranTransects = false;
let runAbort = null;
let runJobId = null;
let runBusy = false;
let runReady = false;
let runBlockReason = "Double-click the map to pin a bridge, then draw the river.";
let busyCount = 0;
let lastPreviewKey = "";
let demTiles = [];
let demProgressOn = false;
let demTransparency = 50;
try {
  const savedTransparency = localStorage.getItem("hydroscreen-dem-transparency");
  if (savedTransparency != null && savedTransparency !== "") {
    const parsed = Number(savedTransparency);
    if (Number.isFinite(parsed)) demTransparency = Math.max(0, Math.min(100, parsed));
  }
} catch (_err) {
  /* ignore */
}

function demOverlayOpacity() {
  return Math.max(0, Math.min(1, 1 - demTransparency / 100));
}

function syncDemOpacityControl() {
  if (demOpacityInput) demOpacityInput.value = String(Math.round(demTransparency));
  if (demOpacityValue) demOpacityValue.textContent = `${Math.round(demTransparency)}%`;
  if (demOpacityControl) demOpacityControl.hidden = !demOverlay;
  if (legendEl) legendEl.classList.toggle("has-dem", Boolean(demOverlay));
}

function applyDemTransparency() {
  if (demOverlay) demOverlay.setOpacity(demOverlayOpacity());
  if (demOpacityValue) demOpacityValue.textContent = `${Math.round(demTransparency)}%`;
  refreshLegend();
}

function setBusy(on, label) {
  if (on) {
    busyCount += 1;
    if (label && busyText) busyText.textContent = label;
    if (busyEl) busyEl.hidden = false;
    document.body.classList.add("is-busy");
  } else {
    busyCount = Math.max(0, busyCount - 1);
    if (busyCount === 0) {
      if (busyEl) busyEl.hidden = true;
      document.body.classList.remove("is-busy");
    }
  }
}

function showDemProgress(percent, label) {
  if (!demProgressOn) {
    demProgressOn = true;
    setBusy(true, label || "Downloading DEM…");
  } else if (label && busyText) {
    busyText.textContent = label;
  }
  if (busyEl) busyEl.classList.add("is-progress");
  const pct = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  if (busyBarFill) busyBarFill.style.width = `${pct}%`;
  if (busyPct) {
    busyPct.hidden = false;
    busyPct.textContent = `${pct}%`;
  }
  if (busyBar) busyBar.hidden = false;
}

function hideDemProgress() {
  if (busyBarFill) busyBarFill.style.width = "0%";
  if (busyPct) busyPct.hidden = true;
  if (busyBar) busyBar.hidden = true;
  if (busyEl) busyEl.classList.remove("is-progress");
  if (demProgressOn) {
    demProgressOn = false;
    setBusy(false);
  }
}

function setStatus(message, kind) {
  const text = message || "";
  const cls = "status" + (kind ? " " + kind : "");
  statusEl.textContent = text;
  statusEl.className = cls;
  if (runStatusEl) {
    runStatusEl.textContent = text;
    runStatusEl.className = cls;
  }
}

function setRunAvailability() {
  runBtn.disabled = runBusy;
  runBtn.setAttribute("aria-disabled", runBusy || !runReady ? "true" : "false");
  runBtn.classList.toggle("is-disabled", runBusy || !runReady);
  runBtn.replaceChildren();
  if (runBusy) {
    const spin = document.createElement("span");
    spin.className = "spinner spinner-btn";
    spin.setAttribute("aria-hidden", "true");
    runBtn.append(spin, document.createTextNode(" Running…"));
  } else {
    runBtn.textContent = "Run screening";
  }
}

function setRunning(running) {
  if (running && !runBusy) setBusy(true, "Running screening…");
  if (!running && runBusy) setBusy(false);
  runBusy = running;
  stopBtn.hidden = !running;
  stopBtn.disabled = !running;
  setRunAvailability();
}

function newJobId() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch (_err) {
    /* fall through */
  }
  return `job-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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

function lineLengthM(latlngs) {
  let metres = 0;
  for (let i = 1; i < latlngs.length; i += 1) {
    metres += map.distance(latlngs[i - 1], latlngs[i]);
  }
  return metres;
}

function closeHelp() {
  document.querySelectorAll(".help-pop").forEach((el) => {
    el.hidden = true;
  });
  document.querySelectorAll(".help").forEach((btn) => {
    btn.setAttribute("aria-expanded", "false");
  });
}

function setLegend(items) {
  legendList.innerHTML = "";
  items.forEach((item) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="swatch ${item.swatch}"></span> ${item.label}`;
    legendList.appendChild(li);
  });
}

function refreshLegend() {
  const items = [{ swatch: `basemap ${currentBasemapId}`, label: BASEMAP_LABELS[currentBasemapId] }];
  if (marker && bridgeName) {
    items.push({ swatch: "bridge", label: `Bridge — ${bridgeName}` });
  }
  if (drawnLatLngs.length >= 2) {
    items.push({
      swatch: "river",
      label: `River centreline — ${lineLengthM(drawnLatLngs).toFixed(0)} m analysis length`,
    });
  } else if (drawnLatLngs.length === 1) {
    items.push({ swatch: "river", label: "River centreline (drawing…)" });
  }
  if (demOverlay) {
    const sheets = demTiles.length ? ` · ${demTiles.join(", ")}` : "";
    items.push({
      swatch: "dem",
      label: `LiDAR DEM 500 m${sheets} (${Math.round(demTransparency)}% transparent)`,
    });
  }
  if (ranTransects) {
    const spacing = Number(runForm.interval.value);
    const length = Number(runForm.length.value);
    items.push({
      swatch: "transect",
      label: `Transects — ${length} m wide, every ${spacing} m`,
    });
  }
  if (xsPoints.length === 2) {
    items.push({
      swatch: "xs",
      label: `DEM cross-section — ${map.distance(xsPoints[0], xsPoints[1]).toFixed(0)} m`,
    });
  }
  setLegend(items);
  syncDemOpacityControl();
}

function updateLayoutPreview() {
  const along = lineLengthM(drawnLatLngs);
  const interval = Number(runForm.interval.value);
  const length = Number(runForm.length.value);
  const spacing = Number(runForm.sample_spacing.value);
  alongInput.value = along > 0 ? String(Math.round(along)) : "300";
  analysisLengthEl.textContent = along >= 2 ? `${along.toFixed(0)} m` : "—";
  if (along < 2) {
    layoutPreview.textContent = "Draw the centreline to set the analysis length.";
    runReady = false;
    runBlockReason = "Draw the river centreline first. Its length is the analysis reach.";
    setRunAvailability();
    return;
  }
  if (!(interval > 0) || !(length > 0) || !(spacing > 0)) {
    layoutPreview.textContent = "Enter transect length and spacing.";
    runReady = false;
    runBlockReason = "Enter transect length, spacing, and sample spacing before running.";
    setRunAvailability();
    return;
  }
  const nTransects = Math.floor(along / interval + 1e-9) + 1;
  const nSamples = Math.min(2001, Math.floor(length / spacing) + 1);
  layoutPreview.textContent = `${nTransects} transects along the drawn ${along.toFixed(0)} m · ${nSamples} DEM points each`;
  runReady = Boolean(marker);
  runBlockReason = marker
    ? ""
    : "Double-click the map to pin a bridge first.";
  setRunAvailability();
}

function setCoach(text) {
  mapHint.textContent = text;
}

function syncChrome() {
  const drawing = mode === "draw";
  const hasPin = Boolean(marker);
  const lineReady = drawnLatLngs.length >= 2;
  coachActions.hidden = !drawing;
  toolActions.hidden = drawing || !hasPin;
  runForm.hidden = !lineReady;
  undoBtn.disabled = drawnLatLngs.length === 0;
  clearBtn.disabled = drawnLatLngs.length === 0;
  finishBtn.disabled = drawnLatLngs.length < 2;
  clearXsBtn.disabled = xsPoints.length === 0;
  if (bridgeName && marker) {
    const { lat, lon } = currentLatLon();
    bridgeSummary.textContent = `${bridgeName} · ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  }
  refreshLegend();
  updateLayoutPreview();
  adaptMapLayout();
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
  demTiles = [];
  if (demTilesEl) {
    demTilesEl.hidden = true;
    demTilesEl.textContent = "";
  }
  refreshLegend();
}

function scheduleDemPreview() {
  clearTimeout(demPreviewTimer);
  demPreviewTimer = setTimeout(refreshDemOverlay, 700);
}

async function readPreviewStream(res) {
  if (!res.body || !res.body.getReader) {
    return res.json();
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let last = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        last = JSON.parse(line);
      } catch (_err) {
        continue;
      }
      if (last.percent != null) showDemProgress(last.percent, last.message);
    }
  }
  if (buf.trim()) {
    try {
      last = JSON.parse(buf);
      if (last && last.percent != null) showDemProgress(last.percent, last.message);
    } catch (_err) {
      /* keep last complete event */
    }
  }
  return last || {};
}

async function refreshDemOverlay() {
  const { lat, lon } = currentLatLon();
  if (Number.isNaN(lat) || Number.isNaN(lon)) return;
  const key = [lat.toFixed(5), lon.toFixed(5), alongInput.value || "300", runForm.length.value].join("|");
  if (key === lastPreviewKey && demOverlay) return;
  const seq = (demPreviewSeq += 1);
  const body = new FormData();
  body.set("lat", String(lat));
  body.set("lon", String(lon));
  body.set("along", alongInput.value || "300");
  body.set("length", runForm.length.value);
  body.set("stream", "1");
  showDemProgress(0, "Finding LINZ tiles…");
  try {
    const res = await fetch("/api/dem-preview?stream=1", { method: "POST", body });
    const data = await readPreviewStream(res);
    if (seq !== demPreviewSeq) return;
    if (!res.ok || data.error || !data.png || !data.bounds) {
      clearDemOverlay();
      lastPreviewKey = "";
      setStatus(data.error || "Could not download the LINZ DEM for this site.", "error");
      return;
    }
    const bytes = Uint8Array.from(atob(data.png), (ch) => ch.charCodeAt(0));
    clearDemOverlay();
    demTiles = Array.isArray(data.tiles) ? data.tiles : [];
    if (demTilesEl) {
      demTilesEl.hidden = demTiles.length === 0;
      demTilesEl.textContent = demTiles.length
        ? `Tiles for this pin: ${demTiles.join(", ")}`
        : "";
    }
    demOverlayUrl = URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    demOverlay = L.imageOverlay(demOverlayUrl, data.bounds, {
      opacity: demOverlayOpacity(),
      pane: "demPane",
      interactive: false,
      className: "dem-overlay",
    }).addTo(map);
    lastPreviewKey = key;
    refreshLegend();
    adaptMapLayout();
    map.fitBounds(data.bounds, { padding: [28, 28], maxZoom: 17, animate: false });
  } catch (err) {
    if (seq !== demPreviewSeq) return;
    clearDemOverlay();
    lastPreviewKey = "";
    setStatus((err && err.message) || "Could not download the LINZ DEM.", "error");
  } finally {
    if (seq === demPreviewSeq) hideDemProgress();
  }
}

function centerlinePayload() {
  if (drawnLatLngs.length < 2) return null;
  return drawnLatLngs.map((ll) => [ll.lng, ll.lat]);
}

function redrawDraft() {
  draftLine.setLatLngs(drawnLatLngs);
  vertexLayer.clearLayers();
  drawnLatLngs.forEach((ll) => {
    L.circleMarker(ll, {
      radius: 5,
      color: "#0f172a",
      fillColor: "#7dd3fc",
      fillOpacity: 1,
      weight: 2,
      interactive: false,
    }).addTo(vertexLayer);
  });
  syncChrome();
}

function addVertex(latlng) {
  if (drawnLatLngs.length) {
    const last = drawnLatLngs[drawnLatLngs.length - 1];
    if (map.distance(last, latlng) < 4) return;
  }
  if (drawnLatLngs.length >= 500) {
    setStatus("The river line has enough points. Finish it, or undo.", "error");
    return;
  }
  drawnLatLngs.push(L.latLng(latlng.lat, latlng.lng));
  redrawDraft();
}

function finishCentreline() {
  if (drawnLatLngs.length < 2) {
    setStatus("Add at least two points along the river before finishing.", "error");
    return;
  }
  ranTransects = false;
  overlay.clearLayers();
  setMode("params");
  scheduleDemPreview();
}

function xsVertexStyle() {
  return {
    radius: 6,
    color: "#5b21b6",
    fillColor: "#ddd6fe",
    fillOpacity: 1,
    weight: 2,
    interactive: false,
  };
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
    L.polyline(xsPoints, {
      color: "#7c3aed",
      weight: 4,
      opacity: 0.95,
      interactive: false,
    }).addTo(xsLayer);
  }
  refreshLegend();
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
  ctx.fillStyle = "#f8fafc";
  ctx.fillRect(0, 0, cssW, cssH);

  const pad = { left: 52, right: 16, top: 14, bottom: 32 };
  const plotW = cssW - pad.left - pad.right;
  const plotH = cssH - pad.top - pad.bottom;
  const xmax = dists.length ? Number(dists[dists.length - 1]) : 1;
  const finite = elevs.filter((z) => z != null && Number.isFinite(Number(z))).map(Number);
  if (!finite.length || plotW < 10 || plotH < 10) {
    ctx.fillStyle = "#64748b";
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

  ctx.strokeStyle = "#e2e8f0";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  ctx.fillStyle = "#64748b";
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
    ctx.fillStyle = "rgba(2, 132, 199, 0.22)";
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
  ctx.strokeStyle = "#0f172a";
  ctx.lineWidth = 2;
  ctx.stroke();
}

function showXsViewer() {
  xsViewer.hidden = false;
  adaptMapLayout();
  requestAnimationFrame(() => {
    if (xsProfile) drawXsProfile(xsProfile);
  });
}

function hideXsViewer() {
  xsViewer.hidden = true;
  adaptMapLayout();
}

function setXsMeta(text) {
  xsMeta.textContent = text;
}

function resetXsDrawing({ keepViewer = false } = {}) {
  xsPoints = [];
  xsProfile = null;
  xsDraftLine.setLatLngs([]);
  xsLayer.clearLayers();
  refreshLegend();
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
  setXsMeta("Sampling the DEM…");
  showXsViewer();
  setBusy(true, "Sampling DEM…");
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
    drawXsProfile(data);
  } catch (_err) {
    if (seq !== xsRequestSeq) return;
    setStatus("Could not reach the HydroBridge server.", "error");
    setXsMeta("Could not sample the DEM.");
  } finally {
    setBusy(false);
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
  if (xsPoints.length === 1) {
    setCoach("Click the second point on the DEM.");
    setStatus("First cross-section point placed. Click the second point.", "ok");
    showXsViewer();
    if (!xsProfile) {
      clearXsChart();
      setXsMeta("Click the second point. The profile will appear here.");
    }
    return;
  }
  xsDraftLine.setLatLngs([]);
  setCoach("Cross-section ready. Click two new points to replace it, or set hydrology parameters.");
  requestXsProfile(xsPoints[0], xsPoints[1]);
}

function setMode(next) {
  mode = next;
  document.body.classList.toggle("mode-draw", mode === "draw");
  document.body.classList.toggle("mode-xs", mode === "xs");
  modeDrawBtn.setAttribute("aria-pressed", String(mode === "draw"));
  modeXsBtn.setAttribute("aria-pressed", String(mode === "xs"));
  drawingStroke = false;
  if (mode === "draw") {
    map.dragging.disable();
    setCoach("Click or drag along the river through the bridge. Double-click the last point when the line is done.");
  } else if (mode === "xs") {
    map.dragging.disable();
    setCoach(
      xsPoints.length === 1
        ? "Click the second point on the DEM."
        : "Cross-section tool is on. Left-click two points on the map to sample the DEM."
    );
    setStatus("Cross-section tool is on. Click two points on the map.", "ok");
  } else if (mode === "params") {
    map.dragging.enable();
    setCoach("Set transect length and spacing, then Run screening. Use Cross-section to sample the DEM, or double-click to pin a different bridge.");
  } else if (marker) {
    map.dragging.enable();
    setCoach("Draw the river centreline through the bridge, or double-click elsewhere to move the pin.");
  } else {
    map.dragging.enable();
    setCoach("Navigate the map, then double-click a bridge to drop a pin.");
  }
  syncChrome();
}

function openNameModal(latlng) {
  pendingPin = L.latLng(latlng.lat, latlng.lng);
  nameCoords.textContent = `${pendingPin.lat.toFixed(5)}, ${pendingPin.lng.toFixed(5)}`;
  nameInput.value = bridgeName || "";
  nameModal.hidden = false;
  nameInput.focus();
  nameInput.select();
}

function closeNameModal() {
  nameModal.hidden = true;
  pendingPin = null;
}

function bindMarker(lat, lon) {
  if (!marker) {
    marker = L.marker([lat, lon], { draggable: true, icon: pinIcon }).addTo(map);
    marker.on("dragend", () => {
      const pos = marker.getLatLng();
      latInput.value = pos.lat.toFixed(6);
      lonInput.value = pos.lng.toFixed(6);
      marker.bindPopup(bridgeName || "Bridge").openPopup();
      syncChrome();
      scheduleDemPreview();
    });
  } else {
    marker.setLatLng([lat, lon]);
  }
  marker.bindPopup(bridgeName || "Bridge").openPopup();
}

function placeBridge(lat, lon, name) {
  bridgeName = name;
  latInput.value = Number(lat).toFixed(6);
  lonInput.value = Number(lon).toFixed(6);
  bindMarker(lat, lon);
  drawnLatLngs = [];
  ranTransects = false;
  overlay.clearLayers();
  redrawDraft();
  resultsEl.hidden = true;
  setMode("draw");
  scheduleDemPreview();
}

function drawRunGeometry(payload) {
  overlay.clearLayers();
  (payload.transects || []).forEach((tran) => {
    const line = (tran.coords || []).map(([lon, lat]) => [lat, lon]);
    L.polyline(line, { color: "#ea580c", weight: 2, opacity: 0.85, interactive: false }).addTo(overlay);
    (tran.samples || []).forEach(([lon, lat]) => {
      L.circleMarker([lat, lon], {
        radius: 3,
        color: "#ea580c",
        fillColor: "#fff",
        fillOpacity: 1,
        weight: 1.5,
        interactive: false,
      }).addTo(overlay);
    });
  });
  ranTransects = true;
  refreshLegend();
}

function renderResults(payload) {
  resultsEl.hidden = false;
  adaptMapLayout();
  resultsBody.innerHTML = "";
  plotsEl.innerHTML = "";
  const lengthNote = payload.layout?.along_m != null
    ? `Transects cover the ${Number(payload.layout.along_m).toFixed(0)} m centreline you drew.`
    : "Transects follow the river centreline you drew.";
  resultsNote.textContent = `${bridgeName ? bridgeName + " · " : ""}${lengthNote}`;
  if (payload.layout) {
    const extra = `${payload.layout.n_transects} transects, sampled every ${payload.layout.sample_spacing_m} m.`;
    const flow = payload.layout.flow_m3_s != null
      ? ` Target Q ${payload.layout.flow_m3_s} m³/s, n=${payload.layout.mannings_n}, slope from centreline S=${Number(payload.layout.slope).toExponential(2)}.`
      : "";
    const clipM = payload.layout.clip_size_m != null
      ? `, ${Number(payload.layout.clip_size_m).toFixed(0)} m clip`
      : "";
    const sheets = Array.isArray(payload.layout.tiles) && payload.layout.tiles.length
      ? ` from ${payload.layout.tiles.join(", ")}`
      : "";
    const dem = payload.layout.dem_source === "linz-lidar-1m"
      ? ` Elevations from the New Zealand LiDAR 1m DEM (LINZ layer 121859${clipM}${sheets}).`
      : "";
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

function applyMapClick(latlng) {
  if (!nameModal.hidden) return;
  if (mode === "draw") {
    addVertex(latlng);
    return;
  }
  if (mode === "xs") {
    handleXsClick(latlng);
  }
}

map.on("click", (event) => {
  applyMapClick(event.latlng);
});

map.on("dblclick", (event) => {
  L.DomEvent.stop(event);
  if (!nameModal.hidden) return;
  if (mode === "draw") {
    addVertex(event.latlng);
    if (drawnLatLngs.length >= 2) finishCentreline();
    return;
  }
  openNameModal(event.latlng);
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

nameForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!pendingPin) return;
  const name = nameInput.value.trim();
  if (!name) {
    nameInput.focus();
    return;
  }
  const { lat, lng } = pendingPin;
  closeNameModal();
  placeBridge(lat, lng, name);
});

document.getElementById("name-cancel").addEventListener("click", () => {
  closeNameModal();
});

nameModal.addEventListener("click", (event) => {
  if (event.target === nameModal) closeNameModal();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !nameModal.hidden) {
    closeNameModal();
  }
});

modeDrawBtn.addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  ranTransects = false;
  overlay.clearLayers();
  setMode("draw");
});

modeXsBtn.addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  setMode("xs");
});

undoBtn.addEventListener("click", () => {
  drawnLatLngs.pop();
  redrawDraft();
});

clearBtn.addEventListener("click", () => {
  drawnLatLngs = [];
  ranTransects = false;
  overlay.clearLayers();
  redrawDraft();
});

finishBtn.addEventListener("click", finishCentreline);

clearXsBtn.addEventListener("click", () => {
  resetXsDrawing();
  if (mode === "xs") {
    setCoach("Left-click two points on the DEM to draw a cross-section.");
  }
});

runForm.addEventListener("click", (event) => {
  const btn = event.target.closest(".help");
  if (!btn) return;
  event.preventDefault();
  const pop = document.getElementById(btn.dataset.help);
  const open = btn.getAttribute("aria-expanded") === "true";
  closeHelp();
  if (!open && pop) {
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }
});

document.addEventListener("click", (event) => {
  if (!runForm.contains(event.target)) closeHelp();
  if (!searchForm.contains(event.target)) hideSearchResults();
});

runForm.addEventListener("input", (event) => {
  updateLayoutPreview();
  if (event.target && (event.target.name === "along" || event.target.name === "length" || event.target.name === "interval")) {
    scheduleDemPreview();
    refreshLegend();
  }
});

runForm.addEventListener("change", (event) => {
  if (event.target.name === "length") {
    scheduleDemPreview();
  }
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  hideSearchResults();
  if (query.length < 2) return;
  setStatus("Searching…");
  setBusy(true, "Searching…");
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
        map.flyTo([hit.lat, hit.lon], 15, { duration: 0.7 });
        hideSearchResults();
        searchInput.value = hit.label;
      });
      li.appendChild(btn);
      searchResults.appendChild(li);
    });
    searchResults.hidden = false;
  } catch (_err) {
    setStatus("Place search is unavailable.", "error");
  } finally {
    setBusy(false);
  }
});

runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  event.stopPropagation();
  const { lat, lon } = currentLatLon();
  if (Number.isNaN(lat) || Number.isNaN(lon) || !marker) {
    setStatus("Double-click the map to pin a bridge first.", "error");
    return;
  }
  const drawn = centerlinePayload();
  if (!drawn) {
    setStatus("Draw the river centreline. Its length is the analysis length.", "error");
    return;
  }
  if (!runReady || runBusy) {
    setStatus(runBlockReason || "Finish placing the bridge and river before running.", "error");
    return;
  }
  const body = new FormData(runForm);
  body.set("lat", String(lat));
  body.set("lon", String(lon));
  body.set("along", alongInput.value);
  body.set("centerline", JSON.stringify(drawn));
  const jobId = newJobId();
  body.set("job_id", jobId);
  runJobId = jobId;
  runAbort = new AbortController();
  setRunning(true);
  setStatus("Downloading the 500 m LiDAR clip and running screening… Click Stop to change parameters and run again.");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      body,
      signal: runAbort.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (data.cancelled || res.status === 409) {
      setStatus("Screening stopped. Change parameters and run again.");
      return;
    }
    if (!res.ok) {
      setStatus(data.error || "Screening failed.", "error");
      return;
    }
    setStatus("Screening complete.", "ok");
    setMode("params");
    drawRunGeometry(data);
    renderResults(data);
    if (marker) marker.setLatLng([data.lat, data.lon]);
  } catch (err) {
    if (err && err.name === "AbortError") {
      setStatus("Screening stopped. Change parameters and run again.");
      return;
    }
    setStatus("Could not reach the HydroBridge server.", "error");
  } finally {
    setRunning(false);
    runAbort = null;
    runJobId = null;
    updateLayoutPreview();
  }
});

runBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  if (runBusy) {
    event.preventDefault();
    setStatus("Screening is already running. Click Stop to cancel it.", "error");
    return;
  }
  if (!runReady) {
    event.preventDefault();
    setStatus(runBlockReason || "Finish placing the bridge and river before running.", "error");
  }
});

document.querySelector(".run-actions").addEventListener("click", (event) => {
  if (event.target === runBtn) return;
  if (runBusy || runReady) return;
  setStatus(runBlockReason || "Finish placing the bridge and river before running.", "error");
});

function adaptMapLayout() {
  document.body.classList.toggle("has-params", Boolean(runForm && !runForm.hidden));
  document.body.classList.toggle("has-xs", Boolean(xsViewer && !xsViewer.hidden));
  document.body.classList.toggle("has-results", Boolean(resultsEl && !resultsEl.hidden));
  requestAnimationFrame(() => {
    map.invalidateSize();
    if (!xsViewer.hidden && xsProfile) drawXsProfile(xsProfile);
  });
}

["coach", "run-form", "legend", "xs-viewer", "name-modal"].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  L.DomEvent.disableClickPropagation(el);
  L.DomEvent.disableScrollPropagation(el);
});
document.querySelectorAll(".chrome, .basemap-switch, .brand, .search, .rail").forEach((el) => {
  L.DomEvent.disableClickPropagation(el);
  L.DomEvent.disableScrollPropagation(el);
});

stopBtn.addEventListener("click", async () => {
  const jobId = runJobId;
  const abort = runAbort;
  if (!jobId && !abort) return;
  stopBtn.disabled = true;
  setStatus("Stopping screening…");
  if (jobId) {
    try {
      await fetch("/api/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId }),
      });
    } catch (_err) {
      /* still abort the in-flight run request */
    }
  }
  if (abort) abort.abort();
});

if (demOpacityInput) {
  demOpacityInput.addEventListener("input", () => {
    demTransparency = Math.max(0, Math.min(100, Number(demOpacityInput.value) || 0));
    try {
      localStorage.setItem("hydroscreen-dem-transparency", String(Math.round(demTransparency)));
    } catch (_err) {
      /* ignore */
    }
    applyDemTransparency();
  });
}
syncDemOpacityControl();

window.addEventListener("resize", () => {
  adaptMapLayout();
});

const mapStage = document.querySelector(".map-stage");
if (mapStage && typeof ResizeObserver === "function") {
  new ResizeObserver(() => map.invalidateSize()).observe(mapStage);
}

setMode("idle");
syncChrome();

