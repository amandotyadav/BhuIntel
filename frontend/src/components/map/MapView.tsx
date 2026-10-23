import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { useEffect, useMemo, useState } from "react";
import { GeoJSON, MapContainer, TileLayer, useMap } from "react-leaflet";
import { boundsOf, type Feature, type FeatureCollection, type Geometry } from "../../api/geojson";

export type LayerKind =
  | "study_area" | "parcels" | "buildings" | "predicted" | "conflicts" | "proposed" | "approved" | "integrated_parcels"
  | "integrated_buildings" | "changes" | "highlight" | "original";

/** Distinct, documented styles for every layer class (see docs/UI.md). */
export const LAYER_STYLES: Record<LayerKind, L.PathOptions & { label: string }> = {
  study_area: { label: "Study area", color: "#0f172a", weight: 1.5, dashArray: "6 4", fillOpacity: 0 },
  parcels: { label: "Existing parcels", color: "#1d4ed8", weight: 1.5, fillColor: "#93c5fd", fillOpacity: 0.15 },
  buildings: { label: "Existing building footprints", color: "#475569", weight: 1, fillColor: "#94a3b8", fillOpacity: 0.55 },
  predicted: { label: "Predicted buildings (unverified)", color: "#c026d3", weight: 1.5, dashArray: "3 3", fillColor: "#f0abfc", fillOpacity: 0.35 },
  conflicts: { label: "Potential conflicts", color: "#dc2626", weight: 2.5, fillColor: "#fca5a5", fillOpacity: 0.35 },
  proposed: { label: "Proposed corrections", color: "#d97706", weight: 2.5, dashArray: "5 4", fillColor: "#fcd34d", fillOpacity: 0.3 },
  approved: { label: "Approved corrections", color: "#059669", weight: 2.5, fillColor: "#6ee7b7", fillOpacity: 0.35 },
  integrated_parcels: { label: "Integrated parcels", color: "#0e7490", weight: 1.5, fillColor: "#67e8f9", fillOpacity: 0.15 },
  integrated_buildings: { label: "Integrated buildings", color: "#334155", weight: 1, fillColor: "#64748b", fillOpacity: 0.6 },
  changes: { label: "Detected changes", color: "#7c3aed", weight: 2, fillColor: "#c4b5fd", fillOpacity: 0.4 },
  highlight: { label: "Selected", color: "#facc15", weight: 4, fillOpacity: 0.1 },
  original: { label: "Original geometry", color: "#dc2626", weight: 2, dashArray: "2 4", fillOpacity: 0.05 },
};

export interface MapLayer {
  id: string;
  kind: LayerKind;
  name?: string;
  data: FeatureCollection<object> | Feature<object> | Geometry | null | undefined;
  visible?: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  styleFn?: (f: Feature<any>) => L.PathOptions;
}

const TILE_URL = (import.meta.env.VITE_TILE_URL as string | undefined) ?? "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTR = (import.meta.env.VITE_TILE_ATTRIBUTION as string | undefined) ?? "&copy; OpenStreetMap contributors";

function asCollection(d: MapLayer["data"]): FeatureCollection<object> | null {
  if (!d) return null;
  if ((d as FeatureCollection).type === "FeatureCollection") return d as FeatureCollection<object>;
  if ((d as Feature).type === "Feature") return { type: "FeatureCollection", features: [d as Feature<object>] };
  return { type: "FeatureCollection", features: [{ type: "Feature", geometry: d as Geometry, properties: {} }] };
}

function FitBounds({ geoms, token }: { geoms: (Geometry | null)[]; token: string }) {
  const map = useMap();
  useEffect(() => {
    const b = boundsOf(geoms);
    if (b) map.fitBounds(b, { padding: [24, 24], maxZoom: 20 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);
  return null;
}

let dataVersion = 0;

export function MapView({ layers, onSelect, fitTo, height = "100%", showLegend = true }: {
  layers: MapLayer[];
  onSelect?: (layerId: string, feature: Feature) => void;
  fitTo?: (Geometry | null)[];
  height?: string;
  showLegend?: boolean;
}) {
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const collections = useMemo(
    () => layers.map((l) => ({ ...l, fc: asCollection(l.data), key: `${l.id}-${++dataVersion}` })),
    [layers],
  );
  const fitGeoms = fitTo ?? collections.flatMap((c) => c.fc?.features.map((f) => f.geometry) ?? []);
  const fitToken = JSON.stringify(boundsOf(fitGeoms));

  return (
    <div className="relative h-full w-full" style={{ height }}>
      <MapContainer center={[20.6, 78.9]} zoom={5} className="h-full w-full rounded-md" preferCanvas={false}>
        {TILE_URL !== "none" && <TileLayer url={TILE_URL} attribution={TILE_ATTR} maxZoom={22} maxNativeZoom={19} />}
        {collections.map((c) =>
          c.fc && c.visible !== false && !hidden[c.id] ? (
            <GeoJSON
              key={c.key}
              data={c.fc as never}
              style={(f) => ({ ...LAYER_STYLES[c.kind], ...(c.styleFn ? c.styleFn(f as unknown as Feature) : {}) })}
              pointToLayer={(_f, latlng) => L.circleMarker(latlng, { radius: 5, ...LAYER_STYLES[c.kind] })}
              onEachFeature={(f, layer) => {
                const p = (f.properties ?? {}) as Record<string, unknown>;
                const label = p.source_id_raw ?? p.integrated_key ?? p.category ?? p.change_type ?? p.id;
                if (label != null) layer.bindTooltip(String(label), { sticky: true });
                if (onSelect) layer.on("click", () => onSelect(c.id, f as unknown as Feature));
              }}
            />
          ) : null,
        )}
        <FitBounds geoms={fitGeoms} token={fitToken} />
      </MapContainer>
      {showLegend && (
        <fieldset className="absolute right-2 top-2 z-[1000] max-w-[15rem] rounded-md border border-slate-200 bg-white/95 p-2 text-xs shadow">
          <legend className="sr-only">Map layers</legend>
          <p className="mb-1 font-semibold text-slate-700">Layers</p>
          {collections.filter((c) => c.visible !== false).map((c) => {
            const s = LAYER_STYLES[c.kind];
            return (
              <label key={c.id} className="flex cursor-pointer items-center gap-2 py-0.5">
                <input type="checkbox" checked={!hidden[c.id]} onChange={() => setHidden((h) => ({ ...h, [c.id]: !h[c.id] }))} />
                <span aria-hidden className="inline-block h-3 w-4 rounded-sm border" style={{ borderColor: s.color,
                  background: s.fillOpacity ? s.fillColor : "transparent", borderStyle: s.dashArray ? "dashed" : "solid" }} />
                <span className="truncate">{c.name ?? s.label}{c.fc ? ` (${c.fc.features.length})` : ""}</span>
              </label>
            );
          })}
          {TILE_URL === "none" && <p className="mt-1 text-slate-500">Basemap disabled (VITE_TILE_URL=none)</p>}
        </fieldset>
      )}
    </div>
  );
}

export function geomFromString(s: string | null | undefined): Geometry | null {
  if (!s) return null;
  try {
    return JSON.parse(s) as Geometry;
  } catch {
    return null;
  }
}
