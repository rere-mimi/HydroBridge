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
const xsTitle = document.getElementById("xs-title");
const xsCanvas = document.getElementById("xs-canvas");
const xsMeasureEl = document.getElementById("xs-measure");
const busyEl = document.getElementById("busy");
const busyText = document.getElementById("busy-text");
const busyPct = document.getElementById("busy-pct");
const busyBar = document.getElementById("busy-bar");
const busyBarFill = document.getElementById("busy-bar-fill");
const legendEl = document.getElementById("legend");
const demOpacityControl = document.getElementById("dem-opacity-control");
const demOpacityInput = document.getElementById("dem-opacity");
const demOpacityValue = document.getElementById("dem-opacity-value");
const arisJson = document.getElementById("aris-json");
const flowPrimary = document.getElementById("flow-primary");

const ARI_YEARS = [10, 25, 50, 100, 1000];
const ARI_COLORS = {
  10: "#0284c7",
  25: "#059669",
  50: "#d97706",
  100: "#7c3aed",
  1000: "#be123c",
};
const ARI_DEFAULT_Q = { 10: 5, 25: 7, 50: 8.5, 100: 10, 1000: 22 };

function ariLabel(years) {
  const n = Number(years);
  return n >= 1000 ? "1,000-year ARI" : `${n}-year ARI`;
}

function selectedAris() {
  if (!runForm) return [];
  return ARI_YEARS.filter((year) => {
    const box = runForm.querySelector(`input[data-ari="${year}"]`);
    return box && box.checked;
  }).map((year) => {
    const input = runForm.querySelector(`[name="flow_${year}"]`);
    return {
      years: year,
      key: `${year}y`,
      label: ariLabel(year),
      flow_m3_s: Number(input && input.value),
      color: ARI_COLORS[year],
    };
  });
}

function syncAriInputs() {
  if (!runForm) return selectedAris();
  ARI_YEARS.forEach((year) => {
    const box = runForm.querySelector(`input[data-ari="${year}"]`);
    const input = runForm.querySelector(`[name="flow_${year}"]`);
    if (!box || !input) return;
    input.disabled = !box.checked;
    if (box.checked && (input.value === "" || !Number.isFinite(Number(input.value)))) {
      input.value = String(ARI_DEFAULT_Q[year]);
    }
  });
  const selected = selectedAris();
  if (arisJson) {
    arisJson.value = JSON.stringify(
      selected.map((item) => ({ years: item.years, flow_m3_s: item.flow_m3_s }))
    );
  }
  if (flowPrimary) {
    flowPrimary.value = selected.length ? String(selected[0].flow_m3_s) : "10";
  }
  return selected;
}

function computedScenarios(payload) {
  const src = payload || lastRun;
  return (src && src.layout && Array.isArray(src.layout.flow_scenarios) && src.layout.flow_scenarios) || [];
}

function visibleScenarios(payload) {
  const computed = computedScenarios(payload);
  const selected = selectedAris();
  if (!computed.length) return selected;
  const byYear = new Map(computed.map((item) => [Number(item.years), item]));
  return selected.map((item) => byYear.get(item.years)).filter(Boolean);
}

function formatSlope(absSlope) {
  const value = Number(absSlope);
  if (!Number.isFinite(value) || value <= 0) return "—";
  const ratio = value >= 1e-9 ? Math.round(1 / value) : null;
  const sci = value.toExponential(2);
  return ratio ? `${sci} (1 in ${ratio})` : sci;
}

function hydFor(record, key) {
  if (record && record.aris && key && record.aris[key]) return record.aris[key];
  return record || {};
}

