import argparse
import logging
import pathlib
import sys

from . import regions as regions_mod
from .engine import cycle, load_state, persist
from .http import Fetcher, load_config
from .publish import build_site, write_state
from . import store as store_mod

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent


def _source_enabled(cfg, name):
    return bool((cfg.get("sources", {}).get(name) or {}).get("enabled", False))


def _filter_args(args):
    return dict(
        enabled=[w.strip() for w in args.regions.split(",") if w.strip()] if getattr(args, "regions", None) else None,
        excluded=[w.strip() for w in args.exclude_regions.split(",") if w.strip()] if getattr(args, "exclude_regions", None) else None,
        countries=[w.strip() for w in args.country.split(",") if w.strip()] if getattr(args, "country", None) else None,
        exclude_countries=[w.strip() for w in args.exclude_country.split(",") if w.strip()] if getattr(args, "exclude_country", None) else None,
        exclude_highlands=getattr(args, "exclude_highlands", False),
    )


def _enabled_regions_by_source(cfg, args, verbose=False):
    """Return {source: [regions]} after config + CLI filters."""
    filters = _filter_args(args)
    out = {}
    if _source_enabled(cfg, "rightmove"):
        regs = regions_mod.load_regions()
        regs = regions_mod.filter_regions(regs, cfg=cfg, **filters)
        if regs:
            out["rightmove"] = regs
    if _source_enabled(cfg, "onthemarket"):
        regs = regions_mod.load_otm_regions()
        regs = regions_mod.filter_regions(regs, cfg=cfg, **filters)
        if regs:
            out["onthemarket"] = regs
    if getattr(args, "limit", None):
        out = {src: regs[: args.limit] for src, regs in out.items()}
    total = sum(len(v) for v in out.values())
    if total == 0:
        print("no regions available - run 'python -m acres_per_pound.cli regions' "
              "(and 'regions --otm') first, or check sources.*.enabled in config.json")
        sys.exit(1)
    if verbose:
        print("regions: " + ", ".join(f"{k}={len(v)}" for k, v in out.items()))
    return out


def _store(cfg):
    p = cfg.get("store_path") or "data/apf.db"
    return store_mod.connect(REPO_DIR / p)


def _snapshots_dir(cfg):
    snaps = pathlib.Path(cfg["snapshots_dir"])
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    return snaps


def cmd_regions(args):
    fetcher = Fetcher()
    if getattr(args, "otm", False):
        regions_mod.discover_otm(fetcher, verbose=True)
        print(f"saved {len(regions_mod.load_otm_regions())} OTM regions to data/regions_otm.json")
        return
    regions_mod.discover(fetcher, limit=args.limit, verbose=True)
    print(f"saved {len(regions_mod.load_regions())} regions to data/regions.json")


def cmd_run_once(args):
    cfg = load_config()
    fetcher = Fetcher(cfg)
    by_source = _enabled_regions_by_source(cfg, args, verbose=True)
    snaps = _snapshots_dir(cfg)
    prev = load_state(snaps / "state.json")
    print(f"scanning {sum(len(v) for v in by_source.values())} regions "
          f"(max price {cfg['search'].get('max_price') or 'unlimited'})")
    state = cycle(fetcher, cfg, by_source, prev_state=prev, verbose=True, force_full=True)
    conn = _store(cfg)
    persist(conn, state, cfg=cfg, verbose=True)
    print(f"listings: {state['stats']['listings']}, with land: {state['stats']['with_land']}, "
          f"land-only: {state['stats']['land_only']}")
    if state["stats"]["truncated_regions"]:
        print(f"WARNING truncated regions: {state['stats']['truncated_regions']}")
    print(f"events: {len(state['events'])} (new/reduced/increased/removed)")
    excl = set(cfg.get("search", {}).get("exclude_subtypes") or [])
    rows = [r for r in state["listings"].values()
            if r.get("active") is not False and r.get("gbp_per_acre") is not None
            and (r.get("subtype") or "") not in excl]
    rows.sort(key=lambda r: r["gbp_per_acre"])
    print("\n=== TOP 25 GBP PER ACRE ===")
    for r in rows[:25]:
        tag = "LAND" if r.get("land_only") else "house"
        print(f"  {r['gbp_per_acre']:>11,.0f} GBP/ac  {r['acres_mid']:>7} ac  "
              f"{r['price_text']:>15}  [{tag}] {r['address'][:50]}")
        print(f"             {r['url']}")
    write_state(state, snaps, snaps / "events.jsonl", state["events"])


