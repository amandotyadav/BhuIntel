import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { FeatureCollection } from "../api/geojson";
import { projectPath, useApiMutation, useConflict, useConflicts } from "../api/hooks";
import type { ConflictDetail, Correction, SourceFeatureProps } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { MapView, geomFromString, type MapLayer } from "../components/map/MapView";
import {
  Badge, Button, Card, EmptyState, ErrorBanner, Field, JsonView, Loading, Notice, Select, StatusBadge, Textarea, fmtDate, fmtNum,
} from "../components/ui";
import { useProjectId } from "./ProjectLayout";

const CATEGORIES = ["parcel_id_mismatch", "attribute_conflict", "source_disagreement", "unmatched_feature", "unmatched_record",
  "ambiguous_record_link", "duplicate_record", "area_discrepancy", "building_crosses_parcel", "potentially_outdated",
  "invalid_geometry", "parcel_overlap", "parcel_gap", "sliver_polygon", "duplicate_geometry", "unexpected_geometry_type",
  "dangling_segment", "disconnected_network"];

const SEV_TONE = { critical: "red", high: "red", medium: "amber", low: "gray" } as const;

function SourceComparison({ d }: { d: ConflictDetail }) {
  const feats = d.features.features;
  const keys = Array.from(new Set(feats.flatMap((f) => Object.keys(f.properties.normalized_attributes ?? {})))).sort();
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead><tr className="bg-slate-50"><th className="px-2 py-1 text-left">Field</th>
          {feats.map((f) => <th key={f.properties.id} className="px-2 py-1 text-left">
            {f.properties.dataset} <span className="font-normal text-slate-500">v{f.properties.version_no}</span><br />
            <span className="font-normal">reliability {f.properties.reliability}{f.properties.source_date ? ` · ${f.properties.source_date.slice(0, 10)}` : ""}</span>
            {f.properties.provenance !== "real" && <> <StatusBadge value={f.properties.provenance} /></>}
          </th>)}</tr></thead>
        <tbody>
          <tr className="border-t"><td className="px-2 py-1 font-medium">identifier</td>{feats.map((f) => <td key={f.properties.id} className="px-2 py-1">{f.properties.source_id_raw ?? "—"}</td>)}</tr>
          <tr className="border-t"><td className="px-2 py-1 font-medium">area (computed)</td>{feats.map((f) => <td key={f.properties.id} className="px-2 py-1">{fmtNum(f.properties.area_m2, 1)} m²</td>)}</tr>
          <tr className="border-t"><td className="px-2 py-1 font-medium">geometry valid</td>{feats.map((f) => <td key={f.properties.id} className="px-2 py-1">{f.properties.is_valid_source === false ? <Badge tone="red">{f.properties.validity_reason}</Badge> : "yes"}</td>)}</tr>
          {keys.map((k) => {
            const vals = feats.map((f) => JSON.stringify(f.properties.normalized_attributes?.[k] ?? null));
            const differ = new Set(vals.filter((v) => v !== "null")).size > 1;
            return (
              <tr key={k} className={`border-t ${differ ? "bg-amber-50" : ""}`}>
                <td className="px-2 py-1 font-medium">{k}{differ && " ⚠"}</td>
                {feats.map((f) => <td key={f.properties.id} className="px-2 py-1">{String(f.properties.normalized_attributes?.[k] ?? "—")}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CorrectionPreview({ c }: { c: Correction }) {
  return (
    <div className="space-y-1 rounded border border-slate-200 p-2 text-xs">
      <p className="font-semibold">Correction #{c.id} · {c.kind.replace(/_/g, " ")} <StatusBadge value={c.status} />{" "}
        <Badge tone={c.safety === "legal_no_auto" ? "red" : c.safety === "auto_safe" ? "green" : "amber"}>{c.safety.replace(/_/g, " ")}</Badge></p>
      <p>{c.reason}</p>
      <p className="text-slate-500">Method: {c.method}</p>
      {Object.keys(c.proposed_attributes ?? {}).length > 0 && (
        <p>Attributes: <code>{JSON.stringify(c.original_attributes)}</code> → <code>{JSON.stringify(c.proposed_attributes)}</code></p>
      )}
      {c.metrics && Object.keys(c.metrics).length > 0 && <JsonView value={c.metrics} />}
    </div>
  );
}

function DecisionForm({ pid, d }: { pid: number; d: ConflictDetail }) {
  const { user, hasRole } = useAuth();
  const [comment, setComment] = useState("");
  const [ack, setAck] = useState(false);
  const m = useApiMutation((decision: string) => api.send("POST", `${projectPath(pid)}/conflicts/${d.id}/decision`,
    { decision, comment, acknowledge_legal: ack }), [["conflict", pid, d.id], ["conflicts", pid], ["corrections", pid], ["queue", pid], ["integrated", pid]]);
  if (!hasRole("reviewer")) return <Notice>Your role ({user?.role}) can view but not decide on conflicts.</Notice>;
  return (
    <div className="space-y-2">
      {d.legal_sensitive && (
        <Notice tone="amber">
          <strong>Legally sensitive.</strong> Approval requires an admin and an explicit acknowledgement that it is backed by authoritative
          evidence (e.g. survey records). Approval does not move cadastral boundaries in source data; it only updates the derived integrated layer.
          <label className="mt-1 flex items-center gap-2"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            I confirm this decision is backed by authoritative evidence</label>
        </Notice>
      )}
      <Field label="Reviewer comment"><Textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} className="font-sans text-sm" /></Field>
      <div className="flex flex-wrap gap-2">
        <Button variant="success" onClick={() => m.mutate("approve")} loading={m.isPending && m.variables === "approve"}>Approve proposal</Button>
        <Button variant="danger" onClick={() => m.mutate("reject")} loading={m.isPending && m.variables === "reject"}>Reject</Button>
        <Button variant="secondary" onClick={() => m.mutate("defer")}>Defer</Button>
        <Button variant="ghost" onClick={() => m.mutate("comment")} disabled={!comment}>Add comment only</Button>
      </div>
      <ErrorBanner error={m.error} title="Decision not saved" />
      {m.isSuccess && <Notice tone="green">Decision recorded{(m.data as { correction_outcome?: string })?.correction_outcome ? ` (correction ${(m.data as { correction_outcome?: string }).correction_outcome})` : ""}.</Notice>}
    </div>
  );
}

function ConflictPanel({ pid, id }: { pid: number; id: number }) {
  const q = useConflict(pid, id);
  const [view, setView] = useState<"after" | "before">("after");
  const layers = useMemo<MapLayer[]>(() => {
    const d = q.data;
    if (!d) return [];
    const byDataset = new Map<number, FeatureCollection<SourceFeatureProps>>();
    d.features.features.forEach((f) => {
      const fc = byDataset.get(f.properties.dataset_id) ?? { type: "FeatureCollection", features: [] };
      fc.features.push(f);
      byDataset.set(f.properties.dataset_id, fc);
    });
    const palette = ["#1d4ed8", "#16a34a", "#9333ea", "#ea580c"];
    const out: MapLayer[] = Array.from(byDataset.entries()).map(([dsId, fc], i) => ({
      id: `ds${dsId}`, kind: "parcels", name: fc.features[0].properties.dataset, data: fc,
      styleFn: () => ({ color: palette[i % palette.length], fillColor: palette[i % palette.length] }) }));
    out.push({ id: "c", kind: "conflicts", name: "Conflict area", data: d.geometry });
    d.corrections.forEach((c) => {
      const g = geomFromString(view === "after" ? c.proposed_geojson : c.original_geojson);
      if (g) out.push({ id: `corr${c.id}`, kind: view === "after" ? (c.status === "applied" ? "approved" : "proposed") : "original",
        name: `${view === "after" ? "Proposed" : "Original"} geometry (#${c.id})`, data: g });
    });
    return out;
  }, [q.data, view]);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBanner error={q.error} />;
  const d = q.data!;
  const hasGeomCorr = d.corrections.some((c) => c.proposed_geojson);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{d.category.replace(/_/g, " ")} #{d.id}</h2>
        <Badge tone={SEV_TONE[d.severity as keyof typeof SEV_TONE] ?? "gray"}>{d.severity}</Badge>
        <StatusBadge value={d.status} />
        {d.legal_sensitive && <Badge tone="red">legally sensitive</Badge>}
        <span className="text-xs text-slate-500">priority {fmtNum(d.priority, 0)} · confidence {fmtNum(d.confidence)} ({d.confidence_kind})</span>
      </div>
      <p className="text-sm">{d.explanation}</p>
      <p className="text-sm text-slate-600"><strong>Proposed action:</strong> {d.proposed_action}</p>
      <div className="h-72 overflow-hidden rounded border border-slate-200"><MapView layers={layers} /></div>
      {hasGeomCorr && (
        <div className="flex gap-2 text-sm" role="group" aria-label="Geometry view">
          <Button variant={view === "before" ? "primary" : "secondary"} onClick={() => setView("before")}>Before (original)</Button>
          <Button variant={view === "after" ? "primary" : "secondary"} onClick={() => setView("after")}>After (proposed)</Button>
        </div>
      )}
      <Card title="Source comparison"><SourceComparison d={d} /></Card>
      {d.corrections.length > 0 && <Card title="Proposed corrections"><div className="space-y-2">{d.corrections.map((c) => <CorrectionPreview key={c.id} c={c} />)}</div></Card>}
      <details><summary className="cursor-pointer text-sm font-medium">Evidence</summary><JsonView value={d.evidence} /></details>
      <Card title="Decision"><DecisionForm pid={pid} d={d} /></Card>
      <Card title="Decision history">
        {d.decisions.length === 0 ? <p className="text-sm text-slate-500">No decisions yet.</p> : (
          <ul className="space-y-1 text-sm">{d.decisions.map((x) => (
            <li key={x.id}><StatusBadge value={x.decision} /> by user #{x.reviewer_id} · {fmtDate(x.created_at)}{x.comment && <> — <em>{x.comment}</em></>}</li>
          ))}</ul>
        )}
      </Card>
    </div>
  );
}

export function ReviewPage() {
  const pid = useProjectId();
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState("pending");
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const selected = params.get("conflict") ? Number(params.get("conflict")) : null;
  const list = useConflicts(pid, { status: status || undefined, category: category || undefined, severity: severity || undefined, limit: 500 });
  return (
    <div className="grid gap-4 xl:grid-cols-[420px_1fr]">
      <Card title={`Review queue${list.data ? ` (${list.data.numberMatched})` : ""}`}>
        <div className="mb-3 grid grid-cols-3 gap-2">
          <Select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>{["pending", "approved", "rejected", "deferred"].map((s) => <option key={s}>{s}</option>)}</Select>
          <Select aria-label="Category filter" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>{CATEGORIES.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}</Select>
          <Select aria-label="Severity filter" value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="">All severities</option>{["critical", "high", "medium", "low"].map((s) => <option key={s}>{s}</option>)}</Select>
        </div>
        {list.isLoading && <Loading />}
        <ErrorBanner error={list.error} />
        {list.data?.features.length === 0 && <EmptyState title="Nothing in this queue" />}
        <ul className="max-h-[70vh] divide-y divide-slate-100 overflow-auto">
          {list.data?.features.map((f) => {
            const p = f.properties;
            return (
              <li key={p.id}>
                <button onClick={() => setParams({ conflict: String(p.id) })}
                  className={`w-full px-2 py-2 text-left text-sm ${p.id === selected ? "bg-brand-50" : "hover:bg-slate-50"}`}>
                  <span className="flex items-center gap-2">
                    <span className="font-medium">{p.category.replace(/_/g, " ")}</span>
                    <Badge tone={SEV_TONE[p.severity] ?? "gray"}>{p.severity}</Badge>
                    {p.legal_sensitive && <Badge tone="red">legal</Badge>}
                    <span className="ml-auto text-xs text-slate-500">P{fmtNum(p.priority, 0)}</span>
                  </span>
                  <span className="line-clamp-2 text-xs text-slate-600">{p.explanation}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
      <div>{selected ? <ConflictPanel key={selected} pid={pid} id={selected} /> : <EmptyState title="Select a conflict to review" />}</div>
    </div>
  );
}
