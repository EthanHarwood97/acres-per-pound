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
    "region_id", "region_name",
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
        "region_id", "region_name",
    )
    return {k: v for k in keep if (v := row.get(k)) is not None}


def ranking(listings):
    rows = [r for r in listings.values()
            if r.get("active") is not False and r.get("gbp_per_acre") is not None]
    rows.sort(key=lambda r: r["gbp_per_acre"])
    return rows


def views(state):
    rows = ranking(state["listings"])
    land = [r for r in rows if r.get("land_only")]
    houses = [r for r in rows if not r.get("land_only")]
    return {
        "ts": state["ts"],
        "stats": state["stats"],
        "land": [_pub(r) for r in land],
        "houses": [_pub(r) for r in houses],
        "all": [_pub(r) for r in rows],
        "events": state["events"][-200:],
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
    payload = views(state)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(STATIC_DIR / name, site / name)
    rows = alerts._top_new(state["listings"], new_events, cfg, 10)
    alerts.console_banner(rows)
    alerts.notify(rows, cfg)
    return payload
