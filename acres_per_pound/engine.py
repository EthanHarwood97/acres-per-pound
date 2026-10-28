"""Orchestration: scan -> enrich -> diff -> score -> events.

Multi-source: each source module exposes scan_region()/enrich() with the
same shape (rightmove, otm). Listings are keyed by uid
("<source>:<source_id>") so the same store serves every portal.
"""

import datetime
import hashlib
import json
import logging
import pathlib
import re

from . import otm, rightmove
from . import store as store_mod
from .landparse import parse_acres

log = logging.getLogger("acres")

SLUG_RE = re.compile(r"[^a-z0-9-]+")

SOURCES = {"rightmove": rightmove, "onthemarket": otm}

# Fields produced by enrichment that must survive re-scans of the search
# pages (which only carry search-level data).
_KEEP_FIELDS = (
    "acres_min", "acres_max", "acres_mid", "acre_unit", "confidence",
    "matched", "candidates", "detail_checked", "communal",
    "internal_sqm", "internal_sqft", "internal_source", "internal_conf",
    "epc_rating", "epc_date", "uprn",
    "plot_sqm", "plot_sqft", "plot_source", "plot_id", "plot_conf",
    "est_acres", "est_plot_m2", "inspire_id", "est_shared", "est_checked",
    "sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence",
)


def load_state(path):
    """Read the scan state. Accepts a .json path or its .json.gz sibling.

    The state is ~110 MB of JSON at current corpus size, which is over
    GitHub's 100 MB file limit, so it is stored gzipped (~15 MB) and
    committed by CI. Plain .json still loads for older checkouts.
    """
    p = pathlib.Path(path)
    candidates = [p, pathlib.Path(str(p) + ".gz")]
    existing = [c for c in candidates if c.exists() and c.stat().st_size > 0]
    if not existing:
        return {}
    p = max(existing, key=lambda c: c.stat().st_mtime)
    try:
        if p.suffix == ".gz":
            import gzip
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path, state):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    if p.suffix == ".gz":
        import gzip
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(state, f, ensure_ascii=False)
        tmp.replace(p)
        return
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _mode_for(region, cfg, day_number):
    """Deterministic shard: each region gets a full reconcile scan once
    every `full_cycle_days` days; other days it gets a cheap newest-first
    sweep. Hash is stable across processes (md5, not hash())."""
    days = int(cfg.get("search", {}).get("full_cycle_days", 7))
    bucket = int(hashlib.md5(str(region["slug"]).encode()).hexdigest(), 16) % days
    return "full" if day_number % days == bucket else "new"


def _source_of(row):
    return row.get("source") or "rightmove"


def _uid_of(rm_id, row):
    if row.get("uid"):
        return row["uid"]
    src = _source_of(row)
    sid = str(row.get("source_id") or row.get("rm_id") or rm_id)
    return f"{src}:{sid}"


