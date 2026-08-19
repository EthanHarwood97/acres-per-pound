"use strict";

let DATA = null;
let view = "all";
let sortKey = "gbp_per_acre";
let sortAsc = true;
let favs = new Set();

const FAV_KEY = "apf_favs";
const CORR_KEY = "apf_corrections";
const VIS_KEY = "apf_visited";

let corrections = {};
function loadCorrections() {
  try { corrections = JSON.parse(localStorage.getItem(CORR_KEY) || "{}"); }
  catch (e) { corrections = {}; }
}
function saveCorrections() {
  try { localStorage.setItem(CORR_KEY, JSON.stringify(corrections)); } catch (e) {}
}

let visited = new Set();
function loadVisited() {
  try { visited = new Set(JSON.parse(localStorage.getItem(VIS_KEY) || "[]")); }
  catch (e) { visited = new Set(); }
}
function saveVisited() {
  if (visited.size > 5000) {
    const keep = [...visited].slice(-5000);
    visited = new Set(keep);
  }
  try { localStorage.setItem(VIS_KEY, JSON.stringify([...visited])); } catch (e) {}
}

// UK Regional & Geographic Constants
const SCOTLAND_REGIONS = new Set([
  "Aberdeenshire", "Angus", "Argyll and Bute", "Clackmannanshire",
  "Dumfries and Galloway", "East Ayrshire", "East Dunbartonshire",
  "East Lothian", "East Renfrewshire", "Falkirk (County)", "Fife",
  "Glasgow", "Highland, Scotland", "Inverclyde", "Midlothian", "Moray",
  "North Ayrshire", "North Lanarkshire", "Orkney, Orkney Islands", "Perth and Kinross",
  "Renfrewshire", "Scottish Borders", "South Ayrshire", "South Lanarkshire",
  "Stirling (County)", "West Dunbartonshire", "West Lothian", "Shetland", "Western Isles"
]);

const HIGHLANDS_REGIONS = new Set([
  "Highland, Scotland", "Orkney, Orkney Islands", "Moray", "Aberdeenshire",
  "Argyll and Bute", "Western Isles", "Shetland", "Angus", "Perth and Kinross"
]);

const WALES_REGIONS = new Set([
  "Bangor, Gwynedd", "Blaenau Gwent", "Bridgend (County of)", "Caerphilly (County of)",
  "Cardiff (County of)", "Carmarthenshire, Mid Wales", "Ceredigion, Mid Wales",
  "Conwy (County of)", "Denbighshire", "Flintshire", "Gwynedd", "Isle Of Anglesey",
  "Merthyr Tydfil (County of)", "Monmouthshire", "Neath Port Talbot",
  "Newport (County of)", "Pembrokeshire, South West Wales", "Powys",
  "Rhondda Cynon Taff", "Swansea (County of)", "Torfaen", "Vale Of Glamorgan",
  "Wrexham (County of)"
]);

function getRegionCountry(regName) {
  if (!regName) return "England";
  if (SCOTLAND_REGIONS.has(regName) || regName.toLowerCase().includes("scotland") || regName.toLowerCase().includes("scottish")) return "Scotland";
  if (WALES_REGIONS.has(regName) || regName.toLowerCase().includes("wales") || regName.toLowerCase().includes("gwynedd")) return "Wales";
  return "England";
}

function isHighlandsRegion(regName, lat) {
  if (HIGHLANDS_REGIONS.has(regName)) return true;
  if (regName && (regName.toLowerCase().includes("highland") || regName.toLowerCase().includes("orkney") ||
                  regName.toLowerCase().includes("shetland") || regName.toLowerCase().includes("moray") ||
                  regName.toLowerCase().includes("aberdeen") || regName.toLowerCase().includes("argyll"))) return true;
  if (lat != null && lat >= 56.7) return true;
  return false;
}

function isScotlandRegion(regName, lat) {
  if (getRegionCountry(regName) === "Scotland") return true;
  if (lat != null && lat >= 55.8) return true;
  return false;
}

