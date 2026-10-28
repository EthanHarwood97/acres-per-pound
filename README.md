# acres-per-pound

Find UK property with the most land — and the best value per square
foot — for the money. Scans **Rightmove, OnTheMarket and Zoopla**,
measures every listing's **interior floor area** (EPC) and **registered
plot area** (HM Land Registry / Registers of Scotland), and ranks the
market by **£/interior sq ft**, **£/land sq ft** and **£/acre**.

Two surfaces:

- **`web/`** — the new app: a data-dense register with server-side
  search, sort, filters and a clustered map (Vite + React + MapLibre).
- **`docs/`** — the original static dashboard (still generated).

The pipeline is Python. Everything runs for free: GitHub Actions for
Rightmove/OnTheMarket, a local scheduled task for Zoopla (Cloudflare
blocks datacenter IPs), Cloudflare Workers + D1 + R2 for the app.

---

## Quick start

```powershell
pip install -r requirements.txt
pip install -r requirements-enrich.txt      # shapely, pyproj, py7zr, playwright
python -m playwright install chromium

python -m acres_per_pound.cli store --import-state   # migrate legacy state.json (one-off)
python -m acres_per_pound.cli regions                # Rightmove region ids
python -m acres_per_pound.cli regions --otm          # OnTheMarket region slugs
python -m acres_per_pound.cli run-once --limit 5     # pilot scan + ranking

python -m acres_per_pound.cli serve                  # API + app on http://127.0.0.1:8138
```

The app needs a frontend build first:

```powershell
cd web; npm install; npm run build; cd ..
python -m acres_per_pound.cli serve --quick
```

---

## The numbers

For every listing the pipeline derives:

| Metric | Source | Column |
|---|---|---|
| **Interior sq ft** | EPC `TOTAL_FLOOR_AREA` (matched by UPRN, else postcode) | `internal_sqft` |
| | fallback: portal-stated floor size | `internal_src = portal` |
| **Land sq ft** | registered plot polygon (INSPIRE E&W / RoS Scotland) | `plot_sqft` |
| | fallback: acreage parsed from the listing text | `acres` |
| **£/interior sq ft** | `price ÷ internal_sqft` | `gbp_in` |
| **£/land sq ft** | `price ÷ plot_sqft` | `gbp_ex` |
| **£/acre** | `price ÷ acres_eff` | `gbp_ac` |

`acres_eff` is the one acreage the app shows and sorts on: the stated
acreage when it is credible, otherwise the registered plot. A registered
plot is the **gross title area** — house footprint, garden and driveway
all included — so £/acre and £/land-sqft are site values, not garden
values. They understate what you pay per usable acre when a large house
sits on the plot.

Listings priced below £10,000 (POA placeholders) get no value ratios.

---

## Data sources

| Source | Coverage | Access |
|---|---|---|
| Rightmove search/detail JSON | UK | HTTP (`__NEXT_DATA__`) |
| OnTheMarket search/detail JSON | UK | **disabled** — page no longer exposes `initialReduxState` |
| Zoopla JSON-LD (incl. `floorSize`) | UK | Playwright (local only) |
| EPC England & Wales | E&W | bulk CSV, **GOV.UK One Login required** |
| EPC Scotland | Scotland | free quarterly extract (`statistics.gov.scot`) |
| OS Open UPRN (lat/lng → UPRN) | GB | free download, no auth |
| HM Land Registry INSPIRE polygons | E&W | free download, no auth |
| Registers of Scotland cadastral parcels | Scotland | free download (partial coverage) |
| Land Registry Price Paid | E&W | free CSV |
| postcodes.io reverse geocoding | UK | free API, no key |

### EPC (interior floor area)

Scotland downloads itself (`acres epc --download scotland`). **England
& Wales requires a one-time manual download** because the service sits
behind GOV.UK One Login:

1. Register free at <https://get-energy-performance-data.communities.gov.uk>
2. Choose **Domestic Energy Performance Certificates**, CSV, whole of
   England & Wales (the "all domestic" zip is ~7.7 GB and contains 19
   `certificates-YYYY.csv` files plus much larger recommendation files
   we never read).
3. Drop the zip into `data/epc/ew/`.
4. Run `acres epc --load --match`.

The loader streams `certificates-*.csv` members straight out of the zip
(no 28 GB extraction) and keeps only the columns it needs, so the
~30M-row extract lands as a few GB of index.

