import { useState } from "react";
import type { Filters, RegionFacet, SortKey } from "../types";

interface Props {
  filters: Filters;
  set: (p: Partial<Filters>) => void;
  facets: RegionFacet[];
  mapOpen: boolean;
  toggleMap: () => void;
  syncMap: boolean;
  setSyncMap: (v: boolean) => void;
  hasBbox: boolean;
}

const SORTS: { key: SortKey; label: string }[] = [
  { key: "gbp_ac", label: "£ / acre" },
  { key: "acres", label: "Acres" },
  { key: "gbp_ex", label: "£ / land sq ft" },
  { key: "gbp_in", label: "£ / interior sq ft" },
  { key: "price", label: "Price" },
  { key: "in_sqft", label: "Interior sq ft" },
  { key: "ex_sqft", label: "Land sq ft" },
  { key: "beds", label: "Beds" },
  { key: "published", label: "Listed" },
];

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

export function FilterBar({
  filters,
  set,
  facets,
  mapOpen,
  toggleMap,
  syncMap,
  setSyncMap,
  hasBbox,
}: Props) {
  const [open, setOpen] = useState(false);
  const countries = Array.from(new Set(facets.map((r) => r.country).filter(Boolean))) as string[];
  const regionList = facets
    .filter((r) => !filters.country || r.country === filters.country)
    .sort((a, b) => (a.med_gbp_ac ?? 1e12) - (b.med_gbp_ac ?? 1e12));

  return (
    <section className="controls">
      <div className="controls-main">
        <div className="search-wrap">
          <span className="search-icon" aria-hidden>
            ⌕
          </span>
          <input
            className="search"
            type="search"
            placeholder="Search address, town or postcode — e.g. Cornwall, EX20, Highland"
            value={filters.q}
            onChange={(e) => set({ q: e.target.value })}
          />
        </div>

        <Field label="Rank by">
          <select
            value={filters.sort}
            onChange={(e) => set({ sort: e.target.value as SortKey })}
          >
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </Field>

        <button
          className={`dir-toggle ${filters.dir === "desc" ? "desc" : ""}`}
          onClick={() => set({ dir: filters.dir === "asc" ? "desc" : "asc" })}
          title="Toggle sort direction"
        >
          {filters.dir === "asc" ? "▲ ascending" : "▼ descending"}
        </button>

        <button className={`more ${open ? "on" : ""}`} onClick={() => setOpen((v) => !v)}>
          {open ? "Fewer filters" : "More filters"}
        </button>

        <button className={`map-toggle ${mapOpen ? "on" : ""}`} onClick={toggleMap}>
          {mapOpen ? "Hide map" : "Show map"}
        </button>
      </div>

      {open && (
        <div className="controls-more">
          <Field label="Price £">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.price_min ?? ""}
                onChange={(e) => set({ price_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.price_max ?? ""}
                onChange={(e) => set({ price_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="Interior sq ft">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.in_sqft_min ?? ""}
                onChange={(e) => set({ in_sqft_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.in_sqft_max ?? ""}
                onChange={(e) => set({ in_sqft_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="Land sq ft">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.ex_sqft_min ?? ""}
                onChange={(e) => set({ ex_sqft_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.ex_sqft_max ?? ""}
                onChange={(e) => set({ ex_sqft_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="Acres">
            <div className="pair">
              <input
                type="number"
                step="0.1"
                placeholder="min"
                value={filters.acres_min ?? ""}
                onChange={(e) => set({ acres_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                step="0.1"
                placeholder="max"
                value={filters.acres_max ?? ""}
                onChange={(e) => set({ acres_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="Beds">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.beds_min ?? ""}
                onChange={(e) => set({ beds_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.beds_max ?? ""}
                onChange={(e) => set({ beds_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="£ / interior sq ft">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.gbp_in_min ?? ""}
                onChange={(e) => set({ gbp_in_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.gbp_in_max ?? ""}
                onChange={(e) => set({ gbp_in_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="£ / land sq ft">
            <div className="pair">
              <input
                type="number"
                placeholder="min"
                value={filters.gbp_ex_min ?? ""}
                onChange={(e) => set({ gbp_ex_min: e.target.value ? Number(e.target.value) : undefined })}
              />
              <input
                type="number"
                placeholder="max"
                value={filters.gbp_ex_max ?? ""}
                onChange={(e) => set({ gbp_ex_max: e.target.value ? Number(e.target.value) : undefined })}
              />
            </div>
          </Field>

          <Field label="Type">
            <select
              value={filters.type ?? ""}
              onChange={(e) => set({ type: e.target.value || undefined })}
            >
              <option value="">Any</option>
              <option value="detached">Detached</option>
              <option value="semi-detached">Semi-detached</option>
              <option value="terraced">Terraced</option>
              <option value="bungalow">Bungalow</option>
              <option value="land">Land</option>
            </select>
          </Field>

          <Field label="Source">
            <select
              value={filters.source ?? ""}
              onChange={(e) => set({ source: e.target.value || undefined })}
            >
              <option value="">All portals</option>
              <option value="rightmove">Rightmove</option>
              <option value="onthemarket">OnTheMarket</option>
              <option value="zoopla">Zoopla</option>
            </select>
          </Field>

          <Field label="Country">
            <select
              value={filters.country ?? ""}
              onChange={(e) => set({ country: e.target.value || undefined, region: undefined })}
            >
              <option value="">All UK</option>
              {countries.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Region">
            <select
              value={filters.region ?? ""}
              onChange={(e) => set({ region: e.target.value || undefined })}
            >
              <option value="">All regions</option>
              {regionList.map((r) => (
                <option key={r.region} value={r.region}>
                  {r.region} ({r.n})
                </option>
              ))}
            </select>
          </Field>

          <Field label="Listing kind">
            <select
              value={filters.land_only ?? ""}
              onChange={(e) => set({ land_only: e.target.value as Filters["land_only"] })}
            >
              <option value="">Everything</option>
              <option value="0">Houses only</option>
              <option value="1">Bare land only</option>
            </select>
          </Field>

          <Field label="Data">
            <div className="checks">
              <label className="check">
                <input
                  type="checkbox"
                  checked={filters.has_in === "1"}
                  onChange={(e) => set({ has_in: e.target.checked ? "1" : "" })}
                />
                has interior size
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={filters.has_ex === "1"}
                  onChange={(e) => set({ has_ex: e.target.checked ? "1" : "" })}
                />
                has measured land
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={filters.no_highlands === "1"}
                  onChange={(e) => set({ no_highlands: e.target.checked ? "1" : "" })}
                />
                no Highlands / islands
              </label>
              <label className={`check ${!hasBbox ? "muted" : ""}`}>
                <input
                  type="checkbox"
                  checked={syncMap}
                  onChange={(e) => setSyncMap(e.target.checked)}
                />
                only what the map shows
              </label>
            </div>
          </Field>
        </div>
      )}
    </section>
  );
}
