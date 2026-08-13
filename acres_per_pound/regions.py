"""Discover UK Rightmove region identifiers from Rightmove's own sitemaps.

Rightmove publishes sitemap-regions-<Name>.xml files listing every
region/town. The SEO search page for a location embeds its internal
locationIdentifier (REGION^<id>) in the __NEXT_DATA__ payload, so the
identifier space can be recovered without any private API:

    sitemap.xml -> sitemap-regions-*.xml -> /property-for-sale/<slug>.html
                                            -> searchResults.location.id

One-time discovery; the result is cached to data/regions.json.
"""

import json
import re
import pathlib

from .http import DATA_DIR, load_config
from .rmjson import search_results

BASE = "https://www.rightmove.co.uk"


def _sitemap_slugs(fetcher):
    r = fetcher.get(BASE + "/sitemap.xml", ttl=86400 * 7, rate_limit=False)
    if r.status_code != 200:
        raise RuntimeError(f"sitemap.xml status {r.status_code}")
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    slugs = []
    for loc in locs:
        m = re.search(r"sitemap-regions-(.+)\.xml", loc)
        if m:
            slug = m.group(1)
            slug = re.sub(r"-\d+$", "", slug)
            slugs.append(slug)
    return sorted(set(slugs))


def _location_of(fetcher, slug):
    url = f"{BASE}/property-for-sale/{slug}.html"
    r = fetcher.get(url, ttl=86400 * 30, rate_limit=True)
    if r.status_code != 200 or "page-not-found" in str(r.url):
        return None
    try:
        sr = search_results(r.text)
    except Exception:
        return None
    loc = sr.get("location") or {}
    if not loc.get("id"):
        return None
    count_raw = str(sr.get("resultCount") or "0").replace(",", "")
    try:
        count = int(count_raw)
    except ValueError:
        count = 0
    return {
        "slug": slug,
        "id": int(loc.get("id")),
        "name": loc.get("displayName"),
        "type": loc.get("locationType"),
        "count_all": count,
    }


MIN_COUNT = 100  # skip hamlets/tiny towns; keeps counties + cities


def discover(fetcher=None, out_path=None, limit=0, verbose=False):
    cfg = load_config()
    fetcher = fetcher or __import__("acres_per_pound.http", fromlist=["Fetcher"]).Fetcher(cfg)
    out_path = pathlib.Path(out_path or cfg.get("regions", {}).get("file", "data/regions.json"))
    if not out_path.is_absolute():
        out_path = DATA_DIR.parent / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    slugs = _sitemap_slugs(fetcher)
    if limit:
        slugs = slugs[:limit]
    regions = []
    for i, slug in enumerate(slugs, 1):
        try:
            loc = _location_of(fetcher, slug)
        except Exception as e:
            loc = None
            if verbose:
                print(f"  {slug}: error {e}")
        if loc:
            count = loc.get("count_all") or 0
            if count >= MIN_COUNT:
                regions.append(loc)
                if verbose:
                    print(f"  {i}/{len(slugs)} {slug} -> REGION^{loc['id']} ({loc['name']}, {count} listings)")
            elif verbose:
                print(f"  {i}/{len(slugs)} {slug}: skipped ({count} listings)")
        else:
            if verbose:
                print(f"  {i}/{len(slugs)} {slug}: skipped (no data)")
    out_path.write_text(json.dumps(regions, ensure_ascii=False, indent=1), encoding="utf-8")
    return regions


def load_regions(path=None):
    cfg = load_config()
    p = pathlib.Path(path or cfg.get("regions", {}).get("file", "data/regions.json"))
    if not p.is_absolute():
        p = DATA_DIR.parent / p
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))