def cmd_publish(args):
    cfg = load_config()
    fetcher = Fetcher(cfg)
    by_source = _enabled_regions_by_source(cfg, args)
    print(f"scanning {sum(len(v) for v in by_source.values())} regions "
          f"(max price {cfg['search'].get('max_price') or 'unlimited'})")
    snaps = _snapshots_dir(cfg)
    prev = load_state(snaps / "state.json")
    prev_n = len((prev or {}).get("listings") or {})
    state = cycle(fetcher, cfg, by_source, prev_state=prev, verbose=args.verbose)
    conn = _store(cfg)
    persist(conn, state, cfg=cfg, verbose=args.verbose)
    # 200k+ rows are in memory twice at this point (scan state + store
    # reload below); drop the scan copy before reloading so CI runners
    # with 7 GB stay comfortable.
    state["listings"] = {}
    # The site is built from the store, not the in-memory scan rows:
    # persistence recomputes acres_eff, shared titles and the metrics, and
    # the site must show exactly what the API and D1 will serve.
    site_state = {
        "ts": state["ts"],
        "stats": dict(state["stats"]),
        "listings": store_mod.listings_map(conn, active_only=False),
        "events": state["events"],
    }
    counts = store_mod.counts(conn)
    site_state["stats"].update({
        "listings": counts["total"],
        "active": counts["active"],
        "with_land": counts["with_acre_metric"],
    })
    write_state(site_state, snaps, snaps / "events.jsonl", state["events"])
    payload = build_site(site_state, cfg, site_dir=cfg["site_dir"], new_events=state["events"])
    print(f"prev {prev_n} -> now {site_state['stats']['listings']} listings, "
          f"with land {site_state['stats']['with_land']}, land-only {site_state['stats']['land_only']}")
    print(f"events {len(state['events'])}, site rows: land {len(payload['land'])}, "
          f"houses {len(payload['houses'])}")
    if state["stats"]["truncated_regions"]:
        print(f"WARNING truncated regions: {state['stats']['truncated_regions']}")


def cmd_scrape_region(args):
    import json as _json

    from . import otm, rightmove

    cfg = load_config()
    fetcher = Fetcher(cfg)
    if args.source == "onthemarket":
        rows, truncated = otm.scan_region(
            fetcher, {"slug": args.id, "name": args.id}, cfg, verbose=True)
        keys = ("uid", "price", "subtype", "address", "lat", "lng")
    else:
        rows, truncated = rightmove.scan_region(
            fetcher, {"id": int(args.id), "name": args.id}, cfg, verbose=True)
        keys = ("uid", "price", "subtype", "address", "lat", "lng")
    print(f"{args.source} {args.id}: {len(rows)} listings, truncated={truncated}")
    for r in rows[:5]:
        print(_json.dumps({k: r.get(k) for k in keys}, ensure_ascii=False))