function haversineMiles(lat1, lon1, lat2, lon2) {
  const R = 3958.8;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

const UK_CITY_COORDS = {
  "london": [51.5074, -0.1278],
  "birmingham": [52.4862, -1.8904],
  "manchester": [53.4808, -2.2426],
  "bristol": [51.4545, -2.5879],
  "leeds": [53.8008, -1.5491],
  "sheffield": [53.3811, -1.4701],
  "liverpool": [53.4084, -2.9916],
  "newcastle": [54.9783, -1.6178],
  "nottingham": [52.9548, -1.1581],
  "cardiff": [51.4816, -3.1791],
  "swansea": [51.6214, -3.9436],
  "edinburgh": [55.9533, -3.1883],
  "glasgow": [55.8642, -4.2518],
  "oxford": [51.7520, -1.2577],
  "cambridge": [52.2053, 0.1218],
  "exeter": [50.7184, -3.5339],
  "plymouth": [50.3755, -4.1427],
  "norwich": [52.6309, 1.2974],
  "southampton": [50.9097, -1.4044],
  "brighton": [50.8225, -0.1372],
  "york": [53.9599, -1.0873],
  "inverness": [57.4778, -4.2247],
  "aberdeen": [57.1497, -2.0943],
};

let geoDebounce = null;
function resolveNearLocation(query) {
  if (!query || !query.trim()) {
    F.nearCoords = null;
    const stat = document.getElementById("geoStatus");
    if (stat) stat.textContent = "";
    refresh();
    return;
  }
  const q = query.trim().toLowerCase();
  if (UK_CITY_COORDS[q]) {
    F.nearCoords = { lat: UK_CITY_COORDS[q][0], lng: UK_CITY_COORDS[q][1], name: query.trim() };
    const stat = document.getElementById("geoStatus");
    if (stat) stat.textContent = `✓ Center: ${query.trim()}`;
    refresh();
    return;
  }
  const cleanPc = encodeURIComponent(query.trim().replace(/\s+/g, ""));
  fetch(`https://api.postcodes.io/postcodes/${cleanPc}`)
    .then((r) => (r.ok ? r.json() : fetch(`https://api.postcodes.io/outcodes/${cleanPc}`).then((r2) => r2.json())))
    .then((data) => {
      if (data && data.result) {
        const res = data.result;
        F.nearCoords = {
          lat: res.latitude,
          lng: res.longitude,
          name: res.postcode || res.outcode || query.trim(),
        };
        const stat = document.getElementById("geoStatus");
        if (stat) stat.textContent = `✓ Center: ${F.nearCoords.name}`;
        refresh();
      } else {
        F.nearCoords = null;
        const stat = document.getElementById("geoStatus");
        if (stat) stat.textContent = "(location not found)";
        refresh();
      }
    })
    .catch(() => {
      F.nearCoords = null;
      const stat = document.getElementById("geoStatus");
      if (stat) stat.textContent = "";
    });
}

const F = {
  search: "", region: "", country: "", excludeHighlands: false, excludeScotland: false,
  excludedRegions: new Set(),
  maxLat: null, minLat: null,
  radiusMiles: null, nearLocation: "", nearCoords: null,
  filterByMap: false,
  onlyNew: false, onlyFavs: false, hideSeen: false,
  minPrice: null, maxPrice: null, minAcres: null, maxAcres: null,
  minBeds: null, maxBeds: null, minGbp: null, maxGbp: null,
  hideEst: false, types: null,
};

const NUM_FIELDS = ["minPrice", "maxPrice", "minAcres", "maxAcres",
                    "minBeds", "maxBeds", "minGbp", "maxGbp", "maxLat", "radiusMiles"];

function loadFavs() {
  try { favs = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || "[]")); }
  catch (e) { favs = new Set(); }
}
function saveFavs() {
  try { localStorage.setItem(FAV_KEY, JSON.stringify([...favs])); } catch (e) {}
}

// ---- filter persistence in the URL hash ----
function saveHash() {
  const out = {};
  for (const k of Object.keys(F)) {
    if (k === "types") {
      if (F.types) out.types = [...F.types];
      continue;
    }
    if (k === "excludedRegions") {
      if (F.excludedRegions && F.excludedRegions.size > 0) out.excludedRegions = [...F.excludedRegions];
      continue;
    }
    if (k === "nearCoords") continue;
    if (F[k] !== null && F[k] !== "" && F[k] !== false) out[k] = F[k];
  }
  const h = Object.keys(out).length ? "f=" + encodeURIComponent(JSON.stringify(out)) : "";
  try { history.replaceState(null, "", h ? "#" + h : location.pathname); } catch (e) {}
}

function loadHash() {
  const m = location.hash.match(/f=([^&]+)/);
  if (!m) return;
  try {
    const o = JSON.parse(decodeURIComponent(m[1]));
    for (const k of Object.keys(o)) {
      if (k === "types") F.types = new Set(o[k]);
      else if (k === "excludedRegions") F.excludedRegions = new Set(o[k]);
      else F[k] = o[k];
    }
    if (F.nearLocation) {
      resolveNearLocation(F.nearLocation);
    }
  } catch (e) {}
}

const fmt = {
  gbp: (n) => n == null ? "—" : "£" + n.toLocaleString("en-GB"),
  gbpAcre: (n) => n == null ? "—" : "£" + Math.round(n).toLocaleString("en-GB"),
  acres: (r) => {
    if (r.acres_min == null) return "—";
    const f = (v) => (v >= 100 ? Math.round(v) : v >= 1 ? +v.toFixed(1) : +v.toFixed(2));
    if (r.acres_max != null && r.acres_max !== r.acres_min) return f(r.acres_min) + " – " + f(r.acres_max);
    return String(f(r.acres_min));
  },
  ac100k: (n) => n == null ? "—" : (n >= 100 ? Math.round(n) : n >= 1 ? +n.toFixed(1) : +n.toFixed(2)),
  days: (ts) => {
    if (!ts) return "";
    const d = (Date.now() - new Date(ts)) / 86400000;
    if (d < 0) return "";
    if (d < 1) return "today";
    return Math.floor(d) + "d";
  },
};

