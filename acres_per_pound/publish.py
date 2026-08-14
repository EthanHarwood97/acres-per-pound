"""Ranking views + static site builder (GitHub Pages)."""

import json
import pathlib
import shutil

from . import alerts

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent

_PUB_FIELDS = (
    "rm_id", "url", "address", "postcode", "lat", "lng", "price", "price_text",
    "beds", "subtype", "type_full", "land_only", "acres_min", "acres_max",
    "acres_mid", "acre_unit", "confidence", "matched", "listing_status",
    "first_published", "first_seen", "last_seen", "gbp_per_acre", "acres_per_100k",
    "region_id", "region_name", "region_median", "value_ratio",
    "est_acres", "est_plot_m2", "inspire_id", "est_shared",
)


def _pub(row):
    return {k: row.get(k) for k in _PUB_FIELDS}


def _state_row(row):
    # minimal persisted row (keeps state.json small + git-diffable)
    keep = (
        "rm_id", "url", "address", "postcode", "lat", "lng", "price", "price_text",
        "beds", "subtype", "land_only", "acres_min", "acres_max", "acres_mid",
        "acre_unit", "confidence", "matched", "first_seen", "last_seen",
        "listing_status", "first_published", "active", "detail_checked",
        "region_id", "region_name", "est_acres", "est_plot_m2", "inspire_id",
        "est_shared", "est_checked",
    )
    return {k: v for k in keep if (v := row.get(k)) is not None}


def excluded_subtypes(cfg):
    return set(cfg.get("search", {}).get("exclude_subtypes") or [])


def ranking(listings, cfg=None):
    excl = excluded_subtypes(cfg or load_config()) if cfg else set()
    out = []
    for r in listings.values():
        row = r
        if r.get("acres_mid") is None and r.get("est_acres"):
            row = dict(r)
            row["acres_min"] = row["acres_max"] = row["acres_mid"] = row["est_acres"]
            row["confidence"] = "est"
            row["matched"] = "registered plot boundary" + (" (shared site)" if r.get("est_shared") else "")
        if row.get("gbp_per_acre") is None and (row.get("price") or 0) >= 1000 and row.get("acres_mid"):
            row["gbp_per_acre"] = round(row["price"] / row["acres_mid"], 2)
        if row.get("acres_per_100k") is None and (row.get("price") or 0) >= 1000 and row.get("acres_mid"):
            row["acres_per_100k"] = round(row["acres_mid"] / (row["price"] / 100000), 3)
        if (row.get("active") is not False
                and row.get("gbp_per_acre") is not None
                and (row.get("subtype") or "") not in excl
                and not row.get("est_shared")):
            out.append(row)
    out.sort(key=lambda r: r["gbp_per_acre"])
    return out


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _annotate(rows):
    by_reg = {}
    for r in rows:
        by_reg.setdefault(r.get("region_name") or "", []).append(r["gbp_per_acre"])
    for r in rows:
        med = _median(by_reg.get(r.get("region_name") or ""))
        if med:
            r["region_median"] = round(med, 2)
            r["value_ratio"] = round(med / r["gbp_per_acre"], 2) if r["gbp_per_acre"] else None
    return rows


def views(state, cfg=None):
    rows = ranking(state["listings"], cfg)
    land = _annotate([r for r in rows if r.get("land_only")])
    houses = _annotate([r for r in rows if not r.get("land_only")])
    return {
        "ts": state["ts"],
        "stats": state["stats"],
        "land": [_pub(r) for r in land],
        "houses": [_pub(r) for r in houses],
        "all": [_pub(r) for r in rows],
        "events": state.get("events")[-200:] if state.get("events") else [],
    }


def write_state(state, snapshots_dir, events_path, new_events):
    base = pathlib.Path(snapshots_dir)
    if not base.is_absolute():
        base = REPO_DIR / base
    base.mkdir(parents=True, exist_ok=True)
    slim = {
        "ts": state["ts"],
        "stats": state["stats"],
        "listings": {k: _state_row(r) for k, r in sorted(state["listings"].items())},
    }
    (base / "state.json").write_text(
        json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if new_events:
        with open(pathlib.Path(events_path), "a", encoding="utf-8") as f:
            for e in new_events:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_site(state, cfg, site_dir="docs", new_events=()):
    site = pathlib.Path(site_dir)
    if not site.is_absolute():
        site = REPO_DIR / site
    site.mkdir(parents=True, exist_ok=True)
    payload = views(state, cfg)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(STATIC_DIR / name, site / name)
    rows = alerts._top_new(state["listings"], new_events, cfg, 10)
    alerts.console_banner(rows)
    alerts.notify(rows, cfg)
    return payload
