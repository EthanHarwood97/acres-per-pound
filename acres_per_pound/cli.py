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

    sp = sub.add_parser("serve", help="scan once then serve the dashboard locally")
    sp.add_argument("--regions", default="")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