def cmd_serve(args):
    import uvicorn

    cfg = load_config()
    web_dist = REPO_DIR / "web" / "dist"
    if not getattr(args, "quick", False):
        fetcher = Fetcher(cfg)
        by_source = _enabled_regions_by_source(cfg, args)
        snaps = _snapshots_dir(cfg)
        prev = load_state(snaps / "state.json")
        state = cycle(fetcher, cfg, by_source, prev_state=prev, verbose=True)
        conn = _store(cfg)
        persist(conn, state, cfg=cfg)
        write_state(state, snaps, snaps / "events.jsonl", state["events"])
        build_site(state, cfg, site_dir=cfg["site_dir"], new_events=state["events"])

    host = cfg["server"]["host"]
    port = cfg["server"]["port"]

    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from . import api as api_mod

    conn = _store(cfg)
    app = FastAPI(title="acres per pound")
    api_mod.register(app, conn)

    if (web_dist / "index.html").exists():
        print(f"\napp: http://{host}:{port}  (frontend: web/dist)")
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="site")
    else:
        site_dir = REPO_DIR / cfg["site_dir"]
        if not (site_dir / "index.html").exists():
            raise SystemExit("No frontend build. Run: cd web && npm install && npm run build")
        print(f"\nlegacy dashboard: http://{host}:{port}  (frontend: docs/)")
        for name in ("index.html", "app.js", "style.css", "data.json", "houses.json", "layers.json"):
            @app.get(f"/{name}")
            def _f(name=name):
                return FileResponse(site_dir / name)
        @app.get("/")
        def _root():
            return FileResponse(site_dir / "index.html")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_enrich(args):
    """Match listings to registered plot boundaries (INSPIRE, E&W only).

    Downloads and parses HM Land Registry INSPIRE polygon zips, then
    point-matches every listing's coordinates to its plot. The matched
    title boundary is stored simplified in WGS84 (plot_geom) so the app
    can draw it; the area lands on the row as plot_sqm/est_acres.

    `--refresh` re-downloads the local authority zips and retries every
    listing that has no polygon, so newly registered titles are picked
    up. Without it the cached polygon sets are reused.
    """
    from . import inspire

    cfg = load_config()
    conn = _store(cfg)

    if args.las:
        names = [n.strip() for n in args.las.split(",") if n.strip()]
        if names and not names[0].endswith(".zip"):
            names = [n if n.endswith(".zip") else n + ".zip" for n in names]
    elif args.all:
        names = inspire.list_las()
        print(f"{len(names)} local authorities in E&W dataset")
    else:
        print("use --all (full E&W, ~4GB download) or --las A.zip,B.zip")
        return

    retry = bool(getattr(args, "refresh", False))
    if retry:
        n = conn.execute(
            "UPDATE listings SET est_checked=0 WHERE active=1 AND plot_source IS NULL"
        ).rowcount
        conn.commit()
        print(f"refresh: {n:,} unmatched listings queued for retry")

    by_id = {uid: row for uid, row in store_mod.listings_map(conn).items()
             if row.get("lat") and row.get("lng")
             and row.get("country") in (None, "England", "Wales")}
    print(f"{len(by_id)} listings with coordinates (E&W)")

    matched_total = 0
    attempted_total = 0
    for name in names:
        try:
            pkl = inspire.build_index(name, verbose=True, force=retry)
        except Exception as e:
            print(f"  {name}: download/parse failed: {e}")
            continue
        if pkl is None:
            print(f"  {name}: no polygons (skipped)")
            continue
        try:
            bbox = inspire.bbox_of(pkl)
            pts = []
            for uid, row in by_id.items():
                if row.get("est_checked") or row.get("plot_source") == "inspire":
                    continue
                e, n = inspire.wgs84_to_bng(row["lat"], row["lng"])
                if bbox[0] <= e <= bbox[2] and bbox[1] <= n <= bbox[3]:
                    pts.append((uid, e, n))
            if not pts:
                continue
            hits = inspire.match_points(pts, bbox, pkl)
            hit_ids = set(hits)
            updates = []
            geoms = {}
            for uid, (area, gid, poly) in hits.items():
                acres = round(area / 4046.86, 3)
                updates.append((uid, {
                    "plot_sqm": round(area, 1),
                    "plot_sqft": round(area * 10.7639, 0),
                    "plot_source": "inspire",
                    "plot_id": gid,
                    "plot_conf": "registered",
                    "est_plot_m2": round(area, 1),
                    "est_acres": acres,
                    "inspire_id": gid,
                    "est_checked": 1,
                }))
                geoms.setdefault(gid, (area, poly))
            for uid, _, _ in pts:
                if uid not in hit_ids:
                    updates.append((uid, {"est_checked": 1}))
            store_mod.update_many(conn, updates)
            stored = store_mod.save_plot_geoms(conn, [
                (gid, inspire.polygon_geojson(poly), area / 4046.86, "inspire")
                for gid, (area, poly) in geoms.items()])
            matched_total += len(hits)
            attempted_total += len(pts)
            print(f"  {name}: {len(hits)}/{len(pts)} matched, {stored} boundaries stored")
        except Exception as e:
            print(f"  {name}: matching failed: {e}")
            continue
    print(f"total: {matched_total}/{attempted_total} matched to registered plots")
    store_mod.recompute_metrics(conn, verbose=True)
    print(f"store: {store_mod.counts(conn)}")


