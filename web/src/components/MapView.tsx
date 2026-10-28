import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection } from "geojson";
import type { Listing, PlotPolygon } from "../types";

interface Props {
  points: Listing[];
  selected: Listing | null;
  plot: PlotPolygon | null;
  onSelect: (l: Listing) => void;
  onBounds: (bbox: string) => void;
}

const STYLE = "https://tiles.openfreemap.org/styles/positron";
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export function MapView({ points, selected, plot, onSelect, onBounds }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    if (!ref.current || map.current) return;
    const m = new maplibregl.Map({
      container: ref.current,
      style: STYLE,
      center: [-2.4, 53.4],
      zoom: 5.4,
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      m.addSource("boundary", { type: "geojson", data: EMPTY });
      m.addLayer({
        id: "boundary-fill",
        type: "fill",
        source: "boundary",
        paint: { "fill-color": "#4338CA", "fill-opacity": 0.14 },
      });
      m.addLayer({
        id: "boundary-line",
        type: "line",
        source: "boundary",
        paint: { "line-color": "#4338CA", "line-width": 2 },
      });
      m.addSource("listings", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
        cluster: true,
        clusterRadius: 46,
        clusterMaxZoom: 13,
      });
      m.addLayer({
        id: "clusters",
        type: "circle",
        source: "listings",
        filter: ["has", "point_count"],
        paint: {
          "circle-color": "#4338CA",
          "circle-opacity": 0.82,
          "circle-radius": ["step", ["get", "point_count"], 13, 25, 17, 200, 22, 1000, 28],
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#F6F7F9",
        },
      });
      m.addLayer({
        id: "cluster-count",
        type: "symbol",
        source: "listings",
        filter: ["has", "point_count"],
        layout: {
          "text-field": ["get", "point_count_abbreviated"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 11,
        },
        paint: { "text-color": "#ffffff" },
      });
      m.addLayer({
        id: "unclustered",
        type: "circle",
        source: "listings",
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-color": [
            "interpolate", ["linear"], ["coalesce", ["get", "gbp_in"], 400],
            50, "#15803D",
            150, "#65A30D",
            300, "#B45309",
            600, "#B91C1C",
          ],
          "circle-radius": 5,
          "circle-stroke-width": 1,
          "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.9,
        },
      });
      m.on("click", "unclustered", (e) => {
        const f = e.features?.[0];
        if (f?.properties) {
          onSelectRef.current(f.properties as unknown as Listing);
        }
      });
      m.on("mouseenter", "unclustered", () => {
        m.getCanvas().style.cursor = "pointer";
      });
      m.on("mouseleave", "unclustered", () => {
        m.getCanvas().style.cursor = "";
      });
      m.on("click", "clusters", (e) => {
        const f = e.features?.[0];
        const src = m.getSource("listings") as maplibregl.GeoJSONSource;
        if (!f || !src) return;
        const clusterId = f.properties?.cluster_id;
        src.getClusterExpansionZoom(clusterId).then((z) => {
          m.easeTo({
            center: (f.geometry as unknown as { coordinates: [number, number] }).coordinates,
            zoom: z,
          });
        });
      });
    });

    const report = () => {
      const b = m.getBounds();
      onBounds(
        [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
          .map((v) => v.toFixed(4))
          .join(","),
      );
    };
    m.on("moveend", report);
    m.on("load", report);
    // the panel can mount at 0 height inside the grid; keep MapLibre in sync
    const ro = new ResizeObserver(() => m.resize());
    if (ref.current) ro.observe(ref.current);
    map.current = m;
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("listings") as maplibregl.GeoJSONSource | undefined;
      if (!src) return;
      src.setData({
        type: "FeatureCollection",
        features: points
          .filter((p) => typeof p.lat === "number" && typeof p.lng === "number")
          .map((p) => ({
            type: "Feature",
            geometry: { type: "Point", coordinates: [p.lng as number, p.lat as number] },
            properties: {
              uid: p.uid,
              source: p.source,
              url: p.url,
              address: p.address,
              price: p.price,
              gbp_in: p.gbp_in,
              gbp_ex: p.gbp_ex,
              gbp_ac: p.gbp_ac,
              in_sqft: p.in_sqft,
              ex_sqft: p.ex_sqft,
              acres: p.acres,
            },
          })),
      });
    };
    if (m.isStyleLoaded()) apply();
    else m.once("load", apply);
  }, [points]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("boundary") as maplibregl.GeoJSONSource | undefined;
      if (!src) return;
      if (!plot) {
        src.setData(EMPTY);
        return;
      }
      src.setData({
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: plot, properties: {} }],
      });
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const [x, y] of plot.coordinates[0] ?? []) {
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
      }
      if (Number.isFinite(minX)) {
        m.fitBounds(
          [[minX, minY], [maxX, maxY]],
          { padding: 90, maxZoom: 18, duration: 700 },
        );
      }
    };
    if (m.isStyleLoaded()) apply();
    else m.once("load", apply);
  }, [plot]);

  useEffect(() => {
    const m = map.current;
    if (plot) return;
    if (!m || !selected || typeof selected.lat !== "number" || typeof selected.lng !== "number") return;
    m.easeTo({ center: [selected.lng, selected.lat], zoom: Math.max(m.getZoom(), 13), duration: 600 });
  }, [selected, plot]);

  return (
    <aside className="mapwrap">
      <div ref={ref} className="map" />
      <div className="map-legend">
        <span><i className="dot good" /> under £100/sq ft</span>
        <span><i className="dot mid" /> £100–300</span>
        <span><i className="dot high" /> over £300</span>
      </div>
    </aside>
  );
}
