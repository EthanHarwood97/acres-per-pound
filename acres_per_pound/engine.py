"""Orchestration: scan -> enrich -> diff -> score -> events."""

import hashlib
import json
import logging
import pathlib
import re

from . import rightmove
from .landparse import parse_acres

log = logging.getLogger("acres")

SLUG_RE = re.compile(r"[^a-z0-9-]+")

_ACRE_FIELDS = ("acres_min", "acres_max", "acres_mid", "acre_unit", "confidence",
                "matched", "candidates", "detail_checked")


def load_state(path):
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path, state):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _mode_for(region, cfg, day_number):
    """Deterministic shard: each region gets a full reconcile scan once
    every `full_cycle_days` days; other days it gets a cheap newest-first
    sweep. Hash is stable across processes (md5, not hash())."""
    days = int(cfg.get("search", {}).get("full_cycle_days", 7))
    bucket = int(hashlib.md5(region["slug"].encode()).hexdigest(), 16) % days
    return "full" if day_number % days == bucket else "new"


def cycle(fetcher, cfg, regions, prev_state=None, verbose=False, force_full=False):
    """One scan cycle.

    Returns {"ts", "listings": {rm_id: row}, "events": [...], "stats": {...}}
    """
    import datetime

    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    prev = (prev_state or {}).get("listings") or {}
    cur = dict(prev)
    day = datetime.datetime.now(datetime.timezone.utc).timetuple().tm_yday
    stats = {"regions": 0, "listings": len(cur), "truncated_regions": [], "full_scans": 0,
             "new_sweeps": 0, "scanned": 0}
    removed = []

    for region in regions:
        mode = "full" if force_full else _mode_for(region, cfg, day)
        prev_ids = {rm_id for rm_id, r in prev.items()
                    if r.get("region_id") == region.get("id")}
        rows, truncated = rightmove.scan_region(
            fetcher, region, cfg, mode=mode, seen_ids=prev_ids, verbose=verbose)
        stats["regions"] += 1
        stats["scanned"] += len(rows)
        if mode == "full":
            stats["full_scans"] += 1
            seen_now = {r["rm_id"] for r in rows}
            for rm_id, old in prev.items():
                if old.get("region_id") == region.get("id") and rm_id not in seen_now:
                    if old.get("active") is not False:
                        removed.append((rm_id, old))
        else:
            stats["new_sweeps"] += 1
        for row in rows:
            cur[row["rm_id"]] = row
        if truncated:
            stats["truncated_regions"].append(region.get("name") or region.get("id"))
        if verbose:
            print(f"  REGION^{region.get('id')} {region.get('name')} [{mode}]: "
                  f"{len(rows)} seen, total {len(cur)}")

    # restore persistent fields on re-scanned rows
    for rm_id, row in cur.items():
        old = prev.get(rm_id)
        row["first_seen"] = (old or {}).get("first_seen") or ts
        row["last_seen"] = ts
        if old:
            for f in _ACRE_FIELDS:
                if old.get(f) is not None and row.get(f) is None:
                    row[f] = old[f]

    # diff -> events
    events = []
    for rm_id, row in cur.items():
        old = prev.get(rm_id)
        if old is None:
            events.append({"ts": ts, "event": "new", "rm_id": rm_id, "address": row["address"]})
        else:
            old_price = old.get("price") or 0
            if row.get("price") and old_price and row["price"] != old_price:
                ev = "reduced" if row["price"] < old_price else "increased"
                events.append({"ts": ts, "event": ev, "rm_id": rm_id, "address": row["address"],
                               "price_old": old_price, "price_new": row["price"]})
    for rm_id, old in removed:
        if rm_id in cur:
            cur[rm_id]["active"] = False
        events.append({"ts": ts, "event": "removed", "rm_id": rm_id, "address": old.get("address")})

    # acreage enrichment: only for listings seen this cycle, and only when
    # we don't already have a persisted figure (or a failed detail check)
    excl = set(cfg.get("search", {}).get("exclude_subtypes") or [])
    for rm_id, row in cur.items():
        if not row.get("last_seen") or row["last_seen"] != ts:
            continue
        if (row.get("subtype") or "") in excl:
            continue
        prev_row = prev.get(rm_id) or {}
        if row.get("acres_mid") is None and prev_row.get("detail_checked"):
            continue
        rightmove.enrich(fetcher, row, cfg, prev_acres=prev_row.get("acres_mid"), verbose=verbose)
        if row.get("acres_mid") is not None:
            prev_acres = prev_row.get("acres_mid")
            if prev_acres is not None and abs(row["acres_mid"] - prev_acres) / max(prev_acres, 1e-9) > 0.05:
                events.append({"ts": ts, "event": "acre_update", "rm_id": rm_id,
                               "address": row["address"], "acres_mid": row["acres_mid"]})
        row.pop("description", None)

    for ev in events:
        row = cur.get(ev["rm_id"])
        if row and ev["event"] in ("new", "reduced"):
            ev["acres_mid"] = row.get("acres_mid")
            ev["gbp_per_acre"] = row.get("gbp_per_acre")

    for row in cur.values():
        if row.get("active") is not False and (row.get("price") or 0) >= 1000 and row.get("acres_mid"):
            row["gbp_per_acre"] = round(row["price"] / row["acres_mid"], 2)
            row["acres_per_100k"] = round(row["acres_mid"] / (row["price"] / 100000), 3)
        else:
            row["gbp_per_acre"] = None
            row["acres_per_100k"] = None

    stats["listings"] = len(cur)
    stats["with_land"] = sum(1 for r in cur.values()
                             if r.get("active") is not False and r.get("acres_mid") is not None)
    stats["land_only"] = sum(1 for r in cur.values()
                             if r.get("active") is not False and r.get("acres_mid") is not None
                             and r.get("land_only"))
    return {"ts": ts, "listings": cur, "events": events, "stats": stats}