Matching order:

1. **UPRN** — listing lat/lng → nearest OS Open UPRN point (≤60 m) →
   EPC certificate carrying that UPRN. Exact, and the path most E&W
   properties take (97% of E&W certificates carry a UPRN).
2. **Postcode + house number** — full postcode plus street/number
   tokens with a floor-area sanity check. Used for Scotland (its
   extract has no UPRN).

Land listings are never given an interior size, and flats are out of
scope entirely (they are excluded from the product set, not just the
ranking).

### Plot (land) area

- **England & Wales**: `acres enrich --all` downloads and parses every
  INSPIRE local authority (~4 GB, one-off), point-matches each listing
  to its registered freehold polygon, and stores the area. Large
  shared titles (farms/estates a pin happens to land in) are flagged
  `est_shared` and hidden from the land columns.
- **Scotland**: `acres ros --all` pulls the 33 Registers of Scotland
  registration-county parcels from the RoS INSPIRE download service
  (`/maps/download/ros-cp.cadastralparcel/<CODE>`), caches each as a
  polygon index, and matches the same way. Coverage is partial by
  design: only Land Register titles are mapped, not the older Sasines
  register.
- **Northern Ireland**: no free EPC or boundary data exists. NI
  listings carry portal-stated sizes only, and the app says so.

### Keeping the land figures honest

Registered boundaries are only useful if they describe *this* property.
Three passes filter the ways they don't:

