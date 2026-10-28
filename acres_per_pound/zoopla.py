"""Zoopla source (local-only: Cloudflare blocks datacenter IPs).

Zoopla renders search results server-side with a JSON-LD
`SearchResultsPage` payload that carries, per listing: price, address,
lat/lng, bedrooms/bathrooms and `floorSize` in square feet - the most
complete portal-stated size data of the three portals. Detail pages
carry a `RealEstateListing` JSON-LD.

Because plain HTTP gets a Cloudflare challenge, this module drives a
real Chromium via Playwright and exposes a tiny `BrowserFetcher` with
the same `.get(url)` shape as `http.Fetcher`, so the engine can treat
it like any other source. Run it on a home connection, not CI.

Run:
    python -m acres_per_pound.cli regions --zoopla
    python -m acres_per_pound.cli scrape-region bristol --source zoopla
"""

import json
import re
import time

from .landparse import has_land_keyword, parse_acres
from .store import dedupe_key

BASE = "https://www.zoopla.co.uk"
SOURCE = "zoopla"

_PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_OUT_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b", re.I)


class BrowserFetcher:
    """Playwright adapter exposing httpx-like .get(url).text/status_code."""

    def __init__(self, cfg=None, headless=True, delay=1.5):
        from playwright.sync_api import sync_playwright

        self.delay = delay
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=headless)
        self.ctx = self.browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 900},
            user_agent=((cfg or {}).get("http", {}).get("user_agent")
                        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
        )
        self.page = self.ctx.new_page()
        self._last = 0.0

    class _Resp:
        def __init__(self, text, status_code, url):
            self.text = text
            self.status_code = status_code
            self.url = url

    def get(self, url, ttl=0, rate_limit=True, wait_selector=None, timeout=60000):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            status = r.status if r else 200
        except Exception:
            status = 0
        # let Cloudflare's managed challenge resolve and JSON-LD render
        deadline = time.time() + 20
        while time.time() < deadline:
            html = self.page.content()
            if "Just a moment" not in html and 'type="application/ld+json"' in html:
                break
            time.sleep(0.5)
        if wait_selector:
            try:
                self.page.wait_for_selector(wait_selector, timeout=8000)
            except Exception:
                pass
        return self._Resp(self.page.content(), status, url)

    def close(self):
        try:
            self.ctx.close()
            self.browser.close()
            self._pw.stop()
        except Exception:
            pass


# --- parsing --------------------------------------------------------------

def _jsonld_blocks(html):
    return re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)