def cycle(fetcher, cfg, regions_by_source, prev_state=None, verbose=False,
          force_full=False):
    """One scan cycle across all enabled sources.

    `regions_by_source` maps source name -> list of region dicts.
    Returns {"ts", "listings": {uid: row}, "events": [...], "stats": {...}}
    """
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    prev_raw = (prev_state or {}).get("listings") or {}
    prev = {}
    for key, row in prev_raw.items():
        prev[_uid_of(key, row)] = row
    cur = dict(prev)
    day = datetime.datetime.now(datetime.timezone.utc).timetuple().tm_yday
    stats = {"regions": 0, "listings": len(cur), "truncated_regions": [],
             "full_scans": 0, "new_sweeps": 0, "scanned": 0, "by_source": {}}
    removed = []

    for source_name, regions in (regions_by_source or {}).items():
        module = SOURCES.get(source_name)
        if module is None:
            log.warning("unknown source %s", source_name)
            continue
        stats["by_source"].setdefault(source_name, 0)
        for region in regions:
            mode = "full" if force_full else _mode_for(region, cfg, day)
            rid = region.get("id")
            prev_ids = {uid for uid, r in prev.items()
                        if r.get("region_id") == rid and _source_of(r) == source_name}
            rows, truncated = module.scan_region(
                fetcher, region, cfg, mode=mode, seen_ids=prev_ids, verbose=verbose)
            stats["regions"] += 1
            stats["scanned"] += len(rows)
            stats["by_source"][source_name] += len(rows)
            if mode == "full":
                stats["full_scans"] += 1
                seen_now = {r["uid"] for r in rows}
                for uid, old in prev.items():
                    if (old.get("region_id") == rid and _source_of(old) == source_name
                            and uid not in seen_now):
                        if old.get("active") is not False:
                            removed.append((uid, old))
            else:
                stats["new_sweeps"] += 1
            for row in rows:
                cur[row["uid"]] = row
            if truncated:
                stats["truncated_regions"].append(
                    f"{source_name}:{region.get('name') or region.get('id')}")
            if verbose:
                print(f"  [{source_name}] {region.get('name')} ({mode}): "
                      f"{len(rows)} seen, total {len(cur)}")

    # restore persistent fields on re-scanned rows
    for uid, row in cur.items():
        old = prev.get(uid)
        row["first_seen"] = (old or {}).get("first_seen") or ts
        row["last_seen"] = ts
        if old:
            for f in _KEEP_FIELDS:
                if old.get(f) is not None and row.get(f) is None:
                    row[f] = old[f]

    # diff -> events
    events = []
    for uid, row in cur.items():
        old = prev.get(uid)
        if old is None:
            events.append({"ts": ts, "event": "new", "uid": uid,
                           "rm_id": row.get("rm_id") or row.get("source_id"),
                           "source": _source_of(row),
                           "address": row.get("address")})
        else:
            old_price = old.get("price") or 0
            if row.get("price") and old_price and row["price"] != old_price:
                ev = "reduced" if row["price"] < old_price else "increased"
                events.append({"ts": ts, "event": ev, "uid": uid,
                               "rm_id": row.get("rm_id") or row.get("source_id"),
                               "source": _source_of(row),
                               "address": row.get("address"),
                               "price_old": old_price, "price_new": row["price"]})
    for uid, old in removed:
        if uid in cur:
            cur[uid]["active"] = False
        events.append({"ts": ts, "event": "removed", "uid": uid,
                       "rm_id": old.get("rm_id") or old.get("source_id"),
                       "source": _source_of(old), "address": old.get("address")})

    # acreage enrichment: only for listings seen this cycle, and only when
    # we don't already have a persisted figure (or a failed detail check)
    excl = set(cfg.get("search", {}).get("exclude_subtypes") or [])
    for uid, row in cur.items():
        if not row.get("last_seen") or row["last_seen"] != ts:
            continue
        if (row.get("subtype") or "") in excl:
            continue
        prev_row = prev.get(uid) or {}
        if row.get("acres_mid") is None and prev_row.get("detail_checked"):
            continue
        module = SOURCES.get(_source_of(row))
        if module is None:
            continue
        module.enrich(fetcher, row, cfg, prev_acres=prev_row.get("acres_mid"),
                      verbose=verbose)
        if row.get("acres_mid") is not None:
            prev_acres = prev_row.get("acres_mid")
            if prev_acres is not None and abs(row["acres_mid"] - prev_acres) / max(prev_acres, 1e-9) > 0.05:
                events.append({"ts": ts, "event": "acre_update", "uid": uid,
                               "rm_id": row.get("rm_id") or row.get("source_id"),
                               "source": _source_of(row),
                               "address": row.get("address"),
                               "acres_mid": row["acres_mid"]})
        row.pop("description", None)

    for ev in events:
        row = cur.get(ev.get("uid"))
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
    stats["active"] = sum(1 for r in cur.values() if r.get("active") is not False)
    return {"ts": ts, "listings": cur, "events": events, "stats": stats}


def persist(conn, state, cfg=None, verbose=False):
    """Write a cycle's listings into the canonical store."""
    rows = list(state["listings"].values())
    ins, upd = store_mod.upsert_many(conn, rows)
    if cfg:
        store_mod.mark_excluded(
            conn, cfg.get("search", {}).get("exclude_subtypes"))
        store_mod.annotate_regions(conn)
    store_mod.backfill_sqft(conn)
    store_mod.recompute_metrics(conn)
    if verbose:
        print(f"store: {ins} inserted, {upd} updated; {store_mod.counts(conn)}")
    return ins, upd
