"""OnTheMarket source: region scans + detail pages -> normalized listings.

OTM is a Next.js app; search and detail pages embed the full Redux
state in <script id="__NEXT_DATA__">. Search results live at
initialReduxState.results.list (30 per page, hard cap 1000 results per
query, so oversized regions are price-banded exactly like Rightmove).
Property types are path segments: /for-sale/detached/<slug>/ etc.

Detail pages carry EPC rating, tenure, council tax band and the full
description. The detail fetch is only used for land parsing when the
search text hints at land, mirroring rightmove.enrich().

Owned by CoStar; keep rates low, personal research use.
"""

import json
import re
import time

from .landparse import has_land_keyword, parse_acres
from .store import dedupe_key

BASE = "https://www.onthemarket.com"
SOURCE = "onthemarket"
_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Property types we care about (houses + land; flats excluded by design).
HOUSE_TYPES = ("detached", "semi-detached", "terraced", "bungalow")
LAND_TYPES = ("land",)

_TYPE_LABEL = {
    "detached": "Detached house",
    "semi-detached": "Semi-detached house",
    "terraced": "Terraced house",
    "bungalow": "Bungalow",
    "land": "Land",
    "park-home": "Park home",
    "cottage": "Cottage",
    "character-property": "Character property",
    "farm": "Farm",
    "equestrian": "Equestrian",
    "barn-conversion": "Barn conversion",
}

_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_OUTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b", re.I)


def _next_data(html):
    m = _NEXT_RE.search(html)
    if not m:
        raise ValueError("no __NEXT_DATA__ on OTM page")
    return json.loads(m.group(1))


def search_state(html):
    """Return (results, query) from an OTM search page."""
    d = _next_data(html)
    irs = d["props"]["initialReduxState"]
    return irs.get("results") or {}, irs.get("query") or {}


def _pc_of(address):
    m = _POSTCODE_RE.search(address or "")
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    m = _OUTCODE_RE.search(address or "")
    return m.group(1).upper() if m else ""


def _price_of(item):
    """OTM price strings: '£260,000', 'Guide price £260,000', 'POA'."""
    raw = item.get("price") or ""
    qual = item.get("price-qualifier") or ""
    m = re.search(r"£\s*([\d,]+)", raw)
    amount = int(m.group(1).replace(",", "")) if m else 0
    text = f"{qual} {raw}".strip()
    return amount, text, qual


def normalize(item, region_id="", region_name=""):
    """Normalize one OTM search result into a listing row."""
    sid = str(item.get("id") or "")
    amount, price_text, qual = _price_of(item)
    address = item.get("address") or ""
    loc = item.get("location") or {}
    lat = loc.get("lat")
    lng = loc.get("lon")
    pc = _pc_of(address)
    ptype = (item.get("humanised-property-type") or "").lower()
    features = [str(f) for f in (item.get("features") or [])]
    return {
        "uid": f"{SOURCE}:{sid}",
        "source": SOURCE,
        "source_id": sid,
        "url": BASE + (item.get("details-url") or f"/details/{sid}/"),
        "address": address,
        "postcode": pc,
        "outcode": pc.split(" ")[0] if pc else "",
        "lat": lat,
        "lng": lng,
        "price": amount,
        "price_text": price_text,
        "price_qualifier": qual,
        "beds": item.get("bedrooms") or 0,
        "baths": item.get("bathrooms") or 0,
        "property_type": ptype,
        "subtype": _subtype_of(ptype),
        "summary": (item.get("property-title") or "")[:300],
        "features": features,
        "listing_status": item.get("main-label") or "",
        "first_published": "",
        "region_id": region_id,
        "region_name": region_name,
        "land_only": _subtype_of(ptype) in ("land", "plot", "farm land", "development land"),
        "dedupe_key": dedupe_key(lat, lng, pc, amount, item.get("bedrooms") or 0),
    }


def _subtype_of(ptype):
    p = (ptype or "").lower()
    for key, sub in (
        ("detached", "detached"), ("semi-detached", "semi-detached"),
        ("terraced", "terraced"), ("bungalow", "bungalow"),
        ("land", "land"), ("plot", "plot"), ("farm", "farm"),
        ("equestrian", "equestrian"), ("park home", "park home"),
        ("cottage", "cottage"), ("barn conversion", "barn conversion"),
        ("maisonette", "maisonette"), ("flat", "flat"), ("apartment", "apartment"),
    ):
        if key in p:
            return sub
    return p


