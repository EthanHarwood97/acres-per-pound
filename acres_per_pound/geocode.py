"""Reverse-geocode listings to full postcodes via postcodes.io.

Rightmove/OTM search cards only carry an outcode ("BS16") or nothing at
all, but every listing has a lat/lng. postcodes.io offers free bulk
reverse geocoding (no key, 100 locations per request), which fills the
full postcode needed for:

  - Scottish EPC matching (that extract has no UPRN)
  - cross-portal dedupe keys
  - human-readable addresses

Rate: ~1000 requests covers the whole active set; run monthly for new
listings only.
"""

import re
import time

import httpx

from . import store as store_mod

API = "https://api.postcodes.io/postcodes"
BATCH = 100

_FULL_PC = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s\d[A-Z]{2}$")


def _has_full_pc(pc):
    return bool(_FULL_PC.match((pc or "").strip().upper()))


def fill_postcodes(conn, limit=None, verbose=False, delay=0.35):
    """Reverse-geocode listings without a full postcode. Returns count filled."""
    listings = [(uid, row) for uid, row in store_mod.listings_map(conn).items()
                if row.get("lat") and row.get("lng")
                and not _has_full_pc(row.get("postcode"))]
    if limit:
        listings = listings[:limit]
    if not listings:
        return 0
    if verbose:
        print(f"  {len(listings):,} listings need a full postcode")
    filled = 0
    updates = []
    with httpx.Client(timeout=60, headers={"User-Agent": "acres-per-pound research"}) as c:
        for i in range(0, len(listings), BATCH):
            chunk = listings[i:i + BATCH]
            payload = {"geolocations": [
                {"longitude": row["lng"], "latitude": row["lat"], "limit": 1}
                for _, row in chunk]}
            try:
                r = c.post(API, json=payload)
                if r.status_code != 200:
                    if verbose:
                        print(f"  batch {i//BATCH}: HTTP {r.status_code}")
                    time.sleep(2)
                    continue
                results = r.json().get("result") or []
            except Exception as e:
                if verbose:
                    print(f"  batch {i//BATCH} failed: {e}")
                time.sleep(2)
                continue
            for (uid, _row), res in zip(chunk, results):
                hit = (res or {}).get("result") or []
                if not hit:
                    continue
                pc = (hit[0].get("postcode") or "").strip().upper()
                if not pc:
                    continue
                updates.append((uid, {"postcode": pc,
                                      "outcode": pc.split(" ")[0]}))
                filled += 1
            if updates and (i // BATCH) % 10 == 0:
                store_mod.update_many(conn, updates)
                updates = []
            if verbose and (i // BATCH) % 25 == 0:
                print(f"    {i:,}/{len(listings):,} ({filled:,} filled)")
            time.sleep(delay)
    if updates:
        store_mod.update_many(conn, updates)
    return filled
