export interface Listing {
  uid: string;
  source: string;
  url: string | null;
  address: string | null;
  postcode: string | null;
  outcode: string | null;
  lat: number | null;
  lng: number | null;
  price: number | null;
  beds: number | null;
  baths: number | null;
  type: string | null;
  tenure: string | null;
  land_only: number;
  in_sqm: number | null;
  in_sqft: number | null;
  in_src: string | null;
  in_conf: string | null;
  ex_sqm: number | null;
  ex_sqft: number | null;
  ex_src: string | null;
  ex_conf: string | null;
  acres: number | null;
  acres_src: string | null;
  epc: string | null;
  epc_date: string | null;
  gbp_ac: number | null;
  gbp_in: number | null;
  gbp_ex: number | null;
  /** region median £/acre; value_ratio = median / this listing's £/acre */
  region_median: number | null;
  value_ratio: number | null;
  plot_id: string | null;
  region: string | null;
  country: string | null;
  highlands: number;
  first_seen: string | null;
  last_seen: string | null;
  status: string | null;
  published: string | null;
  sold_price: number | null;
  sold_date: string | null;
}

export interface RegionFacet {
  region: string;
  country: string | null;
  highlands: number;
  n: number;
  med_gbp_ac: number | null;
  med_gbp_in: number | null;
  med_gbp_ex: number | null;
  med_acres: number | null;
  med_in_sqft: number | null;
  med_ex_sqft: number | null;
}

/** Simplified registered title boundary, WGS84 (GeoJSON). */
export interface PlotPolygon {
  type: "Polygon";
  coordinates: number[][][];
}

export interface Stats {
  n: number;
  with_in: number;
  with_ex: number;
  with_acres: number;
  rightmove: number;
  onthemarket: number;
  zoopla: number;
  min_price: number | null;
  max_price: number | null;
  last_seen: string | null;
  /** coverage is measured over dwellings - land never has an EPC */
  dwellings?: number;
  dwellings_with_in?: number;
  dwellings_with_ex?: number;
}

export type SortKey =
  | "gbp_in"
  | "gbp_ex"
  | "gbp_ac"
  | "price"
  | "in_sqft"
  | "ex_sqft"
  | "acres"
  | "beds"
  | "published"
  | "last_seen";

/** Every filter key that may appear in the URL. */
export const FILTER_KEYS = [
  "q", "sort", "dir",
  "price_min", "price_max", "in_sqft_min", "in_sqft_max",
  "ex_sqft_min", "ex_sqft_max", "acres_min", "acres_max",
  "beds_min", "beds_max",
  "gbp_in_min", "gbp_in_max", "gbp_ex_min", "gbp_ex_max",
  "gbp_ac_min", "gbp_ac_max",
  "type", "source", "country", "region", "land_only",
  "has_in", "has_ex", "no_highlands",
  "lat", "lng", "radius_mi", "bbox",
] as const;

export interface Filters {
  q: string;
  sort: SortKey;
  dir: "asc" | "desc";
  price_min?: number;
  price_max?: number;
  in_sqft_min?: number;
  in_sqft_max?: number;
  ex_sqft_min?: number;
  ex_sqft_max?: number;
  acres_min?: number;
  acres_max?: number;
  beds_min?: number;
  beds_max?: number;
  gbp_in_min?: number;
  gbp_in_max?: number;
  gbp_ex_min?: number;
  gbp_ex_max?: number;
  gbp_ac_min?: number;
  gbp_ac_max?: number;
  type?: string;
  source?: string;
  country?: string;
  region?: string;
  land_only?: "1" | "0" | "";
  has_in?: "1" | "";
  has_ex?: "1" | "";
  no_highlands?: "1" | "";
  lat?: number;
  lng?: number;
  radius_mi?: number;
  bbox?: string;
}