def search_url(slug, page=1, max_price=None, min_price=None, min_beds=None,
               max_beds=None, radius=None, property_type=None, sort=None):
    """Build an OTM search URL.

    Property type is a path segment; everything else is a query param.
    """
    seg = f"/{property_type}" if property_type else "/property"
    u = f"{BASE}/for-sale{seg}/{slug}/?page={page}"
    if max_price:
        u += f"&max-price={max_price}"
    if min_price:
        u += f"&min-price={min_price}"
    if min_beds:
        u += f"&min-bedrooms={min_beds}"
    if max_beds:
        u += f"&max-bedrooms={max_beds}"
    if radius:
        u += f"&radius={radius}"
    if sort:
        u += f"&sort-field={sort}"
    return u


def scan_region(fetcher, region, cfg, max_pages=34, mode="full", seen_ids=None,
                verbose=False):
    """Scan one OTM region.

    mode="full": paginate everything (price-banded if over the 1000 cap).
    mode="new": newest-first pages, stop after idle pages.
    """
    search = cfg["search"]
    cap = search.get("cap", 1000)
    max_price = search.get("max_price") or 0
    bands = list(search.get("price_bands") or [])
    seen_ids = set(seen_ids or ())
    slug = region.get("slug") or region.get("id")
    rname = region.get("name") or slug

    if mode == "new":
        seen = {}
        _scan_query(fetcher, slug, rname, max_price, None, None,
                    search.get("new_pages_per_region", 6), seen, verbose,
                    seen_ids=seen_ids, stop_after=search.get("new_stop_pages", 2),
                    newest=True)
        return list(seen.values()), False

    try:
        r = fetcher.get(search_url(slug), ttl=3600, rate_limit=True)
        res, _q = search_state(r.text)
        total = int(str(res.get("totalResults") or "0").replace(",", ""))
    except Exception as e:
        if verbose:
            print(f"  otm {slug}: probe failed: {e}")
        return [], False

    queries = []
    if total <= cap or not bands:
        queries.append((None, None))
    else:
        if max_price:
            bands = [b for b in bands if b < max_price] + [max_price]
            queries = [(bands[i], bands[i + 1] - 1) for i in range(len(bands) - 1)]
        else:
            queries = [(bands[i], bands[i + 1] - 1) for i in range(len(bands) - 1)]
            queries.append((bands[-1], None))

    seen = {}
    truncated = False
    for lo, hi in queries:
        got = _scan_query(fetcher, slug, rname, max_price, lo, hi, max_pages,
                          seen, verbose)
        truncated = truncated or got
    return list(seen.values()), truncated


def _scan_query(fetcher, slug, rname, max_price, min_price, max_band, max_pages,
                seen, verbose, seen_ids=None, stop_after=0, newest=False):
    idle_pages = 0
    truncated = False
    for page in range(1, max_pages + 1):
        url = search_url(slug, page=page, max_price=max_band if max_band is not None else max_price,
                         min_price=min_price, sort="newest" if newest else None)
        try:
            r = fetcher.get(url, ttl=3600, rate_limit=True)
        except Exception as e:
            if verbose:
                print(f"  otm {slug} page {page} failed: {e}")
            truncated = True
            break
        if r.status_code != 200:
            truncated = True
            break
        try:
            res, _q = search_state(r.text)
        except Exception:
            truncated = True
            break
        items = res.get("list") or []
        if not items:
            break
        new_this_page = 0
        for item in items:
            row = normalize(item, slug, rname)
            if row["uid"] and row["uid"] not in seen:
                seen[row["uid"]] = row
                if seen_ids is not None and row["uid"] not in seen_ids:
                    new_this_page += 1
        if seen_ids is not None:
            if new_this_page == 0:
                idle_pages += 1
                if stop_after and idle_pages >= stop_after:
                    break
            else:
                idle_pages = 0
        total = int(str(res.get("totalResults") or "0").replace(",", ""))
        if page * 30 >= min(total, 1000):
            break
        if page == max_pages:
            truncated = True
    return truncated


# --- region discovery -----------------------------------------------------