def cmd_backfill_geoms(args):
    """Store simplified boundaries for listings already matched to a plot.

    Matching happened before geometry was stored, so this re-runs the
    point-in-polygon lookup for those rows and nothing else.
    """
    from . import inspire

    cfg = load_config()
    conn = _store(cfg)
    names = inspire.cached_names()
    if not names:
        print("no cached polygon sets - run: acres enrich --all")
        return

    rows = conn.execute("""
        SELECT l.uid, l.lat, l.lng FROM listings l
        LEFT JOIN plot_geom g ON g.plot_id = l.plot_id
        WHERE l.plot_id IS NOT NULL AND l.active=1 AND l.excluded=0
          AND l.lat IS NOT NULL AND l.lng IS NOT NULL AND g.plot_id IS NULL
    """).fetchall()
    print(f"{len(rows):,} matched listings without a stored boundary")
    if not rows:
        return

    bb = inspire.bboxes_map(names)
    by_la = {}
    for uid, lat, lng in rows:
        e, n = inspire.wgs84_to_bng(lat, lng)
        for name in names:
            b = bb.get(name)
            if b and b[0] <= e <= b[2] and b[1] <= n <= b[3]:
                by_la.setdefault(name, []).append((uid, e, n))
                break

    stored = 0
    for name, pts in sorted(by_la.items(), key=lambda kv: -len(kv[1])):
        pkl = inspire.build_index(name, verbose=False)
        if pkl is None:
            continue
        hits = inspire.match_points(pts, bb[name], pkl)
        geoms = {}
        for _uid, (area, gid, poly) in hits.items():
            geoms.setdefault(gid, (area, poly))
        stored += store_mod.save_plot_geoms(conn, [
            (gid, inspire.polygon_geojson(poly), area / 4046.86, "inspire")
            for gid, (area, poly) in geoms.items()])
        print(f"  {name}: {len(hits)}/{len(pts)} matched, "
              f"{len(geoms)} boundaries ({stored:,} total)")
    print(f"stored {stored:,} boundaries")
    print(f"store: {store_mod.counts(conn)}")


def cmd_sold(args):
    """Match removed listings against Land Registry Price Paid data."""
    from . import sold as sold_mod

    cfg = load_config()
    conn = _store(cfg)
    listings = store_mod.listings_map(conn, active_only=False)
    years = tuple(int(y) for y in (args.years or "2026,2025").split(","))
    ppd = sold_mod.fetch_ppd(years=years, verbose=True)
    print(f"{len(ppd)} PPD records loaded")
    matched, attempted = sold_mod.match_removed(listings, ppd, verbose=True)
    print(f"matched {matched}/{attempted} removed listings to sold prices")
    updates = [(uid, {k: row[k] for k in
                      ("sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence")
                      if row.get(k) is not None})
               for uid, row in listings.items() if row.get("sold_price")]
    store_mod.update_many(conn, updates)
    print("store updated")


def cmd_layers(args):
    """Build family-suitability map layers.

    airports (OurAirports) + crime heatmap (data.police.uk, ~1.7GB
    one-time) + GP surgeries (CQC directory + postcodes.io) + flood
    zone 3 (Environment Agency) + parks (OpenStreetMap GB, ~1.4GB
    one-time) + schools (GIAS CSV if present - see README).

    Run monthly; every source is cached.
    """
    from . import layers as layers_mod

    payload = layers_mod.build_all_layers(verbose=True)
    print(f"layers.json: airports {len(payload['airports'])} | crime {len(payload['crimes'])} | "
          f"gps {len(payload['gps'])} | flood {len(payload['flood'])} | parks {len(payload['parks'])} | "
          f"schools {'n/a (see README)' if payload['schools'] is None else len(payload['schools'])}")


def cmd_zoopla(args):
    """Scan Zoopla locally via a real browser (Cloudflare blocks CI IPs).

    Run on a home connection: python -m acres_per_pound.cli zoopla --limit 5
    """
    import json as _json

    from . import zoopla

    cfg = load_config()
    conn = _store(cfg)
    fetcher = zoopla.BrowserFetcher(cfg, headless=not args.headful)
    try:
        if args.discover:
            zoopla.discover(fetcher, slugs=None, verbose=True)
            print("saved data/regions_zoopla.json")
            return
        p = REPO_DIR / "data" / "regions_zoopla.json"
        regions = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        if not regions:
            print("no Zoopla regions yet - run 'acres zoopla --discover' first")
            return
        if args.limit:
            regions = regions[: args.limit]
        total = 0
        for i, region in enumerate(regions, 1):
            try:
                rows, truncated = zoopla.scan_region(fetcher, region, cfg, verbose=args.verbose)
            except Exception as e:
                print(f"  {region.get('slug')}: failed: {e}")
                continue
            if rows:
                store_mod.upsert_many(conn, rows)
                total += len(rows)
            print(f"  {i}/{len(regions)} {region.get('slug')}: {len(rows)} listings"
                  f"{' (truncated)' if truncated else ''}")
        store_mod.mark_excluded(conn, cfg.get("search", {}).get("exclude_subtypes"))
        store_mod.backfill_sqft(conn)
        store_mod.annotate_regions(conn)
        store_mod.recompute_metrics(conn, verbose=True)
        print(f"zoopla: {total} listings upserted; {store_mod.counts(conn)}")
    finally:
        fetcher.close()


