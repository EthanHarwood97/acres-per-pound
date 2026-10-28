import type { Filters, Listing, PlotPolygon, RegionFacet, Stats } from "./types";

const BASE = "";

export function toQuery(f: Filters, extra: Record<string, string | number> = {}): string {
  const p = new URLSearchParams();
  const put = (k: string, v: unknown) => {
    if (v === undefined || v === null || v === "") return;
    p.set(k, String(v));
  };
  put("q", f.q);
  put("sort", f.sort);
  put("dir", f.dir);
  for (const k of [
    "price_min", "price_max", "in_sqft_min", "in_sqft_max", "ex_sqft_min",
    "ex_sqft_max", "acres_min", "acres_max", "beds_min", "beds_max",
    "gbp_in_min", "gbp_in_max", "gbp_ex_min", "gbp_ex_max", "gbp_ac_min",
    "gbp_ac_max", "type", "source", "country", "region", "land_only",
    "has_in", "has_ex", "no_highlands", "lat", "lng", "radius_mi", "bbox",
  ] as const) {
    put(k, (f as unknown as Record<string, unknown>)[k]);
  }
  for (const [k, v] of Object.entries(extra)) put(k, v);
  return p.toString();
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { signal });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export interface SearchResult {
  rows: Listing[];
  total: number | null;
  limit: number;
  offset: number;
}

export function search(
  f: Filters,
  opts: { limit?: number; offset?: number; count?: boolean; signal?: AbortSignal } = {},
): Promise<SearchResult> {
  const extra: Record<string, string | number> = {
    limit: opts.limit ?? 100,
    offset: opts.offset ?? 0,
  };
  if (opts.count) extra.count = 1;
  return get<SearchResult>(`/api/search?${toQuery(f, extra)}`, opts.signal);
}

export function mapPoints(
  f: Filters,
  limit = 20000,
  signal?: AbortSignal,
): Promise<{ points: Listing[]; limit: number }> {
  return get(`/api/map?${toQuery(f, { limit })}`, signal);
}

export function regions(signal?: AbortSignal): Promise<{ regions: RegionFacet[] }> {
  return get("/api/regions", signal);
}

export function stats(signal?: AbortSignal): Promise<{ stats: Stats }> {
  return get("/api/stats", signal);
}

export function listing(
  uid: string,
  signal?: AbortSignal,
): Promise<{ listing: Listing; plot_geojson?: PlotPolygon | null }> {
  return get(`/api/listing/${encodeURIComponent(uid)}`, signal);
}

export const SOURCE_LABEL: Record<string, string> = {
  rightmove: "Rightmove",
  onthemarket: "OnTheMarket",
  zoopla: "Zoopla",
};

export const SOURCE_URL = (l: Listing): string =>
  l.url || (l.source === "rightmove"
    ? `https://www.rightmove.co.uk/properties/${l.uid.split(":")[1]}`
    : "#");

export const fmtMoney = (v: number | null | undefined, dp = 0): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : `£${v.toLocaleString("en-GB", { maximumFractionDigits: dp })}`;

export const fmtInt = (v: number | null | undefined): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString("en-GB", { maximumFractionDigits: 0 });

export const fmtNum = (v: number | null | undefined, dp = 1): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString("en-GB", { maximumFractionDigits: dp });

export function daysAgo(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const d = Math.max(0, Math.round((Date.now() - t) / 86400000));
  return d === 0 ? "today" : d === 1 ? "1d" : `${d}d`;
}