function refreshAriPlot() {
  syncAriInputs();
  if (!lastRun || !selectedSection) return;
  renderResults(lastRun, { scroll: false });
  showSelectedProfile();
}

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
var lastRun = null;
var selectedSection = null;
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
      label: `Transects — ${length} m wide, every ${spacing} m · click one to inspect`,
    });
    items.push({
      swatch: "river",
      label: "Click the centreline for the long section",
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
  const aris = syncAriInputs();
  if (!aris.length) {
    layoutPreview.textContent = "Select at least one return period (ARI).";
    runReady = false;
    runBlockReason = "Select at least one return period and enter its flow.";
    setRunAvailability();
    return;
  }
  if (aris.some((item) => !Number.isFinite(item.flow_m3_s) || item.flow_m3_s < 0)) {
    layoutPreview.textContent = "Enter a flow rate ≥ 0 for each selected ARI.";
    runReady = false;
    runBlockReason = "Enter a flow rate for each selected return period.";
    setRunAvailability();
    return;
  }
  const ariNote = aris.map((item) => item.label.replace(" ARI", "")).join(", ");
  let extra = "";
  if (lastRun) {
    const computed = new Set(computedScenarios(lastRun).map((item) => Number(item.years)));
    const missing = aris.filter((item) => !computed.has(item.years));
    if (missing.length) {
      extra = ` · run screening to add ${missing.map((item) => item.label).join(", ")}`;
    }
  }
  layoutPreview.textContent = `${nTransects} transects along the drawn ${along.toFixed(0)} m · ${nSamples} DEM points each · ${ariNote}${extra}`;
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
  draftLine.setStyle({ opacity: drawnLatLngs.length ? 0.95 : 0 });
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
  lastRun = null;
  selectedSection = null;
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

function formatXsDistance(metres) {
  const value = Number(metres);
  if (!Number.isFinite(value) || value < 0) return "0.0 m";
  if (value < 100) return `${value.toFixed(1)} m`;
  return `${Math.round(value)} m`;
}

function pointerFromMapEvent(event) {
  const src = event && (event.originalEvent || event);
  if (!src || !Number.isFinite(src.clientX) || !Number.isFinite(src.clientY)) return null;
  return { x: src.clientX, y: src.clientY };
}

function hideXsMeasure() {
  if (!xsMeasureEl) return;
  xsMeasureEl.hidden = true;
}

function updateXsMeasure(latlng, pointer) {
  if (!xsMeasureEl) return;
  if (mode !== "xs" || xsPoints.length !== 1 || !latlng) {
    hideXsMeasure();
    return;
  }
  xsMeasureEl.textContent = formatXsDistance(map.distance(xsPoints[0], latlng));
  xsMeasureEl.hidden = false;
  if (pointer) {
    xsMeasureEl.style.transform = `translate(${pointer.x + 14}px, ${pointer.y + 12}px)`;
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

function strokePolyline(ctx, pts) {
  let drawing = false;
  pts.forEach((pt) => {
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
}

function drawXsProfile(profile, options = {}) {
  if (!xsCanvas || !profile) return;
  const dists = profile.distance_m || [];
  const elevs = profile.elevation_m || [];
  const waterLevels = [];
  if (Array.isArray(options.waterLevels)) {
    options.waterLevels.forEach((item) => {
      if (item && item.value != null && Number.isFinite(Number(item.value))) {
        waterLevels.push(item);
      }
    });
  } else if (options.waterLevel != null && Number.isFinite(Number(options.waterLevel))) {
    waterLevels.push({
      value: Number(options.waterLevel),
      color: "#1f6f8b",
      label: "Water level",
      velocity: options.velocity,
    });
  }
  const series = Array.isArray(options.series) ? options.series.filter(Boolean) : [];
  const slope = options.slope && Number.isFinite(Number(options.slope.abs)) ? options.slope : null;
  const seriesHasVelocity = series.some((item) =>
    (item.points || []).some((pt) => pt.velocity_m_s != null && Number.isFinite(Number(pt.velocity_m_s)))
  );
  const hasVelocity = seriesHasVelocity || waterLevels.some((item) => item.velocity != null && Number.isFinite(Number(item.velocity)));

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

  const legendCount = 1 + (slope ? 1 : 0) + Math.max(waterLevels.length, series.length);
  const pad = {
    left: 52,
    right: hasVelocity ? 54 : 18,
    top: Math.max(16, 10 + Math.min(legendCount, 6) * 13),
    bottom: 32,
  };
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
  waterLevels.forEach((item) => {
    zmin = Math.min(zmin, Number(item.value));
    zmax = Math.max(zmax, Number(item.value));
  });
  series.forEach((item) => {
    (item.points || []).forEach((pt) => {
      if (pt.water_level_m != null && Number.isFinite(Number(pt.water_level_m))) {
        zmin = Math.min(zmin, Number(pt.water_level_m));
        zmax = Math.max(zmax, Number(pt.water_level_m));
      }
    });
  });
  if (slope && Number.isFinite(Number(slope.signed)) && Number.isFinite(Number(slope.intercept))) {
    const z0 = Number(slope.intercept);
    const z1 = Number(slope.intercept) + Number(slope.signed) * xmax;
    zmin = Math.min(zmin, z0, z1);
    zmax = Math.max(zmax, z0, z1);
  }
  if (zmax <= zmin) zmax = zmin + 1;
  const zPad = (zmax - zmin) * 0.08;
  zmin -= zPad;
  zmax += zPad;

  let vmin = 0;
  let vmax = 0;
  series.forEach((item) => {
    (item.points || []).forEach((pt) => {
      if (pt.velocity_m_s != null && Number.isFinite(Number(pt.velocity_m_s))) {
        vmax = Math.max(vmax, Number(pt.velocity_m_s));
      }
    });
  });
  waterLevels.forEach((item) => {
    if (item.velocity != null && Number.isFinite(Number(item.velocity))) {
      vmax = Math.max(vmax, Number(item.velocity));
    }
  });
  if (vmax <= vmin) vmax = 1;

  const xOf = (d) => pad.left + (Number(d) / Math.max(xmax, 1e-6)) * plotW;
  const yOf = (z) => pad.top + (1 - (Number(z) - zmin) / (zmax - zmin)) * plotH;
  const vOf = (v) => pad.top + (1 - (Number(v) - vmin) / Math.max(vmax - vmin, 1e-6)) * plotH;

  ctx.strokeStyle = "#e2e8f0";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  if (hasVelocity) {
    ctx.moveTo(pad.left + plotW, pad.top);
    ctx.lineTo(pad.left + plotW, pad.top + plotH);
  }
  ctx.stroke();

  ctx.fillStyle = "#64748b";
  ctx.font = "11px Segoe UI, system-ui, sans-serif";
  ctx.fillText(options.xLabel || "Distance (m)", pad.left + plotW / 2 - 36, cssH - 8);
  ctx.save();
  ctx.translate(14, pad.top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Elevation (m)", -40, 0);
  ctx.restore();
  if (hasVelocity) {
    ctx.save();
    ctx.translate(cssW - 12, pad.top + plotH / 2);
    ctx.rotate(Math.PI / 2);
    ctx.fillText("Velocity (m/s)", -42, 0);
    ctx.restore();
    ctx.fillText(vmin.toFixed(1), pad.left + plotW + 6, vOf(vmin) + 3);
    ctx.fillText(vmax.toFixed(1), pad.left + plotW + 6, vOf(vmax) + 3);
  }
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
  strokePolyline(ctx, points);
  const first = points.find((pt) => pt);
  const last = [...points].reverse().find((pt) => pt);
  if (first && last) {
    ctx.lineTo(last[0], pad.top + plotH);
    ctx.lineTo(first[0], pad.top + plotH);
    ctx.closePath();
    ctx.fillStyle = "rgba(148, 163, 184, 0.28)";
    ctx.fill();
  }

  ctx.beginPath();
  strokePolyline(ctx, points);
  ctx.strokeStyle = "#0f172a";
  ctx.lineWidth = 2;
  ctx.stroke();

  if (slope && Number.isFinite(Number(slope.signed)) && Number.isFinite(Number(slope.intercept))) {
    const z0 = Number(slope.intercept);
    const z1 = Number(slope.intercept) + Number(slope.signed) * xmax;
    ctx.beginPath();
    ctx.moveTo(xOf(0), yOf(z0));
    ctx.lineTo(xOf(xmax), yOf(z1));
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "#92400e";
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.setLineDash([]);
  }

  if (waterLevels.length === 1) {
    const level = Number(waterLevels[0].value);
    ctx.beginPath();
    let drawing = false;
    let started = false;
    dists.forEach((dist, i) => {
      const z = elevs[i];
      if (z == null || !Number.isFinite(Number(z)) || Number(z) >= level) {
        drawing = false;
        return;
      }
      const x = xOf(dist);
      const yGround = yOf(Number(z));
      const yWater = yOf(level);
      if (!drawing) {
        if (!started) ctx.moveTo(x, yGround);
        else ctx.lineTo(x, yGround);
        ctx.lineTo(x, yWater);
        drawing = true;
        started = true;
      } else {
        ctx.lineTo(x, yWater);
      }
    });
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = waterLevels[0].color || "#0284c7";
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  waterLevels.forEach((item) => {
    const y = yOf(Number(item.value));
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = item.color || "#1f6f8b";
    ctx.lineWidth = 1.6;
    ctx.stroke();
    ctx.setLineDash([]);
  });

  series.forEach((item) => {
    const wlPts = (item.points || [])
      .filter((pt) => pt.water_level_m != null && Number.isFinite(Number(pt.water_level_m)))
      .map((pt) => [xOf(pt.distance_m), yOf(pt.water_level_m)]);
    if (wlPts.length) {
      ctx.beginPath();
      strokePolyline(ctx, wlPts);
      ctx.strokeStyle = item.color || "#0284c7";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    if (hasVelocity) {
      const vPts = (item.points || [])
        .filter((pt) => pt.velocity_m_s != null && Number.isFinite(Number(pt.velocity_m_s)))
        .map((pt) => [xOf(pt.distance_m), vOf(pt.velocity_m_s)]);
      if (vPts.length) {
        ctx.beginPath();
        strokePolyline(ctx, vPts);
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = item.color || "#0284c7";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
  });

  const legend = [];
  legend.push({ color: "#0f172a", text: "Ground", dash: [] });
  if (slope) legend.push({ color: "#92400e", text: `Slope S = ${formatSlope(slope.abs)}`, dash: [4, 4] });
  waterLevels.forEach((item) => {
    const bits = [item.label || "Water level", `WL ${Number(item.value).toFixed(2)} m`];
    if (item.velocity != null && Number.isFinite(Number(item.velocity))) {
      bits.push(`V ${Number(item.velocity).toFixed(2)} m/s`);
    }
    if (item.flow != null && Number.isFinite(Number(item.flow))) {
      bits.push(`Q ${Number(item.flow).toFixed(1)} m³/s`);
    }
    legend.push({ color: item.color || "#1f6f8b", text: bits.join(" · "), dash: [6, 4] });
  });
  series.forEach((item) => {
    if (waterLevels.length) return;
    const bits = [item.label || "ARI"];
    if (item.flow != null && Number.isFinite(Number(item.flow))) bits.push(`Q ${Number(item.flow).toFixed(1)} m³/s`);
    bits.push("WL solid");
    bits.push("V dotted");
    legend.push({ color: item.color || "#0284c7", text: bits.join(" · "), dash: [] });
  });

  legend.forEach((item, index) => {
    const col = index > 2 ? 1 : 0;
    const row = col ? index - 3 : index;
    const x = pad.left + col * Math.max(220, plotW / 2);
    const y = 10 + row * 12;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + 16, y);
    ctx.strokeStyle = item.color;
    ctx.setLineDash(item.dash || []);
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#334155";
    ctx.font = "10px Segoe UI, system-ui, sans-serif";
    ctx.fillText(item.text, x + 20, y + 3);
  });

  (options.stations || []).forEach((station) => {
    const dist = Number(station.distance_m);
    if (!Number.isFinite(dist)) return;
    const x = xOf(Math.max(0, Math.min(xmax, dist)));
    let z = null;
    if (dists.length) {
      let best = 0;
      let bestAbs = Infinity;
      dists.forEach((d, i) => {
        const gap = Math.abs(Number(d) - dist);
        if (gap < bestAbs && elevs[i] != null && Number.isFinite(Number(elevs[i]))) {
          bestAbs = gap;
          best = i;
        }
      });
      z = elevs[best];
    }
    const y = z != null && Number.isFinite(Number(z)) ? yOf(Number(z)) : pad.top + plotH;
    ctx.beginPath();
    ctx.arc(x, y, station.selected ? 5.5 : 4, 0, Math.PI * 2);
    ctx.fillStyle = station.selected ? "#e11d48" : "#ea580c";
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5;
    ctx.fill();
    ctx.stroke();
    if (station.label) {
      ctx.fillStyle = "#0f172a";
      ctx.font = "10px Segoe UI, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(String(station.label), x, y - 8);
      ctx.textAlign = "start";
    }
  });
}
function showXsViewer() {
  xsViewer.hidden = false;
  adaptMapLayout();
  requestAnimationFrame(() => {
    if (xsProfile) drawXsProfile(xsProfile, xsProfile.options || {});
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
  hideXsMeasure();
  refreshLegend();
  if (keepViewer) {
    clearXsChart();
    if (xsTitle) xsTitle.textContent = "DEM cross-section";
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
    if (xsTitle) xsTitle.textContent = "DEM cross-section";
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
    hideXsMeasure();
    setXsMeta("Click the second point. The profile will regenerate.");
  }
  if (xsPoints.length === 1 && map.distance(xsPoints[0], latlng) < 5) {
    return;
  }
  xsPoints.push(L.latLng(latlng.lat, latlng.lng));
  drawXsLine();
  if (xsPoints.length === 1) {
    setCoach("Click the second point on the DEM. Distance follows the cursor.");
    setStatus("First cross-section point placed. Move to see the distance, then click the second point.", "ok");
    showXsViewer();
    if (!xsProfile) {
      clearXsChart();
      setXsMeta("Click the second point. The profile will appear here.");
    }
    return;
  }
  hideXsMeasure();
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
  if (mode !== "xs" || xsPoints.length !== 1) hideXsMeasure();
  if (mode === "draw") {
    map.dragging.disable();
    setCoach("Click or drag along the river through the bridge. Double-click the last point when the line is done.");
  } else if (mode === "xs") {
    map.dragging.disable();
    setCoach(
      xsPoints.length === 1
        ? "Click the second point on the DEM. Distance follows the cursor."
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
  lastRun = null;
  selectedSection = null;
  setMode("draw");
  scheduleDemPreview();
}

function stopOverlayClick(event) {
  L.DomEvent.stop(event);
  if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
}

function selectSection(next, { scroll = false } = {}) {
  selectedSection = next;
  if (!lastRun) return;
  drawRunGeometry(lastRun);
  renderResults(lastRun, { scroll });
  showSelectedProfile();
}

function showSelectedProfile() {
  if (!lastRun || !selectedSection) return;
  const scenarios = visibleScenarios(lastRun);
  const slopeAbs = Number((lastRun.centerline_profile && lastRun.centerline_profile.slope) || (lastRun.layout && lastRun.layout.slope));
  const slope = Number.isFinite(slopeAbs)
    ? {
        abs: slopeAbs,
        signed: lastRun.centerline_profile && lastRun.centerline_profile.fit_slope,
        intercept: lastRun.centerline_profile && lastRun.centerline_profile.fit_intercept,
      }
    : null;
  if (selectedSection.type === "transect") {
    const feat = (lastRun.transects || []).find((item) => item.transect === selectedSection.id);
    const row = (lastRun.summary || []).find((item) => item.transect === selectedSection.id);
    if (!feat || !feat.distance_m) return;
    const waterLevels = scenarios.map((scenario) => {
      const hyd = hydFor(feat, scenario.key) && hydFor(feat, scenario.key).water_level_m != null
        ? hydFor(feat, scenario.key)
        : hydFor(row, scenario.key);
      return {
        value: hyd.water_level_m,
        velocity: hyd.velocity_m_s,
        color: scenario.color,
        label: scenario.label,
        flow: scenario.flow_m3_s,
      };
    }).filter((item) => item.value != null && Number.isFinite(Number(item.value)));
    xsProfile = {
      distance_m: feat.distance_m,
      elevation_m: feat.elevation_m,
      options: { waterLevels },
    };
    if (xsTitle) xsTitle.textContent = `Transect ${feat.transect}`;
    const ariNote = waterLevels.length
      ? waterLevels.map((item) => `${item.label} WL ${fmt(item.value, 2)} m, V ${fmt(item.velocity, 2)} m/s`).join(" · ")
      : "no ARI selected";
    setXsMeta(
      `Offset ${fmt(feat.offset_m, 0)} m along the centreline · ${feat.n_samples || feat.distance_m.length} samples · ${ariNote}`
    );
    showXsViewer();
    requestAnimationFrame(() => drawXsProfile(xsProfile, xsProfile.options));
    return;
  }
  if (selectedSection.type === "centerline") {
    const profile = lastRun.centerline_profile;
    if (!profile || !profile.distance_m) return;
    const origin = Number(profile.origin_m) || 0;
    const stations = (lastRun.transects || []).map((tran) => ({
      distance_m: tran.station_m != null ? Number(tran.station_m) : origin + Number(tran.offset_m || 0),
      label: String(tran.transect),
      selected: false,
    }));
    const series = scenarios.map((scenario) => ({
      color: scenario.color,
      label: scenario.label,
      flow: scenario.flow_m3_s,
      points: (lastRun.transects || []).map((tran) => {
        const hyd = hydFor(tran, scenario.key);
        const station = tran.station_m != null ? Number(tran.station_m) : origin + Number(tran.offset_m || 0);
        return {
          distance_m: station,
          water_level_m: hyd.water_level_m,
          velocity_m_s: hyd.velocity_m_s,
        };
      }).sort((a, b) => a.distance_m - b.distance_m),
    }));
    xsProfile = {
      distance_m: profile.distance_m,
      elevation_m: profile.elevation_m,
      options: {
        stations,
        series,
        slope,
        xLabel: "Distance along centreline (m)",
      },
    };
    if (xsTitle) xsTitle.textContent = "Centreline profile";
    const ariNote = scenarios.length
      ? scenarios.map((item) => item.label).join(", ")
      : "bed only";
    setXsMeta(
      `${stations.length} transect stations along the ${fmt(profile.length_m || profile.distance_m[profile.distance_m.length - 1], 0)} m river line · slope S = ${formatSlope(slopeAbs)} · ${ariNote}`
    );
    showXsViewer();
    requestAnimationFrame(() => drawXsProfile(xsProfile, xsProfile.options));
  }
}

function drawRunGeometry(payload) {
  overlay.clearLayers();
  draftLine.setStyle({ opacity: 0 });
  vertexLayer.clearLayers();
  const selectedId = selectedSection && selectedSection.type === "transect" ? selectedSection.id : null;
  const centerlineSelected = selectedSection && selectedSection.type === "centerline";
  const cl = payload.centerline || [];
  if (cl.length >= 2) {
    const river = L.polyline(
      cl.map(([lon, lat]) => [lat, lon]),
      {
        color: "#0369a1",
        weight: centerlineSelected ? 7 : 5,
        opacity: 0.95,
        interactive: true,
      }
    ).addTo(overlay);
    river.on("click", (event) => {
      stopOverlayClick(event);
      selectSection({ type: "centerline" });
    });
    river.bindTooltip("Centreline — click for the long section", { sticky: true });
  }
  (payload.transects || []).forEach((tran) => {
    const selected = selectedId === tran.transect;
    const line = (tran.coords || []).map(([lon, lat]) => [lat, lon]);
    const poly = L.polyline(line, {
      color: selected ? "#be123c" : "#ea580c",
      weight: selected ? 5 : 3,
      opacity: selected || !selectedId ? 0.95 : 0.45,
      interactive: true,
    }).addTo(overlay);
    const pick = (event) => {
      stopOverlayClick(event);
      selectSection({ type: "transect", id: tran.transect });
    };
    poly.on("click", pick);
    poly.bindTooltip(`Transect ${tran.transect}`, { sticky: true });
    (tran.samples || []).forEach(([lon, lat]) => {
      L.circleMarker([lat, lon], {
        radius: selected ? 5 : 3,
        color: selected ? "#be123c" : "#ea580c",
        fillColor: "#fff",
        fillOpacity: 1,
        weight: 1.5,
        interactive: true,
      })
        .on("click", pick)
        .addTo(overlay);
    });
  });
  ranTransects = true;
  refreshLegend();
}

function renderResults(payload, { scroll = true } = {}) {
  resultsEl.hidden = false;
  adaptMapLayout();
  resultsBody.innerHTML = "";
  plotsEl.innerHTML = "";
  const lengthNote = payload.layout?.along_m != null
    ? `Transects cover the ${Number(payload.layout.along_m).toFixed(0)} m centreline you drew.`
    : "Transects follow the river centreline you drew.";
  resultsNote.textContent = `${bridgeName ? bridgeName + " · " : ""}${lengthNote} Click a transect on the map to inspect that section, or the blue centreline to see stations along the river.`;
  if (payload.layout) {
    const extra = `${payload.layout.n_transects} transects, sampled every ${payload.layout.sample_spacing_m} m.`;
    const scenarios = visibleScenarios(payload);
    const flow = scenarios.length
      ? ` ${scenarios.map((item) => `${item.label} Q=${Number(item.flow_m3_s).toFixed(1)} m³/s`).join("; ")}, n=${payload.layout.mannings_n}, centreline slope S=${formatSlope(payload.layout.slope)}.`
      : payload.layout.flow_m3_s != null
        ? ` Target Q ${payload.layout.flow_m3_s} m³/s, n=${payload.layout.mannings_n}, slope from centreline S=${formatSlope(payload.layout.slope)}.`
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

  const selectedId = selectedSection && selectedSection.type === "transect" ? selectedSection.id : null;
  const showCenterline = selectedSection && selectedSection.type === "centerline";
  const tableScenario = visibleScenarios(payload)[0];
  const tableKey = tableScenario && tableScenario.key;
  (payload.summary || []).forEach((row) => {
    const hyd = hydFor(row, tableKey);
    const status = hyd.overtopped ? "Overtops banks" : (hyd.conveys ? "OK" : "Cannot convey");
    const tr = document.createElement("tr");
    if (selectedId === row.transect) tr.classList.add("is-selected");
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.innerHTML = `
      <td>${row.transect}</td>
      <td>${fmt(row.offset_m, 1)}</td>
      <td>${row.n_samples ?? "—"}</td>
      <td>${fmt(hyd.water_level_m, 2)}</td>
      <td>${fmt(hyd.max_depth_m, 2)}</td>
      <td>${fmt(hyd.width_m, 1)}</td>
      <td>${fmt(hyd.area_m2, 1)}</td>
      <td>${fmt(hyd.hydraulic_radius_m, 2)}</td>
      <td>${fmt(hyd.velocity_m_s, 2)}</td>
      <td>${fmt(hyd.discharge_m3_s, 2)}</td>
      <td>${status}</td>`;
    tr.addEventListener("click", () => selectSection({ type: "transect", id: row.transect }, { scroll: false }));
    resultsBody.appendChild(tr);

    if (showCenterline) return;
    if (selectedId != null && selectedId !== row.transect) return;
    const fig = document.createElement("figure");
    fig.innerHTML = `
      <img src="${row.plot}" alt="Cross-section for transect ${row.transect}">
      <figcaption>Transect ${row.transect} · offset ${fmt(row.offset_m, 0)} m
        · water level ${fmt(hyd.water_level_m, 2)} m
        · <a href="${row.csv}">CSV</a></figcaption>`;
    plotsEl.appendChild(fig);
  });
  if (scroll) resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
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
  if (mode === "xs" && xsPoints.length === 1) {
    updateXsMeasure(event.latlng, pointerFromMapEvent(event));
  }
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
    updateXsMeasure(event.latlng, pointerFromMapEvent(event));
    return;
  }
  hideXsMeasure();
  if (mode !== "draw" || !drawingStroke) return;
  addVertex(event.latlng);
});

map.on("mouseup", () => {
  drawingStroke = false;
});

map.getContainer().addEventListener("mouseleave", () => {
  drawingStroke = false;
  hideXsMeasure();
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
  if (event.target && event.target.matches("[data-ari]")) {
    refreshAriPlot();
  }
});

runForm.addEventListener("change", (event) => {
  if (event.target.name === "length") {
    scheduleDemPreview();
  }
  if (event.target && event.target.matches("[data-ari], [name^='flow_']")) {
    syncAriInputs();
    if (event.target.matches("[data-ari]")) refreshAriPlot();
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
  syncAriInputs();
  body.set("lat", String(lat));
  body.set("lon", String(lon));
  body.set("along", alongInput.value);
  body.set("centerline", JSON.stringify(drawn));
  body.set("aris", arisJson ? arisJson.value : JSON.stringify(selectedAris().map((item) => ({
    years: item.years,
    flow_m3_s: item.flow_m3_s,
  }))));
  if (flowPrimary) body.set("flow", flowPrimary.value);
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
    setStatus("Screening complete. Click a transect to inspect that section, or the centreline for the long profile.", "ok");
    setMode("params");
    lastRun = data;
    selectedSection = { type: "centerline" };
    drawRunGeometry(data);
    renderResults(data);
    showSelectedProfile();
    setCoach("Click an orange transect to see only that cross-section. Click the blue centreline to see transect stations along the river.");
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
    if (!xsViewer.hidden && xsProfile) drawXsProfile(xsProfile, xsProfile.options || {});
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
syncAriInputs();

window.addEventListener("resize", () => {
  adaptMapLayout();
});

const mapStage = document.querySelector(".map-stage");
if (mapStage && typeof ResizeObserver === "function") {
  new ResizeObserver(() => map.invalidateSize()).observe(mapStage);
}

setMode("idle");
syncChrome();