def cmd_sync(args):
    """Publish the store to Cloudflare (D1 search index + R2 snapshot)."""
    from . import sync as sync_mod

    cfg = load_config()
    conn = _store(cfg)
    if args.full:
        store_mod.backfill_sqft(conn)
        store_mod.annotate_regions(conn)
        store_mod.recompute_metrics(conn)
    if args.d1 or args.all:
        sync_mod.sync_d1(conn, verbose=True, full=args.full)
        sync_mod.sync_plot_geoms(conn, verbose=True, full=args.full)
        sync_mod.sync_regions(conn, verbose=True)
    if args.r2 or args.all:
        sync_mod.sync_r2_snapshot(conn, verbose=True)


def cmd_ros(args):
    """Match Scottish listings to Registers of Scotland cadastral parcels."""
    from . import ros as ros_mod

    cfg = load_config()
    if args.list:
        for code, name in sorted(ros_mod.COUNTIES.items(), key=lambda x: x[1]):
            print(f"  {code}  {name}")
        return
    codes = None
    if args.all:
        codes = list(ros_mod.COUNTIES)
    elif args.codes:
        codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    if not codes:
        print("use --all (33 counties, ~1GB) or --codes ABN,ANG,...")
        return
    conn = _store(cfg)
    matched = ros_mod.enrich_scotland(conn, codes=codes, verbose=True)
    print(f"matched {matched:,} Scottish listings to RoS parcels")
    print(store_mod.counts(conn))


def cmd_reparse(args):
    """Re-read acreage from detail pages with the current parser rules.

    Needed after a parser change (e.g. new communal/estate-copy patterns)
    because descriptions are not stored - only the extracted figures are.
    """
    import json as _json

    from . import otm, rightmove

    cfg = load_config()
    conn = _store(cfg)
    fetcher = Fetcher(cfg)
    q = """SELECT uid, source, url, address, acres_mid, acres_min, acres_max,
                  acre_unit, confidence, matched, communal, summary, features_json
           FROM listings WHERE active=1 AND excluded=0 AND acres_mid IS NOT NULL
             AND source IN ('rightmove','onthemarket')"""
    last = store_mod.get_meta(conn, "reparse_last_uid")
    if last and not args.restart:
        q += f" AND uid > '{last}'"
    q += " ORDER BY uid"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = conn.execute(q).fetchall()
    print(f"re-parsing acreage for {len(rows):,} listings"
          f"{' (resuming after ' + str(last) + ')' if last and not args.restart else ''}")

    modules = {"rightmove": rightmove, "onthemarket": otm}
    changed = 0
    cleared = 0
    updates = []
    for i, raw in enumerate(rows, 1):
        row = dict(raw)
        try:
            features = _json.loads(row.get("features_json") or "[]")
        except Exception:
            features = []
        listing = {
            "uid": row["uid"], "url": row["url"], "address": row["address"],
            "summary": row.get("summary") or "", "features": features,
            "subtype": "", "rm_id": row["uid"].split(":")[1],
        }
        module = modules.get(row["source"])
        if module is None:
            continue
        parsed = module._fetch_and_parse(fetcher, listing, verbose=False)
        if not parsed:
            continue
        a_min, a_max, a_mid, unit, conf, matched, cands, communal = parsed
        before = (row.get("acres_mid"), bool(row.get("communal")))
        after = (a_mid, bool(communal))
        if before == after:
            continue
        changed += 1
        if a_mid is None:
            cleared += 1
        updates.append((row["uid"], {
            "acres_min": a_min, "acres_max": a_max, "acres_mid": a_mid,
            "acre_unit": unit, "confidence": conf, "matched": matched,
            "communal": 1 if communal else 0,
        }))
        if len(updates) >= 200:
            store_mod.update_many(conn, updates)
            updates = []
        if args.verbose and changed <= 20:
            print(f"  {str(row['address'])[:44]:46} {before[0]} -> {a_mid} ac"
                  f"{' (communal)' if communal else ''}")
        store_mod.set_meta(conn, "reparse_last_uid", row["uid"])
        if i % 100 == 0:
            print(f"  ... {i:,}/{len(rows):,} checked, {changed:,} changed")
    if updates:
        store_mod.update_many(conn, updates)
    store_mod.set_meta(conn, "reparse_last_uid", rows[-1]["uid"] if rows else None)
    print(f"re-parsed {len(rows):,}: {changed:,} changed, {cleared:,} acreage removed")
    store_mod.backfill_sqft(conn)
    store_mod.annotate_regions(conn)
    store_mod.recompute_metrics(conn, verbose=True)
    print(store_mod.counts(conn))