def discover(fetcher=None, out_path=None, slugs=None, min_count=100, verbose=False):
    """Probe OTM location slugs and keep the ones with real inventory.

    Seeds from the Rightmove region slugs (data/regions.json) by default;
    OTM's own sitemaps are flaky and mix in SEO variants.
    """
    import pathlib

    from .http import DATA_DIR, load_config
    from .regions import load_regions

    cfg = load_config()
    fetcher = fetcher or __import__("acres_per_pound.http", fromlist=["Fetcher"]).Fetcher(cfg)
    out_path = pathlib.Path(out_path or "data/regions_otm.json")
    if not out_path.is_absolute():
        out_path = DATA_DIR.parent / out_path

    if slugs is None:
        slugs = [r.get("slug", "").lower() for r in load_regions() if r.get("slug")]
    regions = []
    for i, slug in enumerate(slugs, 1):
        try:
            r = fetcher.get(search_url(slug), ttl=86400 * 7, rate_limit=True)
            if r.status_code != 200:
                continue
            res, q = search_state(r.text)
        except Exception:
            continue
        total = int(str(res.get("totalResults") or "0").replace(",", ""))
        loc = q.get("locationData") or {}
        if total >= min_count and loc:
            regions.append({
                "slug": slug,
                "name": loc.get("name") or slug,
                "id": slug,
                "location_type": loc.get("locationType"),
                "parent": loc.get("parent"),
                "count_all": total,
            })
            if verbose:
                print(f"  {i}/{len(slugs)} {slug}: {total} listings ({loc.get('name')})")
        elif verbose:
            print(f"  {i}/{len(slugs)} {slug}: skip ({total})")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(regions, ensure_ascii=False, indent=1), encoding="utf-8")
    return regions


# --- land enrichment ------------------------------------------------------

def enrich(fetcher, listing, cfg, prev_acres=None, verbose=False):
    """Attach parsed acreage from the detail page when the search text
    hints at land. Mirrors rightmove.enrich()."""
    texts = [listing.get("summary") or ""] + (listing.get("features") or [])
    blob = "\n".join(texts)
    parsed = parse_acres(blob)
    needs_detail = (
        parsed is None
        and prev_acres is None
        and (has_land_keyword(blob)
             or (listing.get("subtype") or "") in (
                 "land", "plot", "farm", "farm land", "smallholding",
                 "equestrian", "barn conversion", "cottage", "character property"))
    )
    if needs_detail:
        parsed = _fetch_and_parse(fetcher, listing, verbose)
        listing["detail_checked"] = True
    if parsed is not None and parsed[4] != "exact" and not listing.get("detail_checked"):
        detail_parsed = _fetch_and_parse(fetcher, listing, verbose)
        if detail_parsed is not None:
            rank = {"exact": 4, "approx": 3, "range": 3, "partial": 2, "converted": 1}
            if rank.get(detail_parsed[4], 0) >= rank.get(parsed[4], 0):
                parsed = detail_parsed
        listing["detail_checked"] = True
    if parsed:
        a_min, a_max, a_mid, unit, conf, matched, cands, communal = parsed
        listing["acres_min"] = a_min
        listing["acres_max"] = a_max
        listing["acres_mid"] = a_mid
        listing["acre_unit"] = unit
        listing["confidence"] = conf
        listing["matched"] = matched
        listing["candidates"] = cands
        listing["communal"] = communal
        if a_mid is not None and (listing.get("subtype") or "") in (
                "land", "plot", "farm", "farm land", "development land", "building plot"):
            listing["land_only"] = True
    return listing


def _fetch_and_parse(fetcher, listing, verbose):
    """Fetch the detail page, attach EPC/tenure facts, parse the description."""
    try:
        r = fetcher.get(listing["url"], ttl=86400 * 30, rate_limit=True)
        d = _next_data(r.text)
        prop = (d["props"]["initialReduxState"].get("property") or {})
        desc = prop.get("description") or ""
        extra = [desc]
        for item in prop.get("features") or []:
            if isinstance(item, dict):
                extra.append(item.get("feature") or "")
            else:
                extra.append(str(item))
        listing["description"] = desc
        epc = prop.get("epc") or {}
        if epc.get("rating"):
            listing["epc_rating"] = epc["rating"]
        if epc.get("date"):
            listing["epc_date"] = epc["date"]
        for ki in prop.get("keyInfo") or []:
            title = (ki.get("title") or "").lower()
            if title == "tenure" and ki.get("value"):
                listing["tenure"] = ki["value"]
            elif title == "council tax" and ki.get("value"):
                listing["council_tax"] = ki["value"]
        return parse_acres("\n".join(x for x in extra if x))
    except Exception as e:
        if verbose:
            print(f"  otm detail {listing.get('uid')} failed: {e}")
        return None
