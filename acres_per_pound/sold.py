"""Land Registry Price Paid matching for sold/removed listings.

When a listing drops out of the active state (detected by the weekly
reconcile scans) it is usually sold. We join it against HM Land
Registry Price Paid data (England & Wales, free CSV) by postcode,
asking price and date window, producing sold_price / sold_date and a
sold £/acre figure.

Files: pp-2026.csv (year to date, refreshed monthly) plus pp-2025.csv
for older removals. Download URLs are stable:
  https://price-paid-data.publicdata.landregistry.gov.uk/pp-<year>.csv
"""

import csv
import datetime
import pathlib
import re

import httpx

from .http import DATA_DIR, load_config

PPD_BASE = "https://price-paid-data.publicdata.landregistry.gov.uk"
PPD_DIR = DATA_DIR.parent / "data" / "ppd"


def fetch_ppd(years=(2026, 2025), force=False, verbose=False):
    """Download PPD year CSVs, return list of row dicts (price/date/postcode)."""
    PPD_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for year in years:
        out = PPD_DIR / f"pp-{year}.csv"
        if not out.exists() or force:
            url = f"{PPD_BASE}/pp-{year}.csv"
            if verbose:
                print(f"  downloading {url} ...")
            r = httpx.get(url, headers={"User-Agent": load_config()["http"]["user_agent"]},
                          timeout=600, follow_redirects=True)
            r.raise_for_status()
            out.with_suffix(".part").write_bytes(r.content)
            out.with_suffix(".part").replace(out)
        with open(out, encoding="utf-8", errors="replace") as f:
            for rec in csv.reader(f):
                if len(rec) < 13 or not rec[1]:
                    continue
                try:
                    price = int(rec[1])
                    date = rec[2]
                except ValueError:
                    continue
                if price <= 0:
                    continue
                rows.append({
                    "price": price,
                    "date": date,
                    "postcode": (rec[3] or "").upper().replace(" ", ""),
                    "ptype": rec[4],
                    "street": f"{rec[7]} {rec[8]} {rec[9]}".strip(),
                })
    return rows


_SCOT_NI_PREFIXES = {
    "AB", "BT", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY",
    "ML", "PA", "PH", "TD", "ZE",
}


def _postcode_key(pc):
    return (pc or "").upper().replace(" ", "")


def _street_key(text):
    """Normalize an address/street string for fuzzy matching."""
    t = (text or "").upper()
    t = re.sub(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d?[A-Z]{2}\b", " ", t)
    t = re.sub(r"\d", " ", t)
    t = re.sub(r"[^A-Z ]", " ", t)
    parts = [p for p in t.split() if len(p) > 2]
    return " ".join(parts[:3]) if parts else ""


def match_removed(listings, ppd_rows, verbose=False):
    """Attach sold info to inactive listings. Mutates listings in place.

    Matches by full postcode, outcode prefix, then street-name fallback.
    Returns (matched, attempted).
    """
    by_pc = {}
    by_out = {}
    by_street = {}
    for rec in ppd_rows:
        pc = rec["postcode"]
        by_pc.setdefault(pc, []).append(rec)
        pref = pc[:3] if len(pc) > 2 and pc[1].isdigit() and len(pc) == 3 else pc[:4]
        by_out.setdefault(pref, []).append(rec)
        sk = _street_key(rec["street"])
        if sk:
            by_street.setdefault(sk, []).append(rec)

    matched = attempted = 0
    for rm_id, row in listings.items():
        if row.get("active") is not False:
            continue
        if row.get("sold_price"):
            continue
        attempted += 1
        pc = _postcode_key(row.get("postcode") or "")
        if pc[:2] in _SCOT_NI_PREFIXES or (pc[:1] in ("G",) and pc[1].isdigit()):
            continue  # no PPD coverage
        ask = row.get("price") or 0
        last_seen = row.get("last_seen") or ""
        try:
            seen = datetime.date.fromisoformat(last_seen[:10])
        except ValueError:
            seen = None
        cands = []
        if len(pc) >= 6:
            cands += by_pc.get(pc, [])
            cands += by_out.get(pc[:4], [])
        elif pc:
            cands += by_out.get(pc, [])
        sk = _street_key(row.get("address") or "")
        if sk:
            cands += by_street.get(sk, [])
        best = None
        best_score = 0.0
        seen_keys = set()
        for rec in cands:
            key = (rec["postcode"], rec["price"], rec["date"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            try:
                d = datetime.date.fromisoformat(rec["date"][:10])
            except ValueError:
                continue
            delta = None
            if seen:
                delta = (d - seen).days
                if delta < -120 or delta > 330:
                    continue
            lo, hi = 0.35, 1.6
            if not pc:
                lo, hi = 0.6, 1.4
            if ask and not (lo * ask <= rec["price"] <= hi * ask):
                continue
            score = 0.0
            if pc and rec["postcode"] == pc:
                score += 2.5
            elif pc:
                score += 1.2
            if ask:
                ratio = rec["price"] / ask
                score += max(0.0, 1.0 - abs(ratio - 1.0))
            if seen and delta is not None:
                score += max(0.0, 1.0 - abs(delta) / 250.0)
            if score > best_score:
                best_score = score
                best = rec
        if best and best_score >= 1.5:
            row["sold_price"] = best["price"]
            row["sold_date"] = best["date"]
            if pc and best["postcode"] == pc:
                row["sold_confidence"] = "strong"
            elif pc:
                row["sold_confidence"] = "weak"
            else:
                row["sold_confidence"] = "street"
            if row.get("acres_mid"):
                row["sold_gbp_per_acre"] = round(best["price"] / row["acres_mid"], 2)
            matched += 1
            if verbose:
                print(f"  {rm_id} {row.get('address', '')[:40]:40} ask {ask:>8} -> sold {best['price']:>8} "
                      f"({best['date']}) [{row['sold_confidence']}]")
    return matched, attempted
