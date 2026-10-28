import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { listing as fetchListing, mapPoints, regions, search, stats, toQuery } from "./api";
import type { Filters, Listing, PlotPolygon, RegionFacet, SortKey, Stats } from "./types";
import { FILTER_KEYS } from "./types";
import { FilterBar } from "./components/FilterBar";
import { Header } from "./components/Header";
import { Ledger } from "./components/Ledger";
import { Detail } from "./components/Detail";

const MapView = lazy(() =>
  import("./components/MapView").then((m) => ({ default: m.MapView })),
);

// A land-first product ranks by land value first. £/acre is populated for
// every listing with a measured plot, not just the ones the ad mentions.
const DEFAULT_FILTERS: Filters = {
  q: "",
  sort: "gbp_ac",
  dir: "asc",
};

function readFilters(): Filters {
  const p = new URLSearchParams(window.location.search);
  const f: Filters = { ...DEFAULT_FILTERS };
  const known = new Set<string>(FILTER_KEYS);
  for (const [k, v] of p.entries()) {
    // bbox is a transient map-view filter, never restored from the URL
    if (known.has(k) && k !== "bbox") (f as unknown as Record<string, string>)[k] = v;
  }
  if (!f.sort) f.sort = "gbp_ac";
  if (!f.dir) f.dir = "asc";
  return f;
}

export default function App() {
  const [filters, setFilters] = useState<Filters>(readFilters);
  const [rows, setRows] = useState<Listing[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [points, setPoints] = useState<Listing[]>([]);
  const [facets, setFacets] = useState<RegionFacet[]>([]);
  const [siteStats, setSiteStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<Listing | null>(null);
  const [plot, setPlot] = useState<PlotPolygon | null>(null);
  const [loading, setLoading] = useState(false);
  const [mapOpen, setMapOpen] = useState(true);
  const [bbox, setBbox] = useState<string>("");
  const [syncMap, setSyncMap] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    stats().then((d) => setSiteStats(d.stats)).catch(() => {});
    regions().then((d) => setFacets(d.regions)).catch(() => {});
  }, []);

  const effective = useMemo<Filters>(() => {
    const f = { ...filters };
    if (syncMap && bbox) f.bbox = bbox;
    return f;
  }, [filters, bbox, syncMap]);

  useEffect(() => {
    const p = new URLSearchParams(toQuery(effective));
    // map position and its bounding box are session state, not shareable
    // filters - keeping them out of the URL stops a stale map view from
    // silently narrowing every later search
    p.delete("lat");
    p.delete("lng");
    p.delete("bbox");
    window.history.replaceState(null, "", `?${p.toString()}`);
  }, [effective]);

  // turning the map filter off must also clear the box it had applied
  useEffect(() => {
    if (!syncMap) setBbox("");
  }, [syncMap]);

  useEffect(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    const t = window.setTimeout(() => {
      search(effective, { limit: 200, count: true, signal: ctrl.signal })
        .then((d) => {
          setRows(d.rows);
          setTotal(d.total);
          setLoading(false);
        })
        .catch((e) => {
          if ((e as Error).name !== "AbortError") setLoading(false);
        });
      mapPoints(effective, 20000, ctrl.signal)
        .then((d) => setPoints(d.points))
        .catch(() => {});
    }, 220);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [effective]);

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));

  const onSort = (key: SortKey) => {
    if (filters.sort === key) {
      set({ dir: filters.dir === "asc" ? "desc" : "asc" });
    } else {
      set({ sort: key, dir: "asc" });
    }
  };

  const wantedRef = useRef<string | null>(null);
  const open = (l: Listing | null) => {
    if (!l) {
      wantedRef.current = null;
      setSelected(null);
      setPlot(null);
      return;
    }
    wantedRef.current = l.uid;
    setSelected(l);
    setPlot(null);
    if (!l.uid) return;
    fetchListing(l.uid)
      .then((d) => {
        if (wantedRef.current !== l.uid) return;
        setSelected(d.listing);
        setPlot((d.plot_geojson as PlotPolygon | null) ?? null);
      })
      .catch(() => {});
  };

  return (
    <div className="app">
      <Header stats={siteStats} total={total} onPreset={(p) => set(p)} />
      <FilterBar
        filters={filters}
        set={set}
        facets={facets}
        mapOpen={mapOpen}
        toggleMap={() => setMapOpen((v) => !v)}
        syncMap={syncMap}
        setSyncMap={setSyncMap}
        hasBbox={!!bbox}
      />
      <div className={`workspace ${mapOpen ? "" : "no-map"}`}>
        <Ledger
          rows={rows}
          total={total}
          loading={loading}
          filters={filters}
          onSort={onSort}
          onSelect={open}
          selected={selected?.uid}
          mapFilter={syncMap && !!bbox}
          onClearMapFilter={() => setSyncMap(false)}
        />
        {mapOpen && (
          <Suspense
            fallback={
              <aside className="mapwrap">
                <div className="map-loading">Loading map…</div>
              </aside>
            }
          >
            <MapView
              points={points}
              selected={selected}
              plot={plot}
              onSelect={open}
              onBounds={setBbox}
            />
          </Suspense>
        )}
      </div>
      {selected && <Detail listing={selected} onClose={() => open(null)} />}
    </div>
  );
}
