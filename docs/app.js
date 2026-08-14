"use strict";

let DATA = null;
let view = "all";
let sortKey = "gbp_per_acre";
let sortAsc = true;
let favs = new Set();

const FAV_KEY = "apf_favs";

const F = {
  search: "", region: "", onlyNew: false, onlyFavs: false,
  minPrice: null, maxPrice: null, minAcres: null, maxAcres: null,
  minBeds: null, maxBeds: null, minGbp: null, maxGbp: null,
  hideEst: false, types: null,
};

const NUM_FIELDS = ["minPrice", "maxPrice", "minAcres", "maxAcres",
                    "minBeds", "maxBeds", "minGbp", "maxGbp"];

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
      else F[k] = o[k];
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
  rows = rows.filter((r) => {
    if (s && !(r.address || "").toLowerCase().includes(s) && !(r.region_name || "").toLowerCase().includes(s)) return false;
    if (F.region && r.region_name !== F.region) return false;
    if (F.onlyFavs && !favs.has(String(r.rm_id))) return false;
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

function render() {
  if (!DATA) return;
  const rows = rowsFor();
  const tb = document.getElementById("rows");
  tb.innerHTML = rows.slice(0, 500).map((r, i) => {
    const conf = r.confidence || "";
    return `<tr>
      <td class="num">${i + 1}</td>
      ${starCell(r)}
      <td class="num strong">${fmt.gbpAcre(r.gbp_per_acre)}</td>
      <td class="num">${fmt.acres(r)}</td>
      <td class="num dim">${fmt.ac100k(r.acres_per_100k)}</td>
      <td class="num">${fmt.gbp(r.price)}</td>
      <td><a href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.address || "")}</a></td>
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
    names.map((n) => `<option>${escapeHtml(n)}</option>`).join("");
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
  F.region = document.getElementById("region").value;
  F.onlyNew = document.getElementById("onlyNew").checked;
  F.onlyFavs = document.getElementById("onlyFavs").checked;
  F.hideEst = document.getElementById("hideEst").checked;
  for (const k of NUM_FIELDS) {
    const v = parseFloat(document.getElementById(k).value);
    F[k] = Number.isFinite(v) ? v : null;
  }
}

function applyFToInputs() {
  document.getElementById("search").value = F.search || "";
  document.getElementById("region").value = F.region || "";
  document.getElementById("onlyNew").checked = !!F.onlyNew;
  document.getElementById("onlyFavs").checked = !!F.onlyFavs;
  document.getElementById("hideEst").checked = !!F.hideEst;
  for (const k of NUM_FIELDS) {
    document.getElementById(k).value = F[k] != null ? F[k] : "";
  }
}

function refresh() { saveHash(); render(); }

function resetFilters() {
  for (const k of Object.keys(F)) {
    if (k === "types") F.types = null;
    else F[k] = null;
  }
  F.search = ""; F.region = ""; F.onlyNew = false; F.onlyFavs = false; F.hideEst = false;
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
  document.getElementById("resetFilters").addEventListener("click", resetFilters);
  document.getElementById("search").addEventListener("input", () => { applyInputsToF(); refresh(); });
  document.getElementById("region").addEventListener("input", () => { applyInputsToF(); refresh(); });
  document.getElementById("onlyNew").addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("onlyFavs").addEventListener("change", () => { applyInputsToF(); refresh(); });
  document.getElementById("hideEst").addEventListener("change", () => { applyInputsToF(); refresh(); });
  for (const k of NUM_FIELDS) {
    document.getElementById(k).addEventListener("input", () => { applyInputsToF(); refresh(); });
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
    const b = e.target.closest(".star");
    if (!b) return;
    const id = b.dataset.id;
    if (favs.has(id)) favs.delete(id); else favs.add(id);
    saveFavs();
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
loadHash();
fetch("data.json")
  .then((r) => r.json())
  .then((d) => {
    DATA = d;
    renderStats();
    renderRegions();
    renderTypes();
    applyFToInputs();
    renderNewStrip();
    renderEvents();
    render();
  })
  .catch((e) => {
    document.getElementById("rows").innerHTML = `<tr><td colspan="12">failed to load data.json: ${e}</td></tr>`;
  });

bindEvents();
