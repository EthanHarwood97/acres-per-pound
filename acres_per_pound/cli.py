import argparse
import logging
import pathlib
import sys

from . import regions as regions_mod
from .engine import cycle, load_state
from .http import Fetcher, load_config
from .publish import build_site, write_state

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent


def _enabled_regions(cfg, args):
    regs = regions_mod.load_regions()
    if not regs:
        print("no regions discovered - run 'python -m acres_per_pound.cli regions' first")
        sys.exit(1)
    enabled = cfg.get("regions", {}).get("enabled") or []
    if enabled:
        regs = [r for r in regs if r["slug"] in enabled or r["name"] in enabled]
    if getattr(args, "regions", None):
        wanted = [w.strip() for w in args.regions.split(",") if w.strip()]
        regs = [r for r in regs if r["slug"] in wanted or r["name"] in wanted
                or str(r["id"]) in wanted]
    if getattr(args, "limit", None):
        regs = regs[: args.limit]
    return regs


def cmd_regions(args):
    fetcher = Fetcher()
    regions_mod.discover(fetcher, limit=args.limit, verbose=True)
    print(f"saved {len(regions_mod.load_regions())} regions to data/regions.json")


def cmd_run_once(args):
    cfg = load_config()
    fetcher = Fetcher(cfg)
    regs = _enabled_regions(cfg, args)
    snaps = pathlib.Path(cfg["snapshots_dir"])
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    prev = load_state(snaps / "state.json")
    print(f"scanning {len(regs)} regions (max price {cfg['search']['max_price']})")
    state = cycle(fetcher, cfg, regs, prev_state=prev, verbose=True, force_full=True)
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
    regs = _enabled_regions(cfg, args)
    print(f"scanning {len(regs)} regions (max price {cfg['search']['max_price']})")
    snaps = pathlib.Path(cfg["snapshots_dir"])
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    prev = load_state(snaps / "state.json")
    prev_n = len((prev or {}).get("listings") or {})
    state = cycle(fetcher, cfg, regs, prev_state=prev, verbose=args.verbose)
    write_state(state, snaps, snaps / "events.jsonl", state["events"])
    payload = build_site(state, cfg, site_dir=cfg["site_dir"], new_events=state["events"])
    print(f"prev {prev_n} -> now {state['stats']['listings']} listings, "
          f"with land {state['stats']['with_land']}, land-only {state['stats']['land_only']}")
    print(f"events {len(state['events'])}, site rows: land {len(payload['land'])}, "
          f"houses {len(payload['houses'])}")
    if state["stats"]["truncated_regions"]:
        print(f"WARNING truncated regions: {state['stats']['truncated_regions']}")


def cmd_scrape_region(args):
    import json as _json

    from . import rightmove

    cfg = load_config()
    fetcher = Fetcher(cfg)
    region = {"id": int(args.id), "name": args.id}
    rows, truncated = rightmove.scan_region(fetcher, region, cfg, verbose=True)
    print(f"REGION^{args.id}: {len(rows)} listings, truncated={truncated}")
    for r in rows[:5]:
        print(_json.dumps({k: r[k] for k in ("rm_id", "price", "subtype", "address", "lat", "lng")},
                          ensure_ascii=False))