def cmd_geocode(args):
    """Fill full postcodes for listings via postcodes.io bulk reverse geocoding."""
    from . import geocode as geocode_mod

    cfg = load_config()
    conn = _store(cfg)
    n = geocode_mod.fill_postcodes(conn, limit=args.limit or None, verbose=True)
    print(f"filled {n} postcodes")


def cmd_store(args):
    """Canonical SQLite store: init, migrate the legacy state.json, stats."""
    cfg = load_config()
    conn = _store(cfg)
    if args.import_state:
        n = store_mod.import_state_json(conn, verbose=True)
        store_mod.backfill_sqft(conn)
        store_mod.annotate_regions(conn, verbose=True)
        store_mod.recompute_metrics(conn)
        print(f"imported {n} listings; {store_mod.counts(conn)}")
    elif args.recompute:
        store_mod.mark_excluded(conn, cfg.get("search", {}).get("exclude_subtypes"), verbose=True)
        store_mod.backfill_sqft(conn)
        store_mod.annotate_regions(conn, verbose=True)
        store_mod.recompute_metrics(conn, verbose=True)
    else:
        print(store_mod.counts(conn))
    if args.export_state:
        from .publish import export_state
        export_state(conn, verbose=True)


def cmd_epc(args):
    """EPC enrichment: internal floor area for E&W + Scotland."""
    from . import epc as epc_mod

    cfg = load_config()
    conn = _store(cfg)
    if args.download:
        epc_mod.download_bulk(cfg, which=args.download, verbose=True)
    if args.load:
        have_uprn = conn.execute("SELECT COUNT(*) FROM uprn").fetchone()[0]
        if not have_uprn and (epc_mod.EPC_DIR / "uprn").exists():
            n = epc_mod.load_uprn_index(conn, verbose=True)
            print(f"uprn rows in store: {n}")
        elif have_uprn:
            print(f"uprn index already loaded ({have_uprn:,} rows)")
        n = epc_mod.load_index(conn, verbose=True)
        print(f"epc rows in store: {n}")
    if args.match:
        matched = epc_mod.match_listings(conn, verbose=True)
        store_mod.backfill_sqft(conn)
        store_mod.recompute_metrics(conn)
        print(f"matched {matched} listings to EPC floor areas")
    if args.stats:
        print(epc_mod.stats(conn))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="acres")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_region_filter_args(parser):
        parser.add_argument("--regions", default="", help="comma-separated region slugs/names/ids to include")
        parser.add_argument("--exclude-regions", default="", help="comma-separated region slugs/names to exclude")
        parser.add_argument("--country", default="", help="comma-separated countries to include (England, Wales, Scotland)")
        parser.add_argument("--exclude-country", default="", help="comma-separated countries to exclude (e.g. Scotland)")
        parser.add_argument("--exclude-highlands", action="store_true", help="exclude Scottish Highlands, Islands, and Far North")
        parser.add_argument("--limit", type=int, default=0)

    sp = sub.add_parser("regions", help="discover UK region ids from Rightmove sitemaps (--otm for OnTheMarket)")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--otm", action="store_true", help="discover OnTheMarket regions instead")
    sp.set_defaults(func=cmd_regions)

    sp = sub.add_parser("run-once", help="scan all regions once and print the ranking")
    add_region_filter_args(sp)
    sp.set_defaults(func=cmd_run_once)

    sp = sub.add_parser("publish", help="scan + write snapshots + build static site (GitHub Actions)")
    add_region_filter_args(sp)
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("scrape-region", help="debug: scan one region")
    sp.add_argument("id")
    sp.add_argument("--source", default="rightmove", choices=["rightmove", "onthemarket"])
    sp.set_defaults(func=cmd_scrape_region)

    sp = sub.add_parser("enrich", help="match listings to registered plot boundaries (INSPIRE)")
    sp.add_argument("--all", action="store_true", help="download+parse every E&W local authority (~4GB)")
    sp.add_argument("--las", default="", help="comma-separated LA zip names")
    sp.add_argument("--refresh", action="store_true",
                    help="re-download the polygon sets and retry unmatched listings (monthly)")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("geoms", help="store boundaries for already-matched plots (one-off backfill)")
    sp.set_defaults(func=cmd_backfill_geoms)

    sp = sub.add_parser("epc", help="EPC enrichment: internal floor area (England, Wales, Scotland)")
    sp.add_argument("--download", default="", choices=["", "all", "ew", "scotland", "uprn"],
                    help="download bulk EPC/UPRN data (ew = England & Wales, scotland = Scottish extract, uprn = OS Open UPRN)")
    sp.add_argument("--load", action="store_true", help="parse downloaded EPC files into the store index")
    sp.add_argument("--match", action="store_true", help="match listings to EPC certificates")
    sp.add_argument("--stats", action="store_true", help="show EPC coverage")
    sp.set_defaults(func=cmd_epc)

    sp = sub.add_parser("sold", help="match removed listings to Land Registry sold prices")
    sp.add_argument("--years", default="2026,2025", help="comma-separated PPD years")
    sp.set_defaults(func=cmd_sold)

    sp = sub.add_parser("layers", help="build map layers (airports, crime heatmap)")
    sp.set_defaults(func=cmd_layers)

    sp = sub.add_parser("store", help="canonical store: import legacy state, stats, recompute metrics")
    sp.add_argument("--import-state", action="store_true", help="import snapshots/state.json (one-off migration)")
    sp.add_argument("--recompute", action="store_true", help="recompute derived £/sqft + £/acre metrics")
    sp.add_argument("--export-state", action="store_true",
                    help="write the store back to snapshots/state.json (carries enrichment into CI)")
    sp.set_defaults(func=cmd_store)

    sp = sub.add_parser("geocode", help="fill full postcodes from lat/lng via postcodes.io")
    sp.add_argument("--limit", type=int, default=0, help="max listings to process (debug)")
    sp.set_defaults(func=cmd_geocode)

    sp = sub.add_parser("reparse", help="re-read acreage from detail pages (after a parser change)")
    sp.add_argument("--limit", type=int, default=0, help="max listings per run (resumable)")
    sp.add_argument("--restart", action="store_true", help="ignore the resume checkpoint")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_reparse)

    sp = sub.add_parser("ros", help="match Scottish listings to RoS cadastral parcels (plot areas)")
    sp.add_argument("--all", action="store_true", help="all 33 registration counties (~1GB download)")
    sp.add_argument("--codes", default="", help="comma-separated county codes (ABN,ANG,...)")
    sp.add_argument("--list", action="store_true", help="list county codes")
    sp.set_defaults(func=cmd_ros)

    sp = sub.add_parser("sync", help="publish the store to Cloudflare (D1 + R2)")
    sp.add_argument("--d1", action="store_true", help="sync the search index to D1")
    sp.add_argument("--r2", action="store_true", help="upload the full snapshot to R2")
    sp.add_argument("--all", action="store_true", help="both")
    sp.add_argument("--full", action="store_true", help="recompute metrics and re-sync every row")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("zoopla", help="scan Zoopla locally via a real browser (home IP)")
    sp.add_argument("--discover", action="store_true", help="probe region slugs first")
    sp.add_argument("--limit", type=int, default=0, help="max regions to scan")
    sp.add_argument("--headful", action="store_true", help="show the browser window")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_zoopla)

    sp = sub.add_parser("serve", help="scan once then serve the dashboard locally")
    add_region_filter_args(sp)
    sp.add_argument("--quick", action="store_true",
                    help="skip the scan and serve the last generated site immediately")
    sp.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