1. **Shared titles** — the same polygon matched to listings at two
   different coordinates is a developer or communal title (a whole
   street's land), not one house's garden. Duplicate listings for the
   same property share a coordinate, so they don't count as reuse.
2. **Stated acreage corroboration** — when the ad's own figure agrees
   with the polygon (within 0.5x–2x), the title is accepted at any size:
   a farmhouse on 200 acres is a listing, not an error. This is what
   stops the size ceilings hiding the properties the site exists to
   find.
3. **Uncorroborated size** — without a corroborating figure, per-type
   ceilings still apply (terraced 0.5 ac, semi 1.0 ac, detached 6.0 ac),
   and any polygon over 50 acres is treated as a registration or estate
   parcel. Scottish RoS parcels are especially coarse, so this pass does
   most of its work there.

Stated acreage is also rejected when it describes the wider estate: an
ad claiming 50+ acres at 20x the registered polygon ("the island extends
to 2,948 hectares" on a quarter-acre croft) is marketing copy, not the
plot. `landparse._COMMUNAL` catches the common phrasings; this ratio
check catches the rest.

Flagged rows fall back to any credible stated acreage, and are hidden
otherwise. Coverage is reported over **dwellings** — land and plots never
have an EPC, so counting them would understate interior coverage.

### Comparing £/acre

£/acre is meaningless for a terraced house on a 0.01-acre garden
(£20m/acre), and it falls sharply as plot size rises. So the regional
median and the per-listing `value_ratio` are computed only over
land-bearing listings (0.5 acres or more) within the same **size band**
and the same land/house split, falling back to the national band median
when a regional cell is thin. Above 1.0 means cheaper than comparable
local land. A 350-acre hill farm is never compared with half-acre
building plots.

---

## Commands

```
regions [--otm]            discover region ids (Rightmove sitemaps / OTM probe)
run-once                   scan every enabled source once, print the ranking
publish                    scan + write snapshots + build the static docs/ site
scrape-region ID           debug one region (--source rightmove|onthemarket)
enrich --all | --las A.zip match listings to registered plots (INSPIRE, E&W)
enrich --refresh              re-download the polygon sets + retry unmatched (monthly)
geoms                         store boundaries for already-matched plots (one-off)
ros --all | --codes ABN,ANG     match Scottish plots (Registers of Scotland)
epc --download|--load|--match|--stats    interior floor areas
geocode [--limit N]        fill full postcodes from lat/lng (postcodes.io)
reparse [--limit N]        re-read acreage from detail pages (resumable)
zoopla [--discover]        scan Zoopla locally via Chromium
sold [--years 2026,2025]   Land Registry Price Paid join for removed listings
layers                     map layers (airports, crime, flood, GPs, parks, schools)
store --import-state|--recompute|--export-state   canonical SQLite maintenance
sync --all [--full]        publish to Cloudflare D1 (search index) + R2 (snapshot)
serve [--quick]            local API + app
```

`store --export-state` writes the enriched store back to
`snapshots/state.json`. That file is the portable carrier of enrichment
between machines and CI: the SQLite store holds multi-gigabyte EPC/UPRN
indexes and stays out of git, while CI reads the previous state,
re-scans, merges the enrichment fields forward and pushes to D1.

---

## Cloudflare deployment

The search API runs on a Worker reading D1; R2 keeps the full snapshot.

```powershell
# 1. credentials (once) - create a token with D1 Edit + R2 Edit
"CLOUDFLARE_API_TOKEN=..."  | Out-File .env -Encoding utf8
"CLOUDFLARE_ACCOUNT_ID=..." | Out-File .env -Append -Encoding utf8

# 2. publish the data
python -m acres_per_pound.cli sync --all --full

# 3. deploy the Worker + app
cd worker
npx wrangler deploy
```

Already provisioned on this account:

- D1 database `acres-per-pound` (`ee23e644-83e3-40b4-9fdd-56e6f1acaaf0`)
- R2 bucket `acres-per-pound`
- Worker `acres-per-pound` (serves `web/dist` + `/api/*`)

### API

| Endpoint | Notes |
|---|---|
| `GET /api/search` | filters, sort, pagination (`limit`, `offset`, `count=1`) |
| `GET /api/map` | slim points for the current filters |
| `GET /api/listing/:uid` | one listing |
| `GET /api/regions` | region facet table |
| `GET /api/stats` | coverage + freshness |

Filters: `q`, `price_min/max`, `in_sqft_min/max`, `ex_sqft_min/max`,
`acres_min/max`, `beds_min/max`, `gbp_in_min/max`, `gbp_ex_min/max`,
`gbp_ac_min/max`, `type`, `source`, `country`, `region`, `land_only`,
`has_in`, `has_ex`, `no_highlands`, `lat`+`lng`+`radius_mi`, `bbox`.
Sorts: `gbp_in`, `gbp_ex`, `gbp_ac`, `price`, `in_sqft`, `ex_sqft`,
`acres`, `beds`, `published`, `last_seen`.

---

## Scheduling

| Job | Where | Cadence |
|---|---|---|
| Rightmove + OnTheMarket scan | GitHub Actions | 2×/day |
| Zoopla scan | local Windows task (`acres zoopla`) | daily |
| EPC refresh (Scotland monthly, E&W quarterly) | local / Actions | monthly |
| INSPIRE + RoS refresh | local | monthly |
| Price Paid sold join | Actions | weekly |
| `sync --all` | after each scan | — |

D1 free tier allows 100k row writes/day and 500 MB per database; the
diff-based sync only writes changed rows. At full UK scale (~500k
listings) the Workers Paid plan ($5/mo) removes the daily limits and
raises the database to 10 GB.

---

## Layout

```
acres_per_pound/
  store.py      canonical SQLite: listings + epc + uprn + metrics
  engine.py     multi-source cycle: scan -> enrich -> diff -> events
  rightmove.py  Rightmove source
  otm.py        OnTheMarket source
  zoopla.py     Zoopla source (Playwright BrowserFetcher)
  epc.py        EPC download/index/match (E&W + Scotland)
  geocode.py    postcodes.io reverse geocoding
  inspire.py    INSPIRE polygon download/parse/match (E&W)
  sold.py       Land Registry Price Paid matching
  layers.py     map layers (airports, crime, flood, GPs, parks, schools)
  api.py        local FastAPI mirror of the Worker API
  sync.py       D1 + R2 publishing
  publish.py    legacy static site builder
  cli.py        command line
web/            new app (Vite + React + MapLibre)
worker/         Cloudflare Worker (API + static assets)
docs/           legacy static site (GitHub Pages)
snapshots/      state.json + events.jsonl
tests/          parser unit tests
```

## Caveats

- Rightmove, OnTheMarket and Zoopla terms prohibit scraping. Keep rates
  low; personal research use; the site publishes derived data only.
- Acreage parsed from listing text is a hint, not a survey — the
  `confidence` and `matched` fields show where each figure came from.
- Scotland's plot coverage is partial (Land Register only); Northern
  Ireland has no free plot or EPC data.
- Always confirm sizes with the agent before acting.
