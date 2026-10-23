import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Feature, FeatureCollection } from "../api/geojson";
import { projectPath, useAllVersions, useConflicts, useCorrections, useIntegrated, useStudyAreas } from "../api/hooks";
import { MapView, geomFromString, type LayerKind, type MapLayer } from "../components/map/MapView";
import { Badge, Card, ErrorBanner, JsonView, Loading, StatusBadge, fmtNum } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

const KIND_STYLE: Record<string, LayerKind> = {
  parcels: "parcels", buildings: "buildings", predicted_buildings: "predicted", property_records: "parcels",
  utility_lines: "buildings", other: "buildings",
};
const SEVERITY_COLOR: Record<string, string> = { critical: "#7f1d1d", high: "#dc2626", medium: "#f97316", low: "#facc15" };

export function MapPage() {
  const pid = useProjectId();
  const versions = useAllVersions(pid);
  const [enabled, setEnabled] = useState<Record<number, boolean>>({});
  const [overlays, setOverlays] = useState({ conflicts: true, proposed: true, approved: true, integrated: false });
  const [selected, setSelected] = useState<{ layer: string; feature: Feature } | null>(null);
  const sa = useStudyAreas(pid);
  const conflicts = useConflicts(pid, { status: "pending", limit: 2000 });
  const proposed = useCorrections(pid, "proposed");
  const applied = useCorrections(pid, "applied");
  const integratedP = useIntegrated(pid, "parcel");
  const integratedB = useIntegrated(pid, "building");

  const processed = useMemo(() => (versions.data ?? []).filter((v) => v.status === "processed" && v.geometry_type
    && v.dataset.kind !== "imagery"), [versions.data]);
  // latest processed version per dataset is on by default
  const isOn = (id: number) => enabled[id] ?? processed.some((v) => v.id === id &&
    !processed.some((w) => w.dataset_id === v.dataset_id && w.version_no > v.version_no));
  const active = processed.filter((v) => isOn(v.id));
  const featureQueries = useQueries({
    queries: active.map((v) => ({
      queryKey: ["features", pid, v.id],
      queryFn: () => api.get<FeatureCollection>(`${projectPath(pid)}/versions/${v.id}/features`, { limit: 5000 }),
    })),
  });

  const layers: MapLayer[] = useMemo(() => {
    const out: MapLayer[] = [{ id: "sa", kind: "study_area", data: sa.data }];
    active.forEach((v, i) => out.push({ id: `v${v.id}`, kind: KIND_STYLE[v.dataset.kind] ?? "buildings",
      name: `${v.dataset.name} v${v.version_no}`, data: featureQueries[i]?.data }));
    if (overlays.integrated) {
      out.push({ id: "ip", kind: "integrated_parcels", data: integratedP.data });
      out.push({ id: "ib", kind: "integrated_buildings", data: integratedB.data });
    }
    if (overlays.proposed && proposed.data) out.push({ id: "prop", kind: "proposed", data: proposed.data });
    if (overlays.approved && applied.data) out.push({ id: "appr", kind: "approved", data: applied.data });
    if (overlays.conflicts) out.push({ id: "conf", kind: "conflicts", data: conflicts.data,
      styleFn: (f) => ({ color: SEVERITY_COLOR[String((f.properties as { severity?: string }).severity)] ?? "#dc2626" }) });
    if (selected?.feature.geometry) out.push({ id: "sel", kind: "highlight", data: selected.feature.geometry, name: "Selected" });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sa.data, featureQueries.map((q) => q.dataUpdatedAt).join(","), overlays, conflicts.data, proposed.data, applied.data,
    integratedP.data, integratedB.data, selected, active.map((v) => v.id).join(",")]);

  const fitTo = useMemo(() => {
    const g = (sa.data?.features ?? []).map((f) => f.geometry);
    if (g.length) return g;
    return featureQueries.flatMap((q) => q.data?.features.slice(0, 500).map((f) => f.geometry) ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sa.data, featureQueries.length]);

  return (
    <div className="grid h-[calc(100vh-2rem)] min-h-[520px] gap-3 lg:grid-cols-[260px_1fr_320px]">
      <Card title="Layers" className="overflow-auto">
        {versions.isLoading && <Loading />}
        <ErrorBanner error={versions.error} />
        <fieldset className="space-y-1 text-sm">
          <legend className="mb-1 text-xs font-semibold uppercase text-slate-500">Dataset versions</legend>
          {processed.length === 0 && <p className="text-slate-500">No processed vector datasets. <Link className="text-brand-700 underline" to="../datasets">Process one</Link>.</p>}
          {processed.map((v) => (
            <label key={v.id} className="flex items-start gap-2">
              <input type="checkbox" checked={isOn(v.id)} onChange={() => setEnabled((e) => ({ ...e, [v.id]: !isOn(v.id) }))} />
              <span>{v.dataset.name} <span className="text-xs text-slate-500">v{v.version_no} · {v.feature_count}</span>
                {v.dataset.provenance !== "real" && <> <StatusBadge value={v.dataset.provenance} /></>}</span>
            </label>
          ))}
        </fieldset>
        <fieldset className="mt-4 space-y-1 text-sm">
          <legend className="mb-1 text-xs font-semibold uppercase text-slate-500">Overlays</legend>
          {(Object.keys(overlays) as (keyof typeof overlays)[]).map((k) => (
            <label key={k} className="flex items-center gap-2">
              <input type="checkbox" checked={overlays[k]} onChange={() => setOverlays((o) => ({ ...o, [k]: !o[k] }))} />
              {{ conflicts: "Pending conflicts", proposed: "Proposed corrections", approved: "Approved corrections",
                integrated: "Integrated layer" }[k]}
            </label>
          ))}
        </fieldset>
        {featureQueries.some((q) => q.isLoading) && <Loading label="Loading features…" />}
      </Card>
      <div className="min-h-[400px] overflow-hidden rounded-lg border border-slate-200">
        <MapView layers={layers} fitTo={fitTo} onSelect={(layer, feature) => setSelected({ layer, feature })} />
      </div>
      <Card title="Selection" className="overflow-auto">
        {!selected ? <p className="text-sm text-slate-500">Click a feature on the map.</p> : <FeatureInfo pid={pid} sel={selected} />}
      </Card>
    </div>
  );
}

function FeatureInfo({ pid, sel }: { pid: number; sel: { layer: string; feature: Feature } }) {
  const p = sel.feature.properties as Record<string, unknown>;
  if (sel.layer === "conf") {
    return (
      <div className="space-y-2 text-sm">
        <p className="font-semibold">{String(p.category).replace(/_/g, " ")}</p>
        <p><StatusBadge value={String(p.severity)} /> priority {fmtNum(p.priority, 0)} · confidence {fmtNum(p.confidence)}</p>
        {p.legal_sensitive ? <Badge tone="red">legally sensitive</Badge> : null}
        <p>{String(p.explanation)}</p>
        <p className="text-slate-600"><em>Proposed:</em> {String(p.proposed_action)}</p>
        <Link className="text-brand-700 underline" to={`../review?conflict=${p.id}`}>Open in review queue →</Link>
      </div>
    );
  }
  if (sel.layer === "prop" || sel.layer === "appr") {
    const orig = geomFromString(p.original_geojson as string);
    return (
      <div className="space-y-2 text-sm">
        <p className="font-semibold">Correction #{String(p.id)} · {String(p.kind)}</p>
        <StatusBadge value={String(p.status)} /> <Badge tone="amber">{String(p.safety).replace(/_/g, " ")}</Badge>
        <p>{String(p.reason)}</p>
        <p className="text-xs text-slate-500">Method: {String(p.method)}</p>
        {orig && <p className="text-xs text-slate-500">Original geometry preserved ({orig.type}).</p>}
      </div>
    );
  }
  return (
    <div className="space-y-2 text-sm">
      <p className="font-semibold">{String(p.source_id_raw ?? p.integrated_key ?? `feature ${p.id}`)}</p>
      {p.repair_status && p.repair_status !== "none" ? <StatusBadge value={String(p.repair_status)} /> : null}
      {p.is_valid_source === false && <p className="text-rose-700">Invalid source geometry: {String(p.validity_reason)}</p>}
      {p.area_m2 != null && <p>Area (working CRS): {fmtNum(p.area_m2, 1)} m²</p>}
      <p className="text-xs font-semibold uppercase text-slate-500">Normalized attributes</p>
      <JsonView value={p.normalized_attributes ?? p.attributes} />
      {p.attributes != null ? <><p className="text-xs font-semibold uppercase text-slate-500">Original attributes</p><JsonView value={p.attributes} /></> : null}
      {p.lineage != null ? <><p className="text-xs font-semibold uppercase text-slate-500">Lineage</p><JsonView value={p.lineage} /></> : null}
      <p className="text-xs text-slate-400">project {pid}</p>
    </div>
  );
}
