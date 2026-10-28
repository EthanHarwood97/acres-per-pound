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
from .store import dedupe_key

BASE = "https://www.rightmove.co.uk"
SALE_PAGE = BASE + "/property-for-sale/find.html"
SOURCE = "rightmove"

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


def _floor_sqm_of(prop):
    """Internal floor area in sqm, from displaySize ('829 sq. ft.') or
    first keyFeature ('829 Sqft'). Returns None when absent."""
    import re as _re
    for raw in [prop.get("displaySize") or ""] + _features_of(prop)[:3]:
        m = _re.search(r"([\d,]+(?:\.\d+)?)\s*(sq\.?\s*ft|sqft|square\s*feet)", raw or "", _re.I)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                return round(v * 0.092903, 1)
            except ValueError:
                pass
        m = _re.search(r"([\d,]+(?:\.\d+)?)\s*(sq\.?\s*m(?:etres?|eters?)?|sqm|square\s*metres?|square\s*meters?)",
                       raw or "", _re.I)
        if m:
            try:
                return round(float(m.group(1).replace(",", "")), 1)
            except ValueError:
                pass
    return None


def normalize(prop, region_id=0, region_name=""):
    """Normalize one searchResults.properties item into a listing row."""
    amount, price_text = _price_of(prop)
    features = _features_of(prop)
    address = prop.get("displayAddress") or ""
    reason, lu_date = _listing_update(prop)
    sid = str(prop.get("id") or "")
    lat = (prop.get("location") or {}).get("latitude")
    lng = (prop.get("location") or {}).get("longitude")
    pc = _pc_from_address(address)
    return {
        "uid": f"{SOURCE}:{sid}",
        "source": SOURCE,
        "source_id": sid,
        "rm_id": sid,
        "url": BASE + (prop.get("propertyUrl") or ""),
        "address": address,
        "postcode": pc,
        "outcode": pc.split(" ")[0] if pc else "",
        "lat": lat,
        "lng": lng,
        "price": amount,
        "price_text": price_text,
        "price_qualifier": ((prop.get("price") or {}).get("displayPrices") or [{}])[0].get("displayPriceQualifier") or "",
        "beds": prop.get("bedrooms") or 0,
        "baths": prop.get("bathrooms") or 0,
        "floor_sqm": _floor_sqm_of(prop),
        "tenure": ((prop.get("tenure") or {}).get("tenureType") or "") if isinstance(prop.get("tenure"), dict) else "",
        "subtype": (prop.get("propertySubType") or "").lower(),
        "type_full": prop.get("propertyTypeFullDescription") or "",
        "summary": (prop.get("summary") or "")[:2000],
        "features": features,
        "listing_status": reason,
        "first_published": _first_published(prop),
        "region_id": region_id,
        "region_name": region_name,
        "land_only": is_land_only(prop, {"beds": prop.get("bedrooms") or 0}),
        "dedupe_key": dedupe_key(lat, lng, pc, amount, prop.get("bedrooms") or 0),
    }


def _search_url(region_id, max_price, index=0, min_price=None, per_page=24,
                sort_newest=False, property_types=None, max_band=None):
    effective_max = max_band if max_band is not None else max_price
    u = (
        f"{SALE_PAGE}?locationIdentifier=REGION%5E{region_id}"
        f"&index={index}"
        f"&numberOfPropertiesPerPage={per_page}&includeSSTC=false"
    )
    if effective_max:
        u += f"&maxPrice={effective_max}"
    if property_types:
        u += "&propertyTypes=" + "%2C".join(property_types)
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
    max_price = search.get("max_price") or 0
    bands = list(search.get("price_bands") or [])
    ptypes = search.get("property_types") or []
    seen_ids = set(seen_ids or ())
    if mode == "new":
        seen = {}
        _scan_query(fetcher, region, max_price, None, None,
                    search.get("new_pages_per_region", 6), seen, verbose,
                    sort_newest=True, seen_ids=seen_ids,
                    stop_after=search.get("new_stop_pages", 2), ptypes=ptypes)
        return list(seen.values()), False

    # probe result count first
    try:
        r = fetcher.get(_search_url(region["id"], max_price, property_types=ptypes),
                        ttl=3600, rate_limit=True)
        sr = search_results(r.text)
        total = int(str(sr.get("resultCount") or "0").replace(",", ""))
    except Exception as e:
        if verbose:
            print(f"  REGION^{region['id']} ({region.get('name')}): probe failed: {e}")
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
            queries.append((bands[-1], None))  # open-ended top band

    seen = {}
    truncated = False
    for lo, hi in queries:
        got = _scan_query(fetcher, region, max_price, lo, hi, max_pages, seen, verbose,
                          ptypes=ptypes)
        truncated = truncated or got
    return list(seen.values()), truncated


def _scan_query(fetcher, region, max_price, min_price, max_band, max_pages, seen,
                verbose, sort_newest=False, seen_ids=None, stop_after=0, ptypes=None):
    index = 0
    truncated = False
    idle_pages = 0
    for page in range(max_pages):
        url = _search_url(region["id"], max_price, index=index, min_price=min_price,
                          sort_newest=sort_newest, property_types=ptypes,
                          max_band=max_band)
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

    _LANDISH_FETCH = {
        "barn", "barn conversion", "farm house", "farm land", "smallholding",
        "equestrian facility", "country house", "character property", "plot",
        "land", "lodge", "coach house", "stone house",
    }
    needs_detail = (
        parsed is None
        and prev_acres is None
        and (has_land_keyword(blob) or (listing.get("subtype") or "") in _LANDISH_FETCH)
    )
    if needs_detail:
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
        if not isinstance(pd, dict):
            return None
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
            print(f"  detail {listing.get('uid') or listing.get('rm_id')} failed: {e}")
        return None
