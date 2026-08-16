# acres-per-pound

Find UK property with the most land for the money. Scrapes Rightmove
UK-wide, parses stated land size from listing text, and ranks every
listing by **pounds per acre** — with a toggle between bare land and
houses with land.

Inspired by the architecture of [ai-model-tracker](https://github.com/EthanHarwood97/ai-model-tracker):
run for free on GitHub Actions, published as a static site on GitHub Pages.

## How it works

1. **Discover regions** — Rightmove's own sitemaps reveal every UK
   region/town slug; each SEO search page embeds its internal
   `REGION^<id>`. Cached in `data/regions.json`.
2. **Scan** — for each region, paginate the public search results and
   read the embedded `__NEXT_DATA__` JSON (price, address, lat/lng,
   key features, summary). Price-banded when a region exceeds the
   ~1000-result query cap.
3. **Parse land size** — `landparse.py` mines the listing text:
   acres, hectares, fractions, ranges, "just over", "stms" etc.
   Detail pages (full descriptions) are fetched only when the search
   text hints at land but yields no figure — and only once per listing.
4. **Rank** — `£/acre = price ÷ acres`, cross-checked on every cycle;
   NEW / reduced / removed events appended to `snapshots/events.jsonl`.
5. **Publish** — `docs/` static site (vanilla JS) + `snapshots/state.json`,
   committed by the workflow every run.

## Running online (GitHub Actions + Pages, $0)

- `.github/workflows/find.yml` runs `python -m acres_per_pound.cli publish`
  twice a day on free runners and commits the site + state.
- Enable GitHub Pages on the repo, root `/docs`, branch `main` →
  `https://<you>.github.io/acres-per-pound/`.
- Push alerts (ntfy): set `alerts.webhook_url` in `config.json` to your
  ntfy topic; new high-value listings get pushed.

Manual trigger: Actions tab → Run workflow.

## Local usage

```powershell
pip install -r requirements.txt
python -m acres_per_pound.cli regions            # one-time discovery (~10 min)
python -m acres_per_pound.cli run-once --limit 5 # pilot scan, top 25 table
python -m acres_per_pound.cli scrape-region 475  # debug one region
python -m acres_per_pound.cli serve              # dashboard on http://127.0.0.1:8138
python tests\test_landparse.py                   # parser unit tests
```

## Map layers (`python -m acres_per_pound.cli layers`)

Family-suitability overlays, all free data, run monthly:

| Layer | Source | Notes |
|---|---|---|
| ✈ Airports | OurAirports | 48 UK scheduled airports + 15km noise rings |
| 🔥 Crime | data.police.uk | street-level, England/Wales/NI only |
| 🌊 Flood zone 3 | Environment Agency | rivers & sea, England only |
| 🏥 GP surgeries | CQC directory | geocoded via postcodes.io |
| 🌳 Parks | OpenStreetMap GB | top 3000 by area (~1.4GB one-time download) |
| 🏫 Schools + Ofsted | GIAS | needs a one-time manual download (below) |

**Schools setup (the only manual step):** register a free account at
https://get-information-schools.service.gov.uk → sign in → Downloads →
download the "Establishments" CSV (all fields) and save it as
`data/layers/establishments.csv`. The `layers` command picks it up and
geocodes every school with its Ofsted rating.

```powershell
pip install -r requirements-enrich.txt
 python -m acres_per_pound.cli layers
 ```

## Plot-boundary enrichment (INSPIRE)

Houses whose ads never mention land (the "semi with a huge garden" case)
can be measured against HM Land Registry's free **INSPIRE Index Polygons**
(registered freehold plot boundaries, England & Wales):

```powershell
pip install -r requirements-enrich.txt
python -m acres_per_pound.cli enrich --all   # download+parse all E&W LAs (~4GB, one-off)
# or a subset:
python -m acres_per_pound.cli enrich --las Cornwall_Council.zip,Devon.zip
```

Matched listings get `est_acres` / `est_plot_m2` / `inspire_id` and join
the ranking with `confidence=est`. Matches over the house-size threshold
or with vague pins are flagged `est_shared` and excluded from the ranking.
Run it monthly to catch new listings. Scotland/NI have no equivalent
free dataset - those listings are skipped.

## Config knobs (`config.json`)

- `search.max_price` — price ceiling (default £300k)
- `search.price_bands` — how oversized regions are split
- `search.delay_sec` — politeness delay between requests
- `regions.enabled` — restrict to a subset (empty = all UK)
- `alerts.webhook_url` / `alerts.min_acres` / `alerts.top_n`

## Caveats

- Rightmove's ToS prohibit scraping — low rate, personal research use.
- Acreage is parsed from free text (`confidence` + `matched` show what it
  came from) or estimated from registered plot boundaries (`confidence=est`).
  Always confirm with the agent.
- The site publishes derived data only (price, acreage, link) — not
  listing descriptions.

## Layout

```
acres_per_pound/
  http.py       httpx fetcher: disk cache, retry/backoff, rate limit
  rmjson.py     __NEXT_DATA__ / PAGE_MODEL JSON extraction (brace-match + graph decode)
  regions.py    sitemap -> region id discovery
  rightmove.py  search scan + detail enrichment + normalization
  landparse.py  free-text acreage parser (unit-tested)
  inspire.py    INSPIRE polygon download/parse/match (local enrich)
  engine.py     cycle: scan -> diff -> parse -> score -> events
  publish.py    state.json + events.jsonl + docs/ site builder
  alerts.py     console + ntfy push
  cli.py        regions / run-once / publish / scrape-region / enrich / serve
static/         dashboard (vanilla JS)
snapshots/      state.json + events.jsonl (committed; git history = time machine)
docs/           GitHub Pages site (generated)
tests/          landparse corpus
```
