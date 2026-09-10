const WELLINGTON = { lat: -41.2865, lon: 174.7762, zoom: 14 };
const placeLabel = document.getElementById("place-label");
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

const map = L.map("map").setView([WELLINGTON.lat, WELLINGTON.lon], WELLINGTON.zoom);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

const overlay = L.layerGroup().addTo(map);
let marker = L.marker([WELLINGTON.lat, WELLINGTON.lon], { draggable: true }).addTo(map);
marker.bindPopup("Bridge site").openPopup();

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

function drawRunGeometry(payload) {
  overlay.clearLayers();
  if (payload.centerline && payload.centerline.length) {
    const line = payload.centerline.map(([lon, lat]) => [lat, lon]);
    L.polyline(line, { color: "#1f6f8b", weight: 4, opacity: 0.9 }).addTo(overlay);
  }
  (payload.transects || []).forEach((tran) => {
    const line = (tran.coords || []).map(([lon, lat]) => [lat, lon]);
    L.polyline(line, { color: "#c45c26", weight: 2, opacity: 0.85 }).addTo(overlay);
  });
}

function renderResults(payload) {
  resultsEl.hidden = false;
  resultsBody.innerHTML = "";
  plotsEl.innerHTML = "";
  if (payload.used_synthetic_centerline) {
    resultsNote.textContent = "No OpenStreetMap waterway was found within 500 m, so a short east–west centreline through the pin was used.";
  } else {
    resultsNote.textContent = "Transects are drawn perpendicular to the nearby waterway, centred on the selected bridge pin.";
  }
  summaryLink.hidden = !payload.summary_xlsx;
  summaryLink.href = payload.summary_xlsx || "#";

  (payload.summary || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.transect}</td>
      <td>${fmt(row.offset_m, 1)}</td>
      <td>${fmt(row.width_m, 1)}</td>
      <td>${fmt(row.area_m2, 1)}</td>
      <td>${fmt(row.depth_mean_m, 2)}</td>
      <td>${fmt(row.velocity_m_s, 2)}</td>
      <td>${fmt(row.discharge_m3_s, 1)}</td>`;
    resultsBody.appendChild(tr);

    const fig = document.createElement("figure");
    fig.innerHTML = `
      <img src="${row.plot}" alt="Cross-section for transect ${row.transect}">
      <figcaption>Transect ${row.transect} · offset ${fmt(row.offset_m, 0)} m
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
  setLocation(event.latlng.lat, event.latlng.lng);
});

marker.on("dragend", () => {
  const pos = marker.getLatLng();
  setLocation(pos.lat, pos.lng);
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
