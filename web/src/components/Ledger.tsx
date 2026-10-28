import { fmtInt, fmtMoney, fmtNum, daysAgo, SOURCE_LABEL } from "../api";
import type { Filters, Listing, SortKey } from "../types";

interface Props {
  rows: Listing[];
  total: number | null;
  loading: boolean;
  filters: Filters;
  onSort: (k: SortKey) => void;
  onSelect: (l: Listing) => void;
  selected?: string;
  mapFilter?: boolean;
  onClearMapFilter?: () => void;
}

function valueClass(v: number | null): string {
  if (v === null) return "num";
  if (v < 100) return "num good";
  if (v < 300) return "num mid";
  return "num high";
}

/** £/acre runs in the thousands, not the hundreds. */
function acreClass(v: number | null): string {
  if (v === null) return "num";
  if (v < 10000) return "num good";
  if (v < 25000) return "num mid";
  return "num high";
}

/** Stated acreage and the registered plot disagree by >2x. */
function plotMismatch(r: Listing): boolean {
  if (!r.acres || !r.ex_sqft) return false;
  const plotAcres = r.ex_sqft / 43560;
  if (plotAcres <= 0) return false;
  const ratio = r.acres / plotAcres;
  return ratio > 2 || ratio < 0.5;
}

function sizeCell(sqft: number | null, conf: string | null, src: string | null) {
  if (sqft === null) return <span className="dim">—</span>;
  const badge =
    conf === "uprn" ? "EPC" :
    conf === "postcode" ? "EPC~" :
    src === "portal" ? "listed" :
    src === "inspire" || src === "ros" ? "reg" :
    src === "stated" ? "stated" : "";
  return (
    <span className="size">
      {fmtInt(sqft)}
      {badge && <span className={`src-badge src-${badge.toLowerCase().replace("~", "")}`}>{badge}</span>}
    </span>
  );
}

export function Ledger({
  rows,
  total,
  loading,
  filters,
  onSort,
  onSelect,
  selected,
  mapFilter,
  onClearMapFilter,
}: Props) {
  const arrow = (k: SortKey) =>
    filters.sort === k ? (filters.dir === "asc" ? " ▲" : " ▼") : "";

  return (
    <main className={`ledger ${loading ? "loading" : ""}`}>
      <div className="ledger-head">
        <span className="count">
          {total !== null ? (
            <>
              <strong>{fmtInt(total)}</strong> listings
            </>
          ) : (
            <>{rows.length} shown</>
          )}
          {total !== null && total > rows.length && (
            <span className="dim"> · top {rows.length}</span>
          )}
        </span>
        <span className="dim small">
          sort: {filters.sort.replace("gbp_", "£/").replace("_", " ")} {filters.dir}
        </span>
      </div>

      {mapFilter && (
        <div className="map-filter-chip">
          <span>Filtered to the visible map area — other results are hidden</span>
          <button onClick={onClearMapFilter}>Show all</button>
        </div>
      )}

      <div className="table-scroll">
        <table className="grid">
          <thead>
            <tr>
              <th className="col-rank">#</th>
              <th className="sortable" onClick={() => onSort("gbp_in")} title="Price per square foot of interior floor area">
                £/in sq ft{arrow("gbp_in")}
              </th>
              <th className="sortable" onClick={() => onSort("gbp_ex")} title="Price per square foot of registered plot (land + building footprint)">
                £/land sq ft{arrow("gbp_ex")}
              </th>
              <th className="sortable" onClick={() => onSort("gbp_ac")} title="Price per acre of land">
                £/acre{arrow("gbp_ac")}
              </th>
              <th className="sortable" onClick={() => onSort("price")}>
                Price{arrow("price")}
              </th>
              <th className="sortable" onClick={() => onSort("in_sqft")} title="Interior floor area (EPC or portal-stated)">
                Interior{arrow("in_sqft")}
              </th>
              <th className="sortable" onClick={() => onSort("ex_sqft")} title="Registered plot area (land incl. building footprint)">
                Land{arrow("ex_sqft")}
              </th>
              <th className="sortable" onClick={() => onSort("acres")}>
                Acres{arrow("acres")}
              </th>
              <th className="sortable" onClick={() => onSort("beds")}>
                Beds{arrow("beds")}
              </th>
              <th>Type</th>
              <th className="col-address">Address</th>
              <th>Region</th>
              <th className="sortable" onClick={() => onSort("published")}>
                Listed{arrow("published")}
              </th>
              <th>Src</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr
                key={r.uid}
                className={selected === r.uid ? "sel" : ""}
                onClick={() => onSelect(r)}
              >
                <td className="col-rank">{i + 1}</td>
                <td className={valueClass(r.gbp_in)}>{fmtMoney(r.gbp_in)}</td>
                <td className={valueClass(r.gbp_ex)}>{fmtMoney(r.gbp_ex)}</td>
                <td className={acreClass(r.gbp_ac)}>
                  {fmtMoney(r.gbp_ac)}
                  {r.value_ratio != null && r.value_ratio >= 1.15 && (
                    <span
                      className="ratio"
                      title={`${Math.round((r.value_ratio - 1) * 100)}% below the ${r.region ?? "regional"} median of £${Math.round(r.region_median ?? 0).toLocaleString("en-GB")}/acre`}
                    >
                      −{Math.round((r.value_ratio - 1) * 100)}%
                    </span>
                  )}
                </td>
                <td className="num price">{fmtMoney(r.price)}</td>
                <td className="num">{sizeCell(r.in_sqft, r.in_conf, r.in_src)}</td>
                <td className="num">{sizeCell(r.ex_sqft, r.ex_conf, r.ex_src)}</td>
                <td className="num">
                  {fmtNum(r.acres, 2)}
                  {plotMismatch(r) && (
                    <span
                      className="warn-badge"
                      title="Stated acreage disagrees with the registered plot area — check both"
                    >
                      ⚠
                    </span>
                  )}
                </td>
                <td className="num">{r.beds || "—"}</td>
                <td className="type">{r.land_only ? "land" : r.type || "—"}</td>
                <td className="col-address">
                  <span className="addr">{r.address || "—"}</span>
                  {r.postcode && <span className="pc">{r.postcode}</span>}
                </td>
                <td className="region">{r.region || "—"}</td>
                <td className="num dim">{daysAgo(r.published || r.first_seen)}</td>
                <td className="src">{SOURCE_LABEL[r.source] ?? r.source}</td>
              </tr>
            ))}
            {!rows.length && !loading && (
              <tr>
                <td colSpan={14} className="empty">
                  No listings match these filters. Try clearing “has interior size”
                  or widening the price range.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </main>
  );
}
