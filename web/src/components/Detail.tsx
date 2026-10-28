import { fmtInt, fmtMoney, fmtNum, SOURCE_LABEL, SOURCE_URL, daysAgo } from "../api";
import type { Listing } from "../types";

interface Props {
  listing: Listing;
  onClose: () => void;
}

function Row({ label, value, note }: { label: string; value: React.ReactNode; note?: string }) {
  return (
    <div className="drow">
      <span className="dlabel">{label}</span>
      <span className="dvalue">
        {value}
        {note && <span className="dnote">{note}</span>}
      </span>
    </div>
  );
}

export function Detail({ listing: l, onClose }: Props) {
  const inConf =
    l.in_conf === "uprn" ? "matched to the property's EPC" :
    l.in_conf === "postcode" ? "matched by postcode (EPC)" :
    l.in_src === "portal" ? "stated in the listing" : "";
  const exConf =
    l.ex_conf === "registered" ? "HM Land Registry registered plot" :
    l.ex_src === "ros" ? "Registers of Scotland cadastral parcel" :
    l.ex_src === "inspire" ? "registered plot boundary" :
    l.ex_src === "stated" ? "stated in the listing" : "";

  return (
    <aside className="drawer" role="dialog" aria-label="Listing detail">
      <button className="drawer-close" onClick={onClose} aria-label="Close">
        ×
      </button>

      <div className="drawer-head">
        <h2>{l.address || "Listing"}</h2>
        <div className="drawer-sub">
          {[l.postcode, l.region, l.country].filter(Boolean).join(" · ")}
        </div>
      </div>

      <div className="drawer-price">{fmtMoney(l.price)}</div>

      <div className="drawer-metrics">
        <div className="metric">
          <span className="metric-v">{fmtMoney(l.gbp_in)}</span>
          <span className="metric-l">per interior sq ft</span>
        </div>
        <div className="metric">
          <span className="metric-v">{fmtMoney(l.gbp_ex)}</span>
          <span className="metric-l">per land sq ft</span>
        </div>
        <div className="metric">
          <span className="metric-v">{fmtMoney(l.gbp_ac)}</span>
          <span className="metric-l">per acre</span>
        </div>
      </div>

      {l.value_ratio != null && l.region_median != null && (
        <div className={`vs-region ${l.value_ratio >= 1.15 ? "good" : l.value_ratio <= 0.87 ? "high" : ""}`}>
          {l.value_ratio >= 1
            ? `${Math.round((l.value_ratio - 1) * 100)}% below`
            : `${Math.round((1 - l.value_ratio) * 100)}% above`}{" "}
          the {l.region || "regional"} median
          <span className="dim">
            {" "}· £{Math.round(l.region_median).toLocaleString("en-GB")}/acre
          </span>
        </div>
      )}

      <div className="drawer-rows">
        <Row
          label="Interior"
          value={l.in_sqft ? `${fmtInt(l.in_sqft)} sq ft` : "—"}
          note={l.in_sqft ? ` (${fmtNum(l.in_sqm, 1)} m²${inConf ? " · " + inConf : ""})` : ""}
        />
        <Row
          label="Land / plot"
          value={l.ex_sqft ? `${fmtInt(l.ex_sqft)} sq ft` : "—"}
          note={l.ex_sqft ? ` (${fmtNum(l.ex_sqm, 1)} m²${exConf ? " · " + exConf : ""})` : ""}
        />        <Row
          label="Acres"
          value={l.acres ? fmtNum(l.acres, 3) : "—"}
          note={exConf ? ` · ${exConf}` : l.acres_src ? ` · ${l.acres_src}` : ""}
        />
        <Row label="Beds / baths" value={`${l.beds ?? "—"} bed · ${l.baths ?? "—"} bath`} />
        <Row label="Type" value={l.land_only ? "Land" : l.type || "—"} />
        <Row label="Tenure" value={l.tenure || "—"} />
        <Row
          label="EPC"
          value={l.epc || "—"}
          note={l.epc_date ? ` · ${l.epc_date}` : ""}
        />
        <Row label="Source" value={SOURCE_LABEL[l.source] ?? l.source} />
        <Row
          label="Listed"
          value={l.published ? `${new Date(l.published).toLocaleDateString("en-GB")}` : "—"}
          note={` · seen ${daysAgo(l.last_seen)} ago`}
        />
        {l.sold_price && (
          <Row
            label="Sold"
            value={`${fmtMoney(l.sold_price)} · ${l.sold_date ?? ""}`}
          />
        )}
      </div>

      <div className="drawer-actions">
        <a className="btn primary" href={SOURCE_URL(l)} target="_blank" rel="noopener noreferrer">
          Open on {SOURCE_LABEL[l.source] ?? l.source} ↗
        </a>
        {l.postcode && (
          <a
            className="btn"
            href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
              `${l.address ?? ""} ${l.postcode}`,
            )}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Street view ↗
          </a>
        )}
        <a
          className="btn"
          href={`https://www.gov.uk/find-energy-certificate`}
          target="_blank"
          rel="noopener noreferrer"
        >
          EPC register ↗
        </a>
      </div>

      <p className="drawer-note">
        Land and plot figures are the gross registered title: house footprint,
        garden and driveway all included. Boundaries are indicative, drawn from
        HM Land Registry / Registers of Scotland polygons — the title plan is
        the legal record. Interior sizes come from Energy Performance
        Certificates where one exists, otherwise the listing itself. A dash
        means neither is on record for this address. Always confirm with the
        agent before acting.
      </p>
    </aside>
  );
}
