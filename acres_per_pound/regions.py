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

SCOTLAND_REGIONS = {
    "aberdeenshire", "angus", "argyll and bute", "argyll-and-bute", "clackmannanshire",
    "dumfries and galloway", "dumfries-and-galloway", "east ayrshire", "east-ayrshire",
    "east dunbartonshire", "east-dunbartonshire", "east lothian", "east-lothian",
    "east renfrewshire", "east-renfrewshire", "falkirk", "falkirk (county)", "falkirk-county",
    "fife", "glasgow", "highland", "highland, scotland", "inverclyde", "midlothian",
    "moray", "north ayrshire", "north-ayrshire", "north lanarkshire", "north-lanarkshire",
    "orkney", "orkney, orkney islands", "perth and kinross", "perth-and-kinross",
    "renfrewshire", "scottish borders", "scottish-borders", "south ayrshire", "south-ayrshire",
    "south lanarkshire", "south-lanarkshire", "stirling", "stirling (county)", "stirling-county",
    "west dunbartonshire", "west-dunbartonshire", "west lothian", "west-lothian",
    "shetland", "shetland islands", "western isles", "edinburgh", "dundee",
}

WALES_REGIONS = {
    "bangor", "bangor, gwynedd", "blaenau gwent", "blaenau-gwent",
    "bridgend", "bridgend (county of)", "bridgend-county-of",
    "caerphilly", "caerphilly (county of)", "caerphilly-county-of",
    "cardiff", "cardiff (county of)", "cardiff-county-of",
    "carmarthenshire", "carmarthenshire, mid wales",
    "ceredigion", "ceredigion, mid wales",
    "conwy", "conwy (county of)", "conwy-county-of",
    "denbighshire", "flintshire", "gwynedd", "isle of anglesey", "isle-of-anglesey",
    "merthyr tydfil", "merthyr tydfil (county of)", "merthyr-tydfil-county-of",
    "monmouthshire", "neath port talbot", "neath-port-talbot",
    "newport", "newport (county of)", "newport-county-of",
    "pembrokeshire", "pembrokeshire, south west wales",
    "powys", "rhondda cynon taff", "rhondda-cynon-taff",
    "swansea", "swansea (county of)", "swansea-county-of",
    "torfaen", "vale of glamorgan", "vale-of-glamorgan",
    "wrexham", "wrexham (county of)", "wrexham-county-of",
}

# Highlands & Islands / Far North ("top of Scotland")
HIGHLANDS_ISLANDS_REGIONS = {
    "highland", "highland, scotland", "orkney", "orkney, orkney islands",
    "moray", "aberdeenshire", "argyll and bute", "argyll-and-bute",
    "angus", "perth and kinross", "perth-and-kinross", "western isles", "shetland",
}


def classify_region(region_or_name_or_slug):
    """Classify region by country (England, Scotland, Wales) and highland/island flag."""
    if isinstance(region_or_name_or_slug, dict):
        slug = region_or_name_or_slug.get("slug") or ""
        name = region_or_name_or_slug.get("name") or ""
    elif isinstance(region_or_name_or_slug, str):
        slug = region_or_name_or_slug
        name = region_or_name_or_slug
    else:
        slug = ""
        name = ""

    slug_l = slug.strip().lower()
    name_l = name.strip().lower()

    if (slug_l in SCOTLAND_REGIONS or name_l in SCOTLAND_REGIONS
            or "scotland" in name_l or "scottish" in name_l):
        country = "Scotland"
    elif (slug_l in WALES_REGIONS or name_l in WALES_REGIONS
          or "wales" in name_l or "gwynedd" in name_l or "cymru" in name_l):
        country = "Wales"
    elif "northern ireland" in name_l or name_l.startswith("bt"):
        country = "Northern Ireland"
    else:
        country = "England"

    is_highlands = (
        slug_l in HIGHLANDS_ISLANDS_REGIONS
        or name_l in HIGHLANDS_ISLANDS_REGIONS
        or any(k in name_l for k in ["highland", "orkney", "shetland", "moray", "western isles", "aberdeenshire", "argyll"])
    )
    return {"country": country, "is_highlands": is_highlands}


def filter_regions(regs, cfg=None, enabled=None, excluded=None,
                   countries=None, exclude_countries=None,
                   exclude_highlands=False):
    """Filter list of region objects against whitelists and blacklists."""
    cfg_reg = (cfg or {}).get("regions", {})
    enabled = enabled if enabled is not None else cfg_reg.get("enabled") or []
    excluded = excluded if excluded is not None else cfg_reg.get("excluded") or []
    countries = countries if countries is not None else cfg_reg.get("countries") or []
    exclude_countries = exclude_countries if exclude_countries is not None else cfg_reg.get("exclude_countries") or []
    if exclude_highlands is False:
        exclude_highlands = bool(cfg_reg.get("exclude_highlands", False))

    enabled_set = {e.strip().lower() for e in enabled if e.strip()}
    excluded_set = {x.strip().lower() for x in excluded if x.strip()}
    wanted_countries = {c.strip().lower() for c in countries if c.strip()}
    excl_countries = {c.strip().lower() for c in exclude_countries if c.strip()}

    out = []
    for r in regs:
        cls = classify_region(r)
        c_name = cls["country"].lower()
        slug_low = (r.get("slug") or "").lower()
        name_low = (r.get("name") or "").lower()
        id_str = str(r.get("id") or "")

        # Whitelist checks
        if enabled_set:
            if not (slug_low in enabled_set or name_low in enabled_set or id_str in enabled_set):
                continue
        if wanted_countries and c_name not in wanted_countries:
            continue

        # Blacklist checks
        if excl_countries and c_name in excl_countries:
            continue
        if exclude_highlands and cls["is_highlands"]:
            continue
        if excluded_set:
            if (slug_low in excluded_set or name_low in excluded_set or id_str in excluded_set or
                    any(ex in name_low or ex in slug_low for ex in excluded_set if len(ex) > 2)):
                continue

        out.append(r)
    return out


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
    loc_id = loc.get("id")
    if not loc_id:
        return None
    count_raw = str(sr.get("resultCount") or "0").replace(",", "")
    try:
        count = int(count_raw)
    except ValueError:
        count = 0
    return {
        "slug": slug,
        "id": int(loc_id),
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
