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
const wcsWrap = document.getElementById("wcs-wrap");
const modePinBtn = document.getElementById("mode-pin");
const modeDrawBtn = document.getElementById("mode-draw");
const undoBtn = document.getElementById("undo-vertex");
const clearBtn = document.getElementById("clear-line");
const snapBtn = document.getElementById("snap-pin");
const layoutPreview = document.getElementById("layout-preview");

const map = L.map("map").setView([WELLINGTON.lat, WELLINGTON.lon], WELLINGTON.zoom);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

const overlay = L.layerGroup().addTo(map);
const draftLine = L.polyline([], { color: "#1f6f8b", weight: 5, opacity: 0.95 }).addTo(map);
const vertexLayer = L.layerGroup().addTo(map);
let marker = L.marker([WELLINGTON.lat, WELLINGTON.lon], { draggable: true }).addTo(map);
marker.bindPopup("Bridge site").openPopup();

let mode = "pin";
let drawnLatLngs = [];
let drawingStroke = false;

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

function setMode(next) {
  mode = next;
  document.body.classList.toggle("mode-draw", mode === "draw");
  modePinBtn.setAttribute("aria-pressed", String(mode === "pin"));
  modeDrawBtn.setAttribute("aria-pressed", String(mode === "draw"));
  if (mode === "draw") {
    map.dragging.disable();
    map.doubleClickZoom.disable();
    mapHint.textContent = "Click or drag along the river. Double-click when the line is done.";
  } else {
    map.dragging.enable();
    map.doubleClickZoom.enable();
    drawingStroke = false;
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
    resultsNote.textContent = `${resultsNote.textContent} ${extra}${flow}`;
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

undoBtn.addEventListener("click", () => {
  drawnLatLngs.pop();
  redrawDraft();
});

clearBtn.addEventListener("click", () => {
  drawnLatLngs = [];
  redrawDraft();
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
  const sample = runForm.querySelector('input[name="dem_source"][value="sample"]');
  if (sample) sample.checked = true;
  demFileWrap.hidden = true;
  wcsWrap.hidden = true;
});

runForm.addEventListener("input", updateLayoutPreview);
runForm.addEventListener("change", (event) => {
  if (event.target.name !== "dem_source") return;
  demFileWrap.hidden = event.target.value !== "upload";
  wcsWrap.hidden = event.target.value !== "linz";
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

refreshPlaceName(WELLINGTON.lat, WELLINGTON.lon);
refreshLineLabel();
updateLayoutPreview();
