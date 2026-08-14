"use strict";

let DATA = null;
let view = "all";
let sortKey = "gbp_per_acre";
let sortAsc = true;

const fmt = {
  gbp: (n) => n == null ? "—" : "£" + n.toLocaleString("en-GB"),
  gbpAcre: (n) => n == null ? "—" : "£" + Math.round(n).toLocaleString("en-GB"),
  acres: (r) => {
    if (r.acres_min == null) return "—";
    const f = (v) => (v >= 100 ? Math.round(v) : v >= 1 ? +v.toFixed(1) : +v.toFixed(2));
    if (r.acres_max != null && r.acres_max !== r.acres_min) return f(r.acres_min) + " – " + f(r.acres_max);
    return String(f(r.acres_min));
  },
  days: (ts) => {
    if (!ts) return "";
    const d = (Date.now() - new Date(ts)) / 86400000;
    if (d < 1) return "today";
    return Math.floor(d) + "d";
  },
};

function rowsFor() {
  if (!DATA) return [];
  let rows = DATA[view] || [];
  const minA = parseFloat(document.getElementById("minAcres").value) || 0;
  const maxP = parseFloat(document.getElementById("maxPrice").value) || Infinity;
  const reg = document.getElementById("region").value;
  const onlyNew = document.getElementById("onlyNew").checked;
  rows = rows.filter((r) =>
    (r.acres_mid || 0) >= minA &&
    (r.price || 0) <= maxP &&
    (!reg || r.region_name === reg) &&
    (!onlyNew || ["Added today", "Reduced today", "Added yesterday", "Reduced yesterday"]
      .some((s) => (r.listing_status || "").includes(s.replace(" today", "").replace(" yesterday", ""))) ||
      (r.first_seen && (Date.now() - new Date(r.first_seen)) / 86400000 < 2))
  );
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

function render() {
  if (!DATA) return;
  const rows = rowsFor();
  const tb = document.getElementById("rows");
  tb.innerHTML = rows.slice(0, 500).map((r, i) => {
    const conf = r.confidence || "";
    const status = r.listing_status || "";
    return `<tr>
      <td class="num">${i + 1}</td>
      <td class="num strong">${fmt.gbpAcre(r.gbp_per_acre)}</td>
      <td class="num">${fmt.acres(r)}</td>
      <td class="num">${fmt.gbp(r.price)}</td>
      <td><a href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.address || "")}</a></td>
      <td class="dim">${escapeHtml(r.subtype || "")}</td>
      <td>${escapeHtml(r.region_name || "")}</td>
      <td class="conf" title="${escapeHtml(r.matched || "")}">${escapeHtml(conf)}</td>
      <td class="status">${escapeHtml(status)}</td>
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
    `<span><b>${s.listings ?? "—"}</b> scanned</span>` +
    `<span><b>${s.with_land ?? "—"}</b> with land</span>` +
    `<span><b>${s.land_only ?? "—"}</b> bare land</span>` +
    `<span>updated <b>${new Date(DATA.ts).toLocaleString("en-GB")}</b></span>`;
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

function bindEvents() {
  document.getElementById("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    view = b.dataset.view;
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  });
  for (const id of ["minAcres", "maxPrice", "region"]) {
    document.getElementById(id).addEventListener("input", render);
  }
  document.getElementById("onlyNew").addEventListener("change", render);
  document.querySelectorAll("#rank th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (k === "rank") return;
      if (sortKey === k) sortAsc = !sortAsc;
      else { sortKey = k; sortAsc = k !== "gbp_per_acre"; }
      document.querySelectorAll("#rank th").forEach((x) => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(sortAsc ? "sort-asc" : "sort-desc");
      render();
    });
  });
}

fetch("data.json")
  .then((r) => r.json())
  .then((d) => {
    DATA = d;
    renderStats();
    renderRegions();
    renderEvents();
    render();
  })
  .catch((e) => {
    document.getElementById("rows").innerHTML = `<tr><td colspan="9">failed to load data.json: ${e}</td></tr>`;
  });

bindEvents();
