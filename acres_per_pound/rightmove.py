"""Rightmove source: region scans + detail pages -> normalized listings.

Search pages embed everything we need (id, price, address, lat/lng,
key features, summary) in __NEXT_DATA__; detail pages embed the full
description in window.__PAGE_MODEL. We fetch a detail page only when a
listing's search text hints at land but yields no parseable acreage -
and only once, because parsed acreage is persisted in state.json.

Technique validated against live pages 2026-08; Rightmove prohibits
scraping in its ToS - keep rates low, personal use.
"""

import json
import re
import time

from .http import load_config
from .landparse import has_land_keyword, parse_acres
from .rmjson import page_model, search_results

BASE = "https://www.rightmove.co.uk"
SALE_PAGE = BASE + "/property-for-sale/find.html"

LAND_SUBTYPES = {
    "land", "farm", "farm land", "smallholding", "equestrian", "equestrian facility",
    "development land", "building plot", "plot",
}

_LAND_TITLE_RE = re.compile(
    r"\b(?:development site|building site|land at|land &|woodland|grazing land|"
    r"agricultural land|building land|residential development site|land and building)\b",
    re.I)


def is_land_only(prop, row, acres_mid=None):
    if (prop.get("propertySubType") or "").lower() in LAND_SUBTYPES:
        return True
    if acres_mid is None:
        return False
    if not (row.get("beds") or 0) and acres_mid >= 2:
        return True
    title = (prop.get("propertyTypeFullDescription") or "") + " " + (prop.get("summary") or "")
    if _LAND_TITLE_RE.search(title) and acres_mid >= 5:
        return True
    return False

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s+(\d[A-Z]{2})\b|\b([A-Z]{1,2}\d[A-Z\d]?)\b")


def _pc_from_address(addr):
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b", addr or "")
    if not m:
        return ""
    if m.group(2):
        return f"{m.group(1)} {m.group(2)}"
    return m.group(1)


def _price_of(prop):
    p = prop.get("price") or {}
    amount = p.get("amount") or 0
    dp = (p.get("displayPrices") or [{}])[0]
    qual = (dp.get("displayPriceQualifier") or "").strip()
    disp = (dp.get("displayPrice") or "").strip()
    text = f"{qual} {disp}".strip() if qual else disp
    if not text and amount:
        text = f"£{amount:,}"
    if (int(amount or 0)) < 100:  # POA / Offers Invited arrive as amount=1
        amount = 0
    return int(amount or 0), text


def _features_of(prop):
    out = []
    for item in prop.get("keyFeatures") or []:
        if isinstance(item, dict):
            out.append(item.get("description") or item.get("htmlDescription") or "")
        else:
            out.append(str(item))
    return out


def _listing_update(prop):
    lu = prop.get("listingUpdate") or {}
    reason = lu.get("listingUpdateReason") or ""
    date = lu.get("listingUpdateDate") or ""
    return reason, date


def _first_published(prop):
    for key in ("firstVisibleDate", "firstPublishedDate", "addedOrReducedDate"):
        v = prop.get(key)
        if v:
            return v
    return ""


def normalize(prop, region_id=0, region_name=""):
    """Normalize one searchResults.properties item into a listing row."""
    amount, price_text = _price_of(prop)
    features = _features_of(prop)
    address = prop.get("displayAddress") or ""
    reason, lu_date = _listing_update(prop)
    return {
        "rm_id": str(prop.get("id") or ""),
        "url": BASE + (prop.get("propertyUrl") or ""),
        "address": address,
        "postcode": _pc_from_address(address),
        "lat": (prop.get("location") or {}).get("latitude"),
        "lng": (prop.get("location") or {}).get("longitude"),
        "price": amount,
        "price_text": price_text,
        "beds": prop.get("bedrooms") or 0,
        "baths": prop.get("bathrooms") or 0,
        "subtype": (prop.get("propertySubType") or "").lower(),
        "type_full": prop.get("propertyTypeFullDescription") or "",
        "summary": (prop.get("summary") or "")[:300],
        "features": features,
        "listing_status": reason,
        "first_published": _first_published(prop),
        "region_id": region_id,
        "region_name": region_name,
        "land_only": is_land_only(prop, {"beds": prop.get("bedrooms") or 0}),
    }


def _search_url(region_id, max_price, index=0, min_price=None, per_page=24, sort_newest=False):
    u = (
        f"{SALE_PAGE}?locationIdentifier=REGION%5E{region_id}"
        f"&maxPrice={max_price}&index={index}"
        f"&numberOfPropertiesPerPage={per_page}&includeSSTC=false"
    )
    if min_price:
        u += f"&minPrice={min_price}"
    if sort_newest:
        u += "&sortType=6"
    return u


