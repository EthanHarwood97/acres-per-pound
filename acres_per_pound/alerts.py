"""Console + webhook (ntfy) alerts for new high-value listings."""

import json
import logging

import httpx

log = logging.getLogger("acres")


def _top_new(listings, events, cfg, top_n):
    alert_cfg = cfg.get("alerts", {})
    min_acres = alert_cfg.get("min_acres", 0.5)
    top_n = alert_cfg.get("top_n", top_n)
    new_ids = {e["rm_id"] for e in events if e["event"] in ("new", "reduced")}
    rows = [
        r for r in listings.values()
        if r.get("rm_id") in new_ids
        and (r.get("acres_mid") or 0) >= min_acres
        and r.get("gbp_per_acre") is not None
    ]
    rows.sort(key=lambda r: r["gbp_per_acre"])
    return rows[:top_n]


def console_banner(rows):
    if not rows:
        return
    print("\n" + "=" * 70)
    print("  BEST LAND VALUE - NEW / REDUCED")
    print("=" * 70)
    for r in rows:
        print(f"  {r['gbp_per_acre']:>10,.0f} GBP/acre  {r['acres_mid']:>7} ac  "
              f"{r['price_text']:>16}  {r['address'][:45]}")
        print(f"             {r['url']}")
    print("=" * 70 + "\n")


def notify(rows, cfg):
    alert_cfg = cfg.get("alerts", {})
    webhook = alert_cfg.get("webhook_url")
    if not webhook or not rows:
        return
    lines = [f"{r['gbp_per_acre']:,.0f} GBP/acre | {r['acres_mid']} ac | {r['price_text']} | {r['address']}"
             for r in rows[:5]]
    message = "\n".join(lines)
    try:
        httpx.post(webhook, timeout=10,
                   data=json.dumps({"title": "acres-per-pound: new land value",
                                    "message": message, "priority": 4}),
                   headers={"Content-Type": "application/json"})
    except Exception as e:
        log.warning("webhook failed: %s", e)