def search_items(html):
    """Return the ItemList items from a Zoopla search page's JSON-LD."""
    for block in _jsonld_blocks(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        nodes = graph if isinstance(graph, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "SearchResultsPage":
                items = ((node.get("mainEntity") or {}).get("itemListElement")) or []
                return items
            if node.get("@type") == "ItemList":
                return node.get("itemListElement") or []
    return []


def detail_node(html):
    for block in _jsonld_blocks(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "RealEstateListing":
                return node
    return None


def _subtype(name):
    n = (name or "").lower()
    for key in ("detached", "semi-detached", "terraced", "bungalow", "maisonette",
                "flat", "apartment", "land", "plot", "farm", "cottage",
                "park home", "house", "town house", "mews"):
        if key in n:
            return key
    return n


def _postcode(address):
    m = _PC_RE.search(address or "")
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    m = _OUT_RE.search(address or "")
    return m.group(1).upper() if m else ""


def normalize(item, region_id="", region_name=""):
    """One JSON-LD ListItem -> listing row."""
    product = item.get("item") or item
    url = product.get("url") or ""
    m = re.search(r"/details/(\d+)", url)
    sid = m.group(1) if m else url.rstrip("/").split("/")[-1]
    offer = product.get("offers") or {}
    raw_price = offer.get("price")
    try:
        price = int(float(raw_price)) if raw_price else 0
    except (TypeError, ValueError):
        price = 0
    ent = product.get("isRelatedTo") or {}
    if isinstance(ent, list):
        ent = ent[0] if ent else {}
    geo = ent.get("geo") or {}
    addr = ent.get("address") or product.get("name") or ""
    pc = _postcode(addr)
    floor = ent.get("floorSize") or {}
    raw_floor = floor.get("value")
    try:
        floor_sqft = float(raw_floor) if raw_floor else None
    except (TypeError, ValueError):
        floor_sqft = None
    subtype = _subtype(product.get("name"))
    beds = ent.get("numberOfBedrooms") or 0
    baths = ent.get("numberOfBathroomsTotal") or 0
    return {
        "uid": f"{SOURCE}:{sid}",
        "source": SOURCE,
        "source_id": str(sid),
        "url": url,
        "address": addr,
        "postcode": pc,
        "outcode": pc.split(" ")[0] if pc else "",
        "lat": geo.get("latitude"),
        "lng": geo.get("longitude"),
        "price": price,
        "price_text": f"£{price:,}" if price else "",
        "beds": beds,
        "baths": baths,
        "subtype": subtype,
        "property_type": subtype,
        "summary": (product.get("description") or "")[:300],
        "features": [],
        "floor_sqm": round(floor_sqft * 0.092903, 1) if floor_sqft else None,
        "listing_status": "",
        "first_published": "",
        "region_id": region_id,
        "region_name": region_name,
        "land_only": subtype in ("land", "plot"),
        "dedupe_key": dedupe_key(geo.get("latitude"), geo.get("longitude"),
                                 pc, price, beds),
    }


# --- scanning -------------------------------------------------------------

def search_url(slug, page=1, max_price=None, min_price=None, min_beds=None,
               max_beds=None, property_type=None):
    seg = f"/{property_type}" if property_type else "/property"
    u = f"{BASE}/for-sale{seg}/{slug}/?page={page}"
    if max_price:
        u += f"&max_price={max_price}"
    if min_price:
        u += f"&min_price={min_price}"
    if min_beds:
        u += f"&beds_min={min_beds}"
    if max_beds:
        u += f"&beds_max={max_beds}"
    return u


def scan_region(fetcher, region, cfg, max_pages=40, mode="full", seen_ids=None,
                verbose=False):
    """Scan one Zoopla region (JSON-LD; no detail fetches)."""
    search = cfg["search"]
    cap = search.get("cap", 1000)
    max_price = search.get("max_price") or 0
    bands = list(search.get("price_bands") or [])
    seen_ids = set(seen_ids or ())
    slug = region.get("slug") or region.get("id")
    rname = region.get("name") or slug

    if mode == "new":
        seen = {}
        _scan_pages(fetcher, slug, rname, max_price, None, None,
                    search.get("new_pages_per_region", 4), seen, verbose)
        return list(seen.values()), False

    # Zoopla does not expose a result count in the JSON-LD; probe page 1
    seen = {}
    truncated = _scan_pages(fetcher, slug, rname, max_price, None, None, 1,
                            seen, verbose, probe=True)
    total = len(seen)
    if total >= 20 and bands:
        # assume a big region: split by price bands
        queries = ([(bands[i], bands[i + 1] - 1) for i in range(len(bands) - 1)]
                   + [(bands[-1], None)]) if not max_price else \
                  [(bands[i], bands[i + 1] - 1)
                   for i in range(len(bands) - 1) if bands[i] < max_price]
        for lo, hi in queries:
            _scan_pages(fetcher, slug, rname, max_price, lo, hi, max_pages,
                        seen, verbose)
    else:
        _scan_pages(fetcher, slug, rname, max_price, None, None, max_pages,
                    seen, verbose)
    return list(seen.values()), truncated


def _scan_pages(fetcher, slug, rname, max_price, min_price, max_band, max_pages,
                seen, verbose, probe=False):
    truncated = False
    empty_streak = 0
    for page in range(1, max_pages + 1):
        url = search_url(slug, page=page,
                         max_price=max_band if max_band is not None else max_price,
                         min_price=min_price)
        try:
            r = fetcher.get(url)
        except Exception as e:
            if verbose:
                print(f"  zoopla {slug} p{page}: {e}")
            truncated = True
            break
        items = search_items(r.text)
        if not items:
            empty_streak += 1
            if empty_streak >= 2 or probe:
                break
            continue
        empty_streak = 0
        new = 0
        for item in items:
            row = normalize(item, slug, rname)
            if row["uid"] and row["uid"] not in seen:
                seen[row["uid"]] = row
                new += 1
        if verbose:
            print(f"  zoopla {slug} p{page}: {len(items)} items ({new} new)")
        if new == 0:
            break
        if probe:
            break
        if page == max_pages:
            truncated = True
    return truncated


def discover(fetcher, slugs=None, min_count=50, verbose=False):
    """Probe slugs against Zoopla and keep the ones with inventory."""
    import pathlib

    from .http import DATA_DIR
    from .regions import load_regions

    if slugs is None:
        slugs = [r.get("slug", "").lower() for r in load_regions() if r.get("slug")]
    out = []
    for i, slug in enumerate(slugs, 1):
        try:
            r = fetcher.get(search_url(slug))
            items = search_items(r.text)
        except Exception:
            continue
        if len(items) >= min_count // 20:
            out.append({"slug": slug, "name": slug.replace("-", " ").title(),
                        "id": slug, "count_all": len(items) * 20})
            if verbose:
                print(f"  {i}/{len(slugs)} {slug}: ok")
        elif verbose:
            print(f"  {i}/{len(slugs)} {slug}: skip")
    p = DATA_DIR.parent / "data" / "regions_zoopla.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def enrich(fetcher, listing, cfg, prev_acres=None, verbose=False):
    """Parse acreage from the detail page JSON-LD description."""
    texts = [listing.get("summary") or ""] + (listing.get("features") or [])
    parsed = parse_acres("\n".join(texts))
    needs_detail = (
        parsed is None and prev_acres is None
        and (has_land_keyword("\n".join(texts))
             or (listing.get("subtype") or "") in ("land", "plot", "farm", "cottage"))
    )
    if needs_detail:
        try:
            r = fetcher.get(listing["url"])
            node = detail_node(r.text)
            if node:
                desc = node.get("description") or ""
                listing["description"] = desc
                parsed = parse_acres(desc)
        except Exception as e:
            if verbose:
                print(f"  zoopla detail {listing.get('uid')}: {e}")
        listing["detail_checked"] = True
    if parsed:
        a_min, a_max, a_mid, unit, conf, matched, cands, communal = parsed
        listing.update({"acres_min": a_min, "acres_max": a_max, "acres_mid": a_mid,
                        "acre_unit": unit, "confidence": conf, "matched": matched,
                        "candidates": cands, "communal": communal})
        if a_mid is not None and (listing.get("subtype") or "") in ("land", "plot"):
            listing["land_only"] = True
    return listing