def scan_region(fetcher, region, cfg, max_pages=42, mode="full", seen_ids=None, verbose=False):
    """Scan one region.

    mode="full": paginate everything (price-banded if over the query cap).
    mode="new": newest-first; stop after `new_stop_pages` pages with no
    previously-unseen listings. Used for cheap freshness sweeps between
    full reconcile scans.
    """
    search = cfg["search"]
    cap = search.get("cap", 1000)
    max_price = search["max_price"]
    bands = search.get("price_bands") or []
    seen_ids = set(seen_ids or ())
    if mode == "new":
        seen = {}
        _scan_query(fetcher, region, max_price, None, None,
                    search.get("new_pages_per_region", 6), seen, verbose,
                    sort_newest=True, seen_ids=seen_ids,
                    stop_after=search.get("new_stop_pages", 2))
        return list(seen.values()), False

    # probe result count first
    try:
        r = fetcher.get(_search_url(region["id"], max_price), ttl=3600, rate_limit=True)
        sr = search_results(r.text)
        total = int(str(sr.get("resultCount") or "0").replace(",", ""))
    except Exception as e:
        if verbose:
            print(f"  REGION^{region['id']} ({region.get('name')}): probe failed: {e}")
        return [], False

    queries = []
    if total <= cap:
        queries.append((None, None))
    else:
        for lo, hi in zip(bands, bands[1:]):
            queries.append((lo, hi - 1))

    seen = {}
    truncated = False
    for lo, hi in queries:
        if lo is not None:
            got = _scan_query(fetcher, region, max_price, lo, hi, max_pages, seen, verbose)
        else:
            got = _scan_query(fetcher, region, max_price, None, None, max_pages, seen, verbose)
        truncated = truncated or got
    return list(seen.values()), truncated


def _scan_query(fetcher, region, max_price, min_price, max_band, max_pages, seen,
                verbose, sort_newest=False, seen_ids=None, stop_after=0):
    index = 0
    truncated = False
    idle_pages = 0
    for page in range(max_pages):
        url = _search_url(region["id"], max_price, index=index, min_price=min_price,
                          sort_newest=sort_newest)
        try:
            r = fetcher.get(url, ttl=3600, rate_limit=True)
        except Exception as e:
            if verbose:
                print(f"  page {page} failed: {e}")
            truncated = True
            break
        if r.status_code != 200 or "page-not-found" in str(r.url):
            truncated = True
            break
        try:
            sr = search_results(r.text)
        except Exception:
            truncated = True
            break
        props = sr.get("properties") or []
        if not props:
            break
        new_this_page = 0
        for prop in props:
            row = normalize(prop, region["id"], region.get("name", ""))
            if row["rm_id"] and row["rm_id"] not in seen:
                seen[row["rm_id"]] = row
                if seen_ids is not None and row["rm_id"] not in seen_ids:
                    new_this_page += 1
        if seen_ids is not None:
            if new_this_page == 0:
                idle_pages += 1
                if stop_after and idle_pages >= stop_after:
                    break
            else:
                idle_pages = 0
        pagination = sr.get("pagination") or {}
        nxt = pagination.get("next")
        if not nxt:
            break
        index = int(nxt)
        if page == max_pages - 1 and nxt:
            truncated = True
    return truncated


def enrich(fetcher, listing, cfg, prev_acres=None, verbose=False):
    """Attach parsed acreage. Fetches the detail page when needed.

    Returns the listing row (mutated in place) with acres_* fields set.
    Non-exact parses get a one-time verification against the full
    description (which carries more context than the search summary).
    """
    texts = [listing.get("summary") or ""] + (listing.get("features") or [])
    blob = "\n".join(texts)
    parsed = parse_acres(blob)

    if parsed is None and prev_acres is None and has_land_keyword(blob):
        parsed = _fetch_and_parse(fetcher, listing, verbose)
        if parsed is None:
            listing["detail_checked"] = True

    if parsed is not None and parsed[4] != "exact" and not listing.get("detail_checked"):
        # approximate/range/converted/partial from truncated summary text:
        # the full description usually contains the precise sentence
        detail_parsed = _fetch_and_parse(fetcher, listing, verbose)
        if detail_parsed is not None:
            # prefer the detail parse when it is at least as confident
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
        if a_mid is not None and is_land_only(
                {"propertySubType": listing.get("subtype") or "",
                 "propertyTypeFullDescription": listing.get("type_full") or "",
                 "summary": listing.get("summary") or ""},
                listing, a_mid):
            listing["land_only"] = True
    return listing


def _fetch_and_parse(fetcher, listing, verbose):
    """Fetch the detail page and parse its full description."""
    try:
        r = fetcher.get(listing["url"], ttl=86400 * 30, rate_limit=True)
        pd = page_model(r.text)
        desc = ((pd.get("text") or {}).get("description") or "")
        extra = [desc]
        for item in pd.get("keyFeatures") or []:
            if isinstance(item, dict):
                extra.append(item.get("description") or item.get("htmlDescription") or "")
            else:
                extra.append(str(item))
        listing["description"] = desc
        return parse_acres("\n".join(extra))
    except Exception as e:
        if verbose:
            print(f"  detail {listing['rm_id']} failed: {e}")
        return None