function rowsFor() {
  if (!DATA) return [];
  let rows = DATA[view] || [];
  const s = (F.search || "").toLowerCase();
  rows = rows.map((r) => {
    const corr = corrections[String(r.rm_id)];
    if (corr == null) return r;
    const c = Object.assign({}, r);
    c.acres_min = c.acres_max = c.acres_mid = corr;
    c.confidence = "manual";
    c.gbp_per_acre = c.price >= 1000 ? Math.round((c.price / corr) * 100) / 100 : null;
    c.acres_per_100k = c.price >= 1000 ? Math.round((corr / (c.price / 100000)) * 1000) / 1000 : null;
    return c;
  });
  rows = rows.filter((r) => {
    if (s && !(r.address || "").toLowerCase().includes(s) && !(r.region_name || "").toLowerCase().includes(s)) return false;
    if (F.region && r.region_name !== F.region) return false;

    // Excluded regions list
    if (F.excludedRegions && F.excludedRegions.size > 0 && F.excludedRegions.has(r.region_name)) return false;

    // Quick exclusion toggles
    if (F.excludeHighlands && (r.is_highlands || isHighlandsRegion(r.region_name, r.lat))) return false;
    if (F.excludeScotland && (r.country === "Scotland" || isScotlandRegion(r.region_name, r.lat))) return false;

    // Country selection
    if (F.country) {
      const c = r.country || getRegionCountry(r.region_name);
      if (F.country === "England & Wales") {
        if (c === "Scotland" || isScotlandRegion(r.region_name, r.lat)) return false;
      } else if (F.country === "England") {
        if (c !== "England" || isScotlandRegion(r.region_name, r.lat)) return false;
      } else if (F.country === "Wales") {
        if (c !== "Wales") return false;
      } else if (F.country === "Scotland") {
        if (c !== "Scotland" && !isScotlandRegion(r.region_name, r.lat)) return false;
      }
    }

    // Latitude cutoff
    if (F.maxLat != null && r.lat != null && r.lat > F.maxLat) return false;

    // Radius / Distance filter
    if (F.radiusMiles != null && F.nearCoords && r.lat != null && r.lng != null) {
      const dist = haversineMiles(F.nearCoords.lat, F.nearCoords.lng, r.lat, r.lng);
      if (dist > F.radiusMiles) return false;
    }

    // Map view sync
    if (F.filterByMap && mapView && mapObj && r.lat != null && r.lng != null) {
      if (!mapObj.getBounds().contains([r.lat, r.lng])) return false;
    }

    if (F.onlyFavs && !favs.has(String(r.rm_id))) return false;
    if (F.hideSeen && visited.has(String(r.rm_id))) return false;
    if (F.types && !F.types.has(r.subtype)) return false;
    if (F.hideEst && r.confidence === "est") return false;
    if (F.minPrice != null && (r.price || 0) < F.minPrice) return false;
    if (F.maxPrice != null && (r.price || 0) > F.maxPrice) return false;
    const a = r.acres_mid || 0;
    if (F.minAcres != null && a < F.minAcres) return false;
    if (F.maxAcres != null && a > F.maxAcres) return false;
    if (F.minBeds != null && (r.beds || 0) < F.minBeds) return false;
    if (F.maxBeds != null && (r.beds || 0) > F.maxBeds) return false;
    const g = r.gbp_per_acre;
    if (F.minGbp != null && g < F.minGbp) return false;
    if (F.maxGbp != null && g > F.maxGbp) return false;
    if (F.onlyNew &&
        !["Added today", "Reduced today", "Added yesterday", "Reduced yesterday"]
          .some((x) => (r.listing_status || "").includes(x.replace(" today", "").replace(" yesterday", ""))) &&
        !(r.first_seen && (Date.now() - new Date(r.first_seen)) / 86400000 < 2)) return false;
    return true;
  });
  rows.sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortAsc ? av - bv : bv - av;
  });
  return rows;
}
  rows.sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortAsc ? av - bv : bv - av;
  });
  return rows;
}

function vsRegion(r) {
  if (r.value_ratio == null) return `<td class="num dim">—</td>`;
  const v = r.value_ratio;
  const cls = v >= 3 ? "great" : v >= 1.5 ? "good" : v >= 1 ? "ok" : "weak";
  const txt = v >= 10 ? Math.round(v) + "×" : v.toFixed(1) + "×";
  return `<td class="num ${cls}" title="median £/acre in this region: ${fmt.gbpAcre(r.region_median)}">${txt}</td>`;
}

function starCell(r) {
  const on = favs.has(String(r.rm_id));
  return `<td class="fav-col"><button class="star ${on ? "on" : ""}" data-id="${r.rm_id}">${on ? "★" : "☆"}</button></td>`;
}

function badgeCell(r) {
  let badge = "";
  if (r.verified) badge = `<span class="badge okb" title="stated acreage agrees with the registered plot boundary">✓</span>`;
  if (r.flag === "stated-vs-plot") badge += `<span class="badge warnb" title="stated acreage disagrees with registered plot boundary - verify with agent">⚠</span>`;
  if (r.flag === "low-confidence") badge += `<span class="badge warnb" title="low-confidence parse">⚠</span>`;
  if (corrections[String(r.rm_id)] != null) badge += `<span class="badge okb" title="manually corrected">✎</span>`;
  return `<td class="badge-td">${badge}</td>`;
}