def cmd_serve(args):
    import uvicorn

    cfg = load_config()
    fetcher = Fetcher(cfg)
    regs = _enabled_regions(cfg, args)
    snaps = pathlib.Path(cfg["snapshots_dir"])
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    prev = load_state(snaps / "state.json")
    state = cycle(fetcher, cfg, regs, prev_state=prev, verbose=True)
    write_state(state, snaps, snaps / "events.jsonl", state["events"])
    build_site(state, cfg, site_dir=cfg["site_dir"], new_events=state["events"])
    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    print(f"\ndashboard: http://{host}:{port}")
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI()
    site = (REPO_DIR / cfg["site_dir"]).resolve()
    app.mount("/static", StaticFiles(directory=str(REPO_DIR / "static")), name="static")
    for name in ("index.html", "app.js", "style.css", "data.json"):
        @app.get(f"/{name}")
        def _f(name=name):
            return FileResponse(site / name)
    @app.get("/")
    def _root():
        return FileResponse(site / "index.html")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_enrich(args):
    """Match listings to registered plot boundaries (INSPIRE, E&W only).

    Downloads and parses HM Land Registry INSPIRE polygon zips, then
    point-matches every listing's coordinates to its plot. Plot area is
    permanent, so each LA only needs downloading once; results persist
    in state.json (est_acres / est_plot_m2 / inspire_id).

    --all  : download + parse every E&W local authority (~4GB, one-off)
    --las  : comma-separated LA zip names (e.g. Cornwall_Council.zip)
    """
    from . import inspire

    cfg = load_config()
    snaps = pathlib.Path(cfg["snapshots_dir"])
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    state = load_state(snaps / "state.json")
    listings = state.get("listings") or {}

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

    LANDISH_SUBTYPES = {"land", "plot", "farm", "farm land", "smallholding",
                        "equestrian", "equestrian facility", "development land",
                        "building plot"}

    import re as _re

    house_max = float(cfg.get("enrich", {}).get("house_max_est_acres", 2.0))
    street_ev = _re.compile(r"\d|"
                            r"\b(road|street|lane|close|drive|avenue|way|crescent|terrace|"
                            r"mews|court|gardens|hill|park|place|approach|walk|row|square|"
                            r"villas|view|rise|fields|lawns|green|estate|quay|promenade)\b",
                            _re.I)

    def flag_est_rows():
        """Recompute the shared-site flag on every est row (rule may change)."""
        shared_n = 0
        for row in listings.values():
            if row.get("est_acres"):
                is_landish = (row.get("subtype") or "") in LANDISH_SUBTYPES
                bad_pin = not street_ev.search(row.get("address") or "")
                row["est_shared"] = bool(
                    (not is_landish and (row["est_acres"] > house_max or bad_pin))
                    or row["est_acres"] > 20)
                if row.get("est_shared"):
                    shared_n += 1
        return shared_n

    by_id = {k: v for k, v in listings.items() if v.get("lat") and v.get("lng")}
    print(f"{len(by_id)} listings with coordinates")

    bb = inspire.bboxes_map([n for n in names])
    matched_total = 0
    attempted_total = 0
    for name in names:
        try:
            pkl = inspire.build_index(name, verbose=True)
        except Exception as e:
            print(f"  {name}: download/parse failed: {e}")
            continue
        if pkl is None:
            print(f"  {name}: no polygons (skipped)")
            continue
        try:
            bbox = bb.get(name) or inspire.bbox_of(pkl)
            pts = []
            for rid, row in by_id.items():
                if row.get("est_checked"):
                    continue
                e, n = inspire.wgs84_to_bng(row["lat"], row["lng"])
                if bbox[0] <= e <= bbox[2] and bbox[1] <= n <= bbox[3]:
                    pts.append((rid, e, n))
            if not pts:
                continue
            hits = inspire.match_points(pts, bbox, pkl)
            hit_ids = set(hits)
            for rid, (area, gid) in hits.items():
                row = by_id[rid]
                row["est_plot_m2"] = round(area, 1)
                row["est_acres"] = round(area / 4046.86, 3)
                row["inspire_id"] = gid
                row["est_checked"] = True
                # pin inside a large registered title that isn't a land-type
                # listing is usually a farm/estate/site, not the house plot
                if row["est_acres"] > 20 and (row.get("subtype") or "") not in LANDISH_SUBTYPES:
                    row["est_shared"] = True
            for rid, _, _ in pts:
                if rid not in hit_ids:
                    by_id[rid]["est_checked"] = True
            matched_total += len(hits)
            attempted_total += len(pts)
            print(f"  {name}: {len(hits)}/{len(pts)} matched")
        except Exception as e:
            print(f"  {name}: matching failed: {e}")
            continue
        # persist after every LA so a crash never loses accumulated matches
        shared_n = flag_est_rows()
        state["listings"] = listings
        write_state(state, snaps, snaps / "events.jsonl", [])
        print(f"  ({shared_n} est rows flagged shared; state saved)")
    print(f"total: {matched_total}/{attempted_total} matched to registered plots")

    shared_n = flag_est_rows()
    print(f"{shared_n} est rows flagged as large/vague shared titles (excluded from ranking)")

    state["listings"] = listings
    write_state(state, snaps, snaps / "events.jsonl", [])
    build_site(state, cfg, site_dir=cfg["site_dir"], new_events=[])
    print("state + site updated")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="acres")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("regions", help="discover UK Rightmove region ids from sitemaps")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_regions)

    sp = sub.add_parser("run-once", help="scan all regions once and print the ranking")
    sp.add_argument("--regions", default="")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_run_once)

    sp = sub.add_parser("publish", help="scan + write snapshots + build static site (GitHub Actions)")
    sp.add_argument("--regions", default="")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("scrape-region", help="debug: scan one REGION^ id")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_scrape_region)

    sp = sub.add_parser("enrich", help="match listings to registered plot boundaries (INSPIRE)")
    sp.add_argument("--all", action="store_true", help="download+parse every E&W local authority (~4GB)")
    sp.add_argument("--las", default="", help="comma-separated LA zip names")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("serve", help="scan once then serve the dashboard locally")
    sp.add_argument("--regions", default="")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