function render() {
  if (!DATA) return;
  const rows = rowsFor();
  const tb = document.getElementById("rows");
  tb.innerHTML = rows.slice(0, 500).map((r, i) => {
    const conf = r.confidence || "";
    const seen = visited.has(String(r.rm_id));
    return `<tr class="${seen ? "seen" : ""}">
      <td class="num">${i + 1}</td>
      ${starCell(r)}
      ${badgeCell(r)}
      <td class="num strong">${fmt.gbpAcre(r.gbp_per_acre)}</td>
      <td class="num">${fmt.acres(r)}</td>
      <td class="num dim">${fmt.ac100k(r.acres_per_100k)}</td>
      <td class="num">${fmt.gbp(r.price)}</td>
      <td><a href="${r.url}" target="_blank" rel="noopener" data-vid="${r.rm_id}">${escapeHtml(r.address || "")}</a>
        <button class="edit-acres" data-id="${r.rm_id}" data-acres="${r.acres_mid ?? ""}" title="correct the acreage">✎</button>
      </td>
      <td class="dim">${escapeHtml(r.subtype || "")}</td>
      <td>${escapeHtml(r.region_name || "")}</td>
      ${vsRegion(r)}
      <td class="num dim">${fmt.days(r.first_published || r.first_seen)}</td>
      <td class="conf" title="${escapeHtml(r.matched || "")}">${escapeHtml(conf)}</td>
    </tr>`;
  }).join("");
  document.getElementById("count").textContent =
    `${rows.length} listings · newest ${fmt.days(DATA.ts)} · showing top 500`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let mapView = false;
let mapObj = null;
let mapLayer = null;
let LAYERS = null;
let airportLayer = null;
let crimeLayer = null;
let floodLayer = null;
let gpLayer = null;
let parkLayer = null;
let schoolLayer = null;

function loadLayers() {
  if (LAYERS) return Promise.resolve(LAYERS);
  return fetch("layers.json")
    .then((r) => r.json())
    .then((d) => {
      LAYERS = d;
      if (d.schools && d.schools.length) {
        document.getElementById("schoolsToggle").hidden = false;
      } else if (d.schools_note) {
        document.getElementById("layerNote").textContent = d.schools_note;
      }
      return d;
    })
    .catch(() => { LAYERS = null; return null; });
}

function listingPinsVisible() {
  return document.getElementById("layerListings").checked;
}

function renderAirportLayer() {
  if (!mapObj || !LAYERS || !LAYERS.airports) return;
  if (airportLayer) airportLayer.remove();
  if (!document.getElementById("layerAirports").checked) return;
  airportLayer = L.layerGroup().addTo(mapObj);
  for (const a of LAYERS.airports) {
    L.circleMarker([a.lat, a.lng], { radius: 4, color: "#38bdf8", weight: 1, fillOpacity: 0.9 })
      .bindPopup(`<b>${escapeHtml(a.name)}</b>${a.iata ? " (" + escapeHtml(a.iata) + ")" : ""}`)
      .addTo(airportLayer);
    L.circle([a.lat, a.lng], { radius: 15000, color: "#38bdf8", weight: 1, opacity: 0.25, fillOpacity: 0.05 })
      .addTo(airportLayer);
  }
}

function renderCrimeLayer() {
  if (!mapObj || !LAYERS || !LAYERS.crimes || !window.L || !L.heatLayer) return;
  if (crimeLayer) crimeLayer.remove();
  if (!document.getElementById("layerCrime").checked) return;
  const pts = LAYERS.crimes.map((c) => [c[0], c[1], Math.min(1, c[2] / 40)]);
  crimeLayer = L.heatLayer(pts, { radius: 22, blur: 16, maxZoom: 12, minOpacity: 0.15 }).addTo(mapObj);
}

function renderFloodLayer() {
  if (!mapObj || !LAYERS || !LAYERS.flood || !window.L || !L.heatLayer) return;
  if (floodLayer) floodLayer.remove();
  if (!document.getElementById("layerFlood").checked) return;
  const pts = LAYERS.flood.map((c) => [c[0], c[1], 0.6]);
  floodLayer = L.heatLayer(pts, {
    radius: 26, blur: 18, maxZoom: 12, minOpacity: 0.4,
    gradient: { 0.4: "#3b82f6", 0.7: "#2563eb", 1: "#1e40af" },
  }).addTo(mapObj);
}

function renderParkLayer() {
  if (!mapObj || !LAYERS || !LAYERS.parks) return;
  if (parkLayer) parkLayer.remove();
  if (!document.getElementById("layerParks").checked) return;
  parkLayer = L.layerGroup().addTo(mapObj);
  for (const p of LAYERS.parks) {
    L.circleMarker([p.lat, p.lng], { radius: 3.5, color: "#22c55e", weight: 1, fillOpacity: 0.7 })
      .bindPopup(`<b>${escapeHtml(p.name || "unnamed park")}</b><br>${p.area_ha} ha`)
      .addTo(parkLayer);
  }
}

const RATING_COLORS = {
  "Outstanding": "#4ade80", "Good": "#a3e635",
  "Requires improvement": "#f59e0b", "Inadequate": "#f87171",
};

function renderBoundedPoints() {
  // GPs and schools: only render what's in view (they number in the thousands)
  if (!mapObj || !LAYERS) return;
  const bounds = mapObj.getBounds();
  const zoom = mapObj.getZoom();

  if (gpLayer) gpLayer.remove();
  if (document.getElementById("layerGps").checked && LAYERS.gps && zoom >= 8) {
    gpLayer = L.layerGroup().addTo(mapObj);
    let n = 0;
    for (const g of LAYERS.gps) {
      if (n >= 350) break;
      if (!bounds.contains([g.lat, g.lng])) continue;
      L.circleMarker([g.lat, g.lng], { radius: 3, color: "#f87171", weight: 1, fillOpacity: 0.85 })
        .bindPopup(`<b>${escapeHtml(g.name)}</b><br>GP surgery`)
        .addTo(gpLayer);
      n++;
    }
  }

  if (schoolLayer) schoolLayer.remove();
  if (document.getElementById("layerSchools").checked && LAYERS.schools && zoom >= 9) {
    schoolLayer = L.layerGroup().addTo(mapObj);
    let n = 0;
    for (const s of LAYERS.schools) {
      if (n >= 350) break;
      if (!bounds.contains([s.lat, s.lng])) continue;
      const col = RATING_COLORS[s.rating] || "#8b96a5";
      L.circleMarker([s.lat, s.lng], { radius: 4, color: col, weight: 1, fillOpacity: 0.9 })
        .bindPopup(`<b>${escapeHtml(s.name)}</b><br>Ofsted: ${escapeHtml(s.rating || "n/a")} · ${escapeHtml(s.phase || "")}`)
        .addTo(schoolLayer);
      n++;
    }
  }
}

function renderAllLayers() {
  renderAirportLayer();
  renderCrimeLayer();
  renderFloodLayer();
  renderParkLayer();
  renderBoundedPoints();
}

function mapColor(r) {
  const v = r.value_ratio;
  if (v == null) return "#8b96a5";
  if (v >= 3) return "#4ade80";
  if (v >= 1.5) return "#a3e635";
  if (v >= 1) return "#e2e8f0";
  return "#64748b";
}

function renderMap() {
  if (!DATA || !window.L) return;
  const rows = rowsFor().filter((r) => r.lat != null && r.lng != null).slice(0, 1200);
  if (!mapObj) {
    mapObj = L.map("map").setView([54.5, -2.5], 6);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(mapObj);
    mapObj.on("moveend zoomend", () => {
      renderBoundedPoints();
      if (F.filterByMap) {
        render();
      }
    });
  }
  if (mapLayer) mapLayer.remove();
  mapLayer = L.layerGroup().addTo(mapObj);
  if (listingPinsVisible()) {
    for (const r of rows) {
      const pop = `<b>${escapeHtml(r.address || "")}</b><br>` +
        `${fmt.gbpAcre(r.gbp_per_acre)}/acre · ${fmt.acres(r)} ac · ${fmt.gbp(r.price)}` +
        (r.verified ? " ✓" : "") +
        `<br><a href="${r.url}" target="_blank" rel="noopener">view on Rightmove</a>`;
      L.circleMarker([r.lat, r.lng], {
        radius: r.land_only ? 7 : 5,
        color: mapColor(r),
        weight: 1,
        fillColor: mapColor(r),
        fillOpacity: 0.75,
      }).bindPopup(pop).addTo(mapLayer);
    }
  }
  document.getElementById("mapCount").textContent =
    `${listingPinsVisible() ? rows.length + " listings on map" : "listing pins hidden"}`;
  const all = rows.filter((r) => r.lat != null);
  if (all.length && listingPinsVisible()) {
    const bounds = L.latLngBounds(all.map((r) => [r.lat, r.lng]));
    mapObj.fitBounds(bounds, { padding: [20, 20], maxZoom: 10 });
  }
}

function toggleMap() {
  mapView = !mapView;
  const wrap = document.getElementById("mapwrap");
  const tableMain = document.getElementById("tableMain");
  const btn = document.getElementById("mapToggle");
  wrap.hidden = !mapView;
  tableMain.hidden = mapView;
  btn.classList.toggle("active", mapView);
  if (mapView) {
    renderMap();
    loadLayers().then(renderAllLayers);
    setTimeout(() => { if (mapObj) mapObj.invalidateSize(); }, 50);
  }
}

function renderStats() {
  if (!DATA) return;
  const s = DATA.stats || {};
  document.getElementById("stats").innerHTML =
    `<span><b>${s.listings ?? "—"}</b> tracked</span>` +
    `<span><b>${s.with_land ?? "—"}</b> with land</span>` +
    `<span><b>${s.land_only ?? "—"}</b> bare land</span>` +
    `<span>updated <b>${new Date(DATA.ts).toLocaleString("en-GB")}</b></span>`;
}

function renderNewStrip() {
  if (!DATA) return;
  const strip = document.getElementById("newstrip");
  const events = (DATA.events || []).filter((e) =>
    (e.event === "new" || e.event === "reduced") && e.gbp_per_acre != null);
  events.sort((a, b) => (a.gbp_per_acre || 0) - (b.gbp_per_acre || 0));
  if (!events.length) { strip.hidden = true; return; }
  strip.hidden = false;
  document.getElementById("newlist").innerHTML = events.slice(0, 12).map((e) =>
    `<li><b class="ev">${e.event === "new" ? "NEW" : "REDUCED"}</b>
     ${fmt.gbpAcre(e.gbp_per_acre)}/acre · ${e.acres_mid != null ? e.acres_mid + " ac" : ""} ·
     ${escapeHtml(e.address || e.rm_id)}</li>`).join("");
}

function renderEvents() {
  if (!DATA) return;
  const ul = document.getElementById("events");
  const evs = (DATA.events || []).slice(-30).reverse();
  ul.innerHTML = evs.map((e) => {
    const icon = { new: "🆕", reduced: "🔻", increased: "🔺", removed: "❌", acre_update: "📐" }[e.event] || "•";
    const extra = e.gbp_per_acre ? ` · ${fmt.gbpAcre(e.gbp_per_acre)}/acre` : "";
    return `<li><span class="ev">${icon} ${e.event}</span> ${escapeHtml(e.address || e.rm_id)}${extra} <span class="evts">${new Date(e.ts).toLocaleString("en-GB")}</span></li>`;
  }).join("");
}

function renderRegions() {
  if (!DATA) return;
  const sel = document.getElementById("region");
  const names = [...new Set((DATA.all || []).map((r) => r.region_name).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">All regions</option>' +
    names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  populateExcludeRegionDropdown(names);
}

function populateExcludeRegionDropdown(names) {
  const sel = document.getElementById("addExcludeRegion");
  if (!sel) return;
  sel.innerHTML = '<option value="">+ Choose region to exclude...</option>' +
    names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
}

function renderExcludedRegionChips() {
  const box = document.getElementById("excludedRegionChips");
  if (!box) return;
  if (!F.excludedRegions || F.excludedRegions.size === 0) {
    box.innerHTML = `<span class="dim mini" style="padding: 4px 0;">No specific regions excluded</span>`;
    return;
  }
  const items = [...F.excludedRegions].sort();
  box.innerHTML = items.map((reg) =>
    `<button class="chip excl" type="button">${escapeHtml(reg)} <span class="chip-remove" data-reg="${escapeHtml(reg)}" title="Remove exclusion">×</span></button>`
  ).join("");
}

function updatePresetButtons() {
  document.querySelectorAll(".preset-btn").forEach((b) => {
    const p = b.dataset.preset;
    let active = false;
    if (p === "all") active = !F.country && !F.excludeHighlands && !F.excludeScotland;
    else if (p === "ew") active = F.country === "England & Wales";
    else if (p === "eng") active = F.country === "England";
    else if (p === "wales") active = F.country === "Wales";
    else if (p === "scotland") active = F.country === "Scotland";
    else if (p === "nohighlands") active = !!F.excludeHighlands;
    else if (p === "noscotland") active = !!F.excludeScotland;
    b.classList.toggle("active", active);
  });
  document.querySelectorAll(".lat-btn").forEach((b) => {
    const latStr = b.dataset.lat;
    const latVal = latStr ? parseFloat(latStr) : null;
    const active = (latVal === null && F.maxLat === null) || (latVal !== null && F.maxLat === latVal);
    b.classList.toggle("active", active);
  });
}

function applyPreset(preset) {
  if (preset === "all") {
    F.country = "";
    F.excludeHighlands = false;
    F.excludeScotland = false;
  } else if (preset === "ew") {
    F.country = "England & Wales";
    F.excludeScotland = false;
    F.excludeHighlands = false;
  } else if (preset === "eng") {
    F.country = "England";
    F.excludeScotland = false;
    F.excludeHighlands = false;
  } else if (preset === "wales") {
    F.country = "Wales";
    F.excludeScotland = false;
    F.excludeHighlands = false;
  } else if (preset === "scotland") {
    F.country = "Scotland";
    F.excludeScotland = false;
    F.excludeHighlands = false;
  } else if (preset === "nohighlands") {
    F.excludeHighlands = true;
    if (F.country === "Scotland") F.country = "";
  } else if (preset === "noscotland") {
    F.excludeScotland = true;
    F.excludeHighlands = false;
    if (F.country === "Scotland") F.country = "";
  }
  applyFToInputs();
  refresh();
}

function resetAreaFilters() {
  F.country = "";
  F.region = "";
  F.excludeHighlands = false;
  F.excludeScotland = false;
  F.excludedRegions = new Set();
  F.maxLat = null;
  F.minLat = null;
  F.radiusMiles = null;
  F.nearLocation = "";
  F.nearCoords = null;
  F.filterByMap = false;
  const stat = document.getElementById("geoStatus");
  if (stat) stat.textContent = "";
  applyFToInputs();
  refresh();
}

function renderSold() {
  if (!DATA) return;
  const sold = DATA.sold || [];
  const sec = document.getElementById("soldsec");
  if (!sold.length) { sec.hidden = true; return; }
  sec.hidden = false;
  document.getElementById("soldBody").innerHTML = sold.slice(0, 60).map((r) => {
    const disc = r.discount_pct != null
      ? `<td class="num ${r.discount_pct <= -5 ? "good" : r.discount_pct >= 5 ? "weak" : "dim"}">${r.discount_pct > 0 ? "+" : ""}${r.discount_pct}%</td>`
      : `<td class="num dim">—</td>`;
    const conf = r.sold_confidence === "strong"
      ? `<td class="conf" title="full postcode match">✓ strong</td>`
      : `<td class="dim" title="outcode-level match, verify">~ weak</td>`;
    return `<tr>
      <td><a href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.address || "")}</a></td>
      <td class="num">${fmt.acres(r)}</td>
      <td class="num dim">${fmt.gbp(r.price)}</td>
      <td class="num strong">${fmt.gbp(r.sold_price)}</td>
      <td class="num strong">${fmt.gbpAcre(r.sold_gbp_per_acre)}</td>
      ${disc}
      ${conf}
    </tr>`;
  }).join("");
}

function renderRegionSummary() {
  if (!DATA || !DATA.regions || !DATA.regions.length) return;
  const tb = document.getElementById("regsumBody");
  tb.innerHTML = DATA.regions.slice(0, 100).map((r) => {
    const cheapest = r.cheapest_url
      ? `<a href="${r.cheapest_url}" target="_blank" rel="noopener" title="${escapeHtml(r.cheapest_address || "")}">${fmt.gbpAcre(r.cheapest_gbp)}/ac · ${fmt.ac100k(r.cheapest_acres)}ac</a>`
      : "—";
    const isExcl = F.excludedRegions && F.excludedRegions.has(r.region);
    const country = r.country || getRegionCountry(r.region);
    return `<tr class="clickable ${isExcl ? "dim" : ""}" data-reg="${escapeHtml(r.region)}">
      <td>
        <b>${escapeHtml(r.region)}</b>
        <span class="dim mini" style="margin-left: 4px;">(${country})</span>
        <button type="button" class="btn-exclude-reg" data-excl="${escapeHtml(r.region)}" title="${isExcl ? 'Unexclude this region' : 'Exclude this region'}">${isExcl ? 'Excluded ✓' : 'Exclude'}</button>
      </td>
      <td class="num">${r.n} <span class="dim">(${r.land} land)</span></td>
      <td class="num strong">${fmt.gbpAcre(r.median_gbp)}</td>
      <td class="num">${fmt.ac100k(r.median_acres)}</td>
      <td class="dim">${cheapest}</td>
    </tr>`;
  }).join("");
  tb.onclick = (e) => {
    if (e.target.closest("a")) return;
    const exclBtn = e.target.closest(".btn-exclude-reg");
    if (exclBtn) {
      const reg = exclBtn.dataset.excl;
      if (F.excludedRegions.has(reg)) {
        F.excludedRegions.delete(reg);
      } else {
        F.excludedRegions.add(reg);
      }
      renderExcludedRegionChips();
      refresh();
      renderRegionSummary();
      return;
    }
    const tr = e.target.closest("tr[data-reg]");
    if (!tr) return;
    F.region = tr.dataset.reg;
    document.getElementById("region").value = F.region;
    refresh();
  };
}

function renderTypes() {
  if (!DATA) return;
  const meta = DATA.meta || {};
  const subs = (meta.subtypes && meta.subtypes.length)
    ? meta.subtypes
    : [...new Set((DATA.all || []).map((r) => r.subtype).filter(Boolean))].sort();
  const excluded = new Set(meta.excluded_subtypes || []);
  if (F.types === null) F.types = new Set(subs.filter((s) => !excluded.has(s)));
  const box = document.getElementById("typeChips");
  box.innerHTML = subs.map((s) =>
    `<button class="chip ${F.types.has(s) ? "on" : ""}" data-type="${escapeHtml(s)}">${escapeHtml(s)}</button>`
  ).join("");
}

function applyInputsToF() {
  F.search = document.getElementById("search").value;
  F.country = document.getElementById("country") ? document.getElementById("country").value : "";
  F.region = document.getElementById("region").value;
  F.excludeHighlands = document.getElementById("excludeHighlands") ? document.getElementById("excludeHighlands").checked : false;
  F.excludeScotland = document.getElementById("excludeScotland") ? document.getElementById("excludeScotland").checked : false;
  F.filterByMap = document.getElementById("filterByMap") ? document.getElementById("filterByMap").checked : false;
  F.onlyNew = document.getElementById("onlyNew").checked;
  F.onlyFavs = document.getElementById("onlyFavs").checked;
  F.hideEst = document.getElementById("hideEst").checked;
  F.hideSeen = document.getElementById("hideSeen").checked;
  for (const k of NUM_FIELDS) {
    const el = document.getElementById(k);
    if (el) {
      const v = parseFloat(el.value);
      F[k] = Number.isFinite(v) ? v : null;
    }
  }
  const locEl = document.getElementById("nearLocation");
  if (locEl) {
    const locVal = locEl.value;
    if (locVal !== F.nearLocation) {
      F.nearLocation = locVal;
      resolveNearLocation(locVal);
    }
  }
  updatePresetButtons();
}

function applyFToInputs() {
  if (document.getElementById("search")) document.getElementById("search").value = F.search || "";
  if (document.getElementById("country")) document.getElementById("country").value = F.country || "";
  if (document.getElementById("region")) document.getElementById("region").value = F.region || "";
  if (document.getElementById("excludeHighlands")) document.getElementById("excludeHighlands").checked = !!F.excludeHighlands;
  if (document.getElementById("excludeScotland")) document.getElementById("excludeScotland").checked = !!F.excludeScotland;
  if (document.getElementById("filterByMap")) document.getElementById("filterByMap").checked = !!F.filterByMap;
  if (document.getElementById("onlyNew")) document.getElementById("onlyNew").checked = !!F.onlyNew;
  if (document.getElementById("onlyFavs")) document.getElementById("onlyFavs").checked = !!F.onlyFavs;
  if (document.getElementById("hideEst")) document.getElementById("hideEst").checked = !!F.hideEst;
  if (document.getElementById("hideSeen")) document.getElementById("hideSeen").checked = !!F.hideSeen;
  for (const k of NUM_FIELDS) {
    const el = document.getElementById(k);
    if (el) el.value = F[k] != null ? F[k] : "";
  }
  const locEl = document.getElementById("nearLocation");
  if (locEl) locEl.value = F.nearLocation || "";
  renderExcludedRegionChips();
  updatePresetButtons();
}

function refresh() {
  saveHash();
  render();
  if (mapView) renderMap();
  renderRegionSummary();
}

function resetFilters() {
  for (const k of Object.keys(F)) {
    if (k === "types") F.types = null;
    else if (k === "excludedRegions") F.excludedRegions = new Set();
    else if (k === "nearCoords") F.nearCoords = null;
    else F[k] = null;
  }
  F.search = ""; F.country = ""; F.region = "";
  F.excludeHighlands = false; F.excludeScotland = false;
  F.filterByMap = false;
  F.onlyNew = false; F.onlyFavs = false; F.hideEst = false; F.hideSeen = false;
  F.nearLocation = "";
  const stat = document.getElementById("geoStatus");
  if (stat) stat.textContent = "";
  applyFToInputs();
  renderTypes();
  refresh();
}

function bindEvents() {
  document.getElementById("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    view = b.dataset.view;
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  });
  document.getElementById("filterToggle").addEventListener("click", () => {
    const f = document.getElementById("filters");
    f.hidden = !f.hidden;
  });
  document.getElementById("mapToggle").addEventListener("click", toggleMap);
  document.getElementById("layerListings").addEventListener("change", renderMap);
  document.getElementById("layerAirports").addEventListener("change", renderAirportLayer);
  document.getElementById("layerCrime").addEventListener("change", renderCrimeLayer);
  document.getElementById("layerFlood").addEventListener("change", renderFloodLayer);
  document.getElementById("layerGps").addEventListener("change", renderBoundedPoints);
  document.getElementById("layerParks").addEventListener("change", renderParkLayer);
  document.getElementById("layerSchools").addEventListener("change", renderBoundedPoints);
  document.getElementById("resetFilters").addEventListener("click", resetFilters);

  document.getElementById("search").addEventListener("input", () => { applyInputsToF(); refresh(); });
  const countryEl = document.getElementById("country");
  if (countryEl) countryEl.addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("region").addEventListener("input", () => { applyInputsToF(); refresh(); });

  const exclHighEl = document.getElementById("excludeHighlands");
  if (exclHighEl) exclHighEl.addEventListener("change", () => { applyInputsToF(); refresh(); });
  const exclScotEl = document.getElementById("excludeScotland");
  if (exclScotEl) exclScotEl.addEventListener("change", () => { applyInputsToF(); refresh(); });
  const mapFilterEl = document.getElementById("filterByMap");
  if (mapFilterEl) mapFilterEl.addEventListener("change", () => { applyInputsToF(); refresh(); });

  const addExclReg = document.getElementById("addExcludeRegion");
  if (addExclReg) {
    addExclReg.addEventListener("change", (e) => {
      if (e.target.value) {
        F.excludedRegions.add(e.target.value);
        e.target.value = "";
        renderExcludedRegionChips();
        refresh();
      }
    });
  }

  const clearExcl = document.getElementById("clearExcludedRegions");
  if (clearExcl) {
    clearExcl.addEventListener("click", (e) => {
      e.preventDefault();
      F.excludedRegions.clear();
      renderExcludedRegionChips();
      refresh();
    });
  }

  const resetArea = document.getElementById("resetAreaFilters");
  if (resetArea) {
    resetArea.addEventListener("click", (e) => {
      e.preventDefault();
      resetAreaFilters();
    });
  }

  const exclBox = document.getElementById("excludedRegionChips");
  if (exclBox) {
    exclBox.addEventListener("click", (e) => {
      const rm = e.target.closest(".chip-remove");
      if (rm) {
        const reg = rm.dataset.reg;
        F.excludedRegions.delete(reg);
        renderExcludedRegionChips();
        refresh();
      }
    });
  }

  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyPreset(btn.dataset.preset);
    });
  });

  document.querySelectorAll(".lat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const lat = btn.dataset.lat;
      F.maxLat = lat ? parseFloat(lat) : null;
      const input = document.getElementById("maxLat");
      if (input) input.value = F.maxLat != null ? F.maxLat : "";
      updatePresetButtons();
      refresh();
    });
  });

  const nearLoc = document.getElementById("nearLocation");
  if (nearLoc) {
    nearLoc.addEventListener("input", (e) => {
      if (geoDebounce) clearTimeout(geoDebounce);
      geoDebounce = setTimeout(() => {
        applyInputsToF();
      }, 400);
    });
  }

  document.getElementById("onlyNew").addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("onlyFavs").addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("hideEst").addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("hideSeen").addEventListener("change", () => { applyInputsToF(); refresh(); });
  for (const k of NUM_FIELDS) {
    const el = document.getElementById(k);
    if (el) el.addEventListener("input", () => { applyInputsToF(); refresh(); });
  }

  document.getElementById("typeChips").addEventListener("click", (e) => {
    const c = e.target.closest(".chip");
    if (!c) return;
    const t = c.dataset.type;
    if (!F.types) F.types = new Set(DATA.meta.subtypes);
    if (F.types.has(t)) F.types.delete(t); else F.types.add(t);
    c.classList.toggle("on");
    refresh();
  });
  document.getElementById("typesAll").addEventListener("click", (e) => {
    e.preventDefault();
    F.types = null;
    renderTypes();
    refresh();
  });
  document.getElementById("typesNone").addEventListener("click", (e) => {
    e.preventDefault();
    F.types = new Set();
    renderTypes();
    refresh();
  });
  document.getElementById("rows").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-vid]");
    if (a) {
      const id = a.dataset.vid;
      visited.add(id);
      saveVisited();
      if (F.hideSeen) {
        render();
      } else {
        const tr = a.closest("tr");
        if (tr) tr.classList.add("seen");
      }
      return;
    }
    const b = e.target.closest(".star");
    if (!b) return;
    const id = b.dataset.id;
    if (favs.has(id)) favs.delete(id); else favs.add(id);
    saveFavs();
    render();
  });
  document.getElementById("rows").addEventListener("click", (e) => {
    const b = e.target.closest(".edit-acres");
    if (!b) return;
    const id = b.dataset.id;
    const cur = corrections[id] != null ? corrections[id] : b.dataset.acres;
    const val = prompt("Correct acreage for this listing (blank removes correction):", cur);
    if (val === null) return;
    if (val.trim() === "") {
      delete corrections[id];
    } else {
      const n = parseFloat(val.replace(",", "."));
      if (!Number.isFinite(n) || n <= 0) return alert("Enter a positive number of acres");
      corrections[id] = n;
    }
    saveCorrections();
    render();
  });
  document.querySelectorAll("#rank th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (k === "rank") return;
      if (sortKey === k) sortAsc = !sortAsc;
      else { sortKey = k; sortAsc = k !== "gbp_per_acre" && k !== "value_ratio"; }
      document.querySelectorAll("#rank th").forEach((x) => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(sortAsc ? "sort-asc" : "sort-desc");
      render();
    });
  });
}

loadFavs();
loadCorrections();
loadVisited();
loadHash();
fetch("data.json")
  .then((r) => r.json())
  .then((d) => {
    DATA = d;
    renderStats();
    renderRegions();
    renderTypes();
    renderRegionSummary();
    renderSold();
    applyFToInputs();
    renderNewStrip();
    renderEvents();
    render();
  })
  .catch((e) => {
    document.getElementById("rows").innerHTML = `<tr><td colspan="13">failed to load data.json: ${e}</td></tr>`;
  });

bindEvents();
