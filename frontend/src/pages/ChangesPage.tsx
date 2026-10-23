import { useMemo, useState } from "react";
import { api } from "../api/client";
import { projectPath, useAllVersions, useApiMutation, useChanges, usePredictions } from "../api/hooks";
import type { Job } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTracker } from "../components/JobTracker";
import { MapView } from "../components/map/MapView";
import { Badge, Button, Card, EmptyState, ErrorBanner, Field, Input, Notice, Select, StatusBadge, Table, Td, fmtNum } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

function ChangeDetection({ pid }: { pid: number }) {
  const versions = useAllVersions(pid);
  const [oldV, setOldV] = useState("");
  const [newV, setNewV] = useState("");
  const [unc, setUnc] = useState("");
  const [jobId, setJobId] = useState<number | null>(null);
  const processed = (versions.data ?? []).filter((v) => v.status === "processed");
  const old = processed.find((v) => v.id === Number(oldV));
  const sameDs = processed.filter((v) => old && v.dataset_id === old.dataset_id && v.id !== old.id);
  const run = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/changes`, {
    old_version_id: Number(oldV), new_version_id: Number(newV),
    params: unc ? { positional_uncertainty_m: Number(unc) } : undefined }), [["jobs", pid]]);
  const changes = useChanges(pid, { old_version_id: oldV ? Number(oldV) : undefined, new_version_id: newV ? Number(newV) : undefined });
  const layers = useMemo(() => [{ id: "chg", kind: "changes" as const, data: changes.data,
    styleFn: (f: { properties: Record<string, unknown> }) => ({ color: ({ added: "#16a34a", removed: "#dc2626", geometry_modified: "#7c3aed",
      uncertain: "#f59e0b", attribute_changed: "#0284c7", image_change: "#7c3aed" } as Record<string, string>)[String(f.properties.change_type)] ?? "#7c3aed" }) }], [changes.data]);
  return (
    <Card title="Version change detection">
      <form className="grid gap-2 sm:grid-cols-4 sm:items-end" onSubmit={(e) => { e.preventDefault(); run.mutate(undefined, { onSuccess: (j) => setJobId(j.id) }); }}>
        <Field label="Old version"><Select aria-label="Old version" value={oldV} onChange={(e) => { setOldV(e.target.value); setNewV(""); }}>
          <option value="">Select…</option>{processed.map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
        <Field label="New version"><Select aria-label="New version" value={newV} onChange={(e) => setNewV(e.target.value)}>
          <option value="">Select…</option>{sameDs.map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
        <Field label="Positional uncertainty (m)" hint="blank = from dataset accuracy"><Input type="number" step="0.1" value={unc} onChange={(e) => setUnc(e.target.value)} /></Field>
        <Button type="submit" disabled={!oldV || !newV} loading={run.isPending}>Detect changes</Button>
      </form>
      <ErrorBanner error={run.error} />
      <div className="my-2"><JobTracker projectId={pid} jobId={jobId} invalidate={[["changes", pid]]} /></div>
      <Notice>Differences within the positional uncertainty are reported as <em>uncertain</em>, not as real-world change. Features outside
        the other version's coverage are also uncertain.</Notice>
      {changes.data && (
        <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_1fr]">
          <div>
            <div className="mb-2 flex flex-wrap gap-2">{Object.entries(changes.data.summary).map(([k, n]) => <Badge key={k} tone="violet">{k.replace(/_/g, " ")}: {n}</Badge>)}</div>
            {changes.data.features.length === 0 ? <EmptyState title="No change records" /> : (
              <div className="max-h-96 overflow-auto">
                <Table head={["Type", "Old", "New", "Detail"]}>
                  {changes.data.features.map((f) => {
                    const p = f.properties as Record<string, unknown>;
                    return <tr key={String(p.id)}><Td><StatusBadge value={String(p.change_type)} /></Td><Td>{String(p.old_feature_id ?? "—")}</Td>
                      <Td>{String(p.new_feature_id ?? "—")}</Td><Td className="text-xs">{String(p.explanation)}</Td></tr>;
                  })}
                </Table>
              </div>
            )}
          </div>
          <div className="h-96 overflow-hidden rounded border"><MapView layers={layers} /></div>
        </div>
      )}
    </Card>
  );
}

function BuildingExtraction({ pid }: { pid: number }) {
  const { hasRole } = useAuth();
  const versions = useAllVersions(pid);
  const rasters = (versions.data ?? []).filter((v) => v.dataset.kind === "imagery" && v.status === "processed");
  const [rv, setRv] = useState("");
  const [backend, setBackend] = useState("threshold_baseline");
  const [modelPath, setModelPath] = useState("");
  const [metaPath, setMetaPath] = useState("");
  const [jobId, setJobId] = useState<number | null>(null);
  const preds = usePredictions(pid);
  const run = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/vision/extract`, {
    raster_version_id: Number(rv), backend, model_path: modelPath || undefined, model_meta_path: metaPath || undefined }), [["jobs", pid]]);
  const decide = useApiMutation(({ id, decision }: { id: number; decision: string }) =>
    api.send("POST", `${projectPath(pid)}/vision/predictions/${id}/decision`, { decision }), [["predictions", pid]]);
  const layers = useMemo(() => [{ id: "pred", kind: "predicted" as const, data: preds.data,
    styleFn: (f: { properties: Record<string, unknown> }) => f.properties.review_status === "accepted" ? { color: "#059669", dashArray: "" }
      : f.properties.review_status === "rejected" ? { color: "#64748b", fillOpacity: 0.05 } : {} }], [preds.data]);
  return (
    <Card title="Building extraction from imagery">
      <Notice tone="amber">No pretrained building-segmentation weights ship with this repository (none could be verified as
        suitably licensed and reachable from the build environment). The <strong>threshold baseline is not machine learning</strong>; it exists to
        exercise the tiling and polygonisation pipeline. Provide an ONNX model and metadata file on the server to run real inference.</Notice>
      <form className="mt-2 grid gap-2 sm:grid-cols-5 sm:items-end" onSubmit={(e) => { e.preventDefault(); run.mutate(undefined, { onSuccess: (j) => setJobId(j.id) }); }}>
        <Field label="Imagery version"><Select aria-label="Imagery version" value={rv} onChange={(e) => setRv(e.target.value)}>
          <option value="">Select…</option>{rasters.map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
        <Field label="Backend"><Select value={backend} onChange={(e) => setBackend(e.target.value)}>
          <option value="threshold_baseline">Threshold baseline (non-ML)</option><option value="onnx">ONNX model</option></Select></Field>
        {backend === "onnx" && <>
          <Field label="Model path (server)"><Input value={modelPath} onChange={(e) => setModelPath(e.target.value)} /></Field>
          <Field label="Metadata JSON path"><Input value={metaPath} onChange={(e) => setMetaPath(e.target.value)} /></Field>
        </>}
        <Button type="submit" disabled={!rv || !hasRole("analyst")} loading={run.isPending}>Extract buildings</Button>
      </form>
      <ErrorBanner error={run.error} />
      <div className="my-2"><JobTracker projectId={pid} jobId={jobId} invalidate={[["predictions", pid], ["all-versions", pid]]} /></div>
      {preds.data && preds.data.features.length > 0 && (
        <div className="grid gap-3 lg:grid-cols-2">
          <div className="max-h-96 overflow-auto">
            <Table head={["Prediction", "Area", "Status", "Review"]}>
              {preds.data.features.map((f) => {
                const p = f.properties as { id: number; source_id_raw: string; area_m2: number; review_status: string };
                return (
                  <tr key={p.id}><Td>{p.source_id_raw}</Td><Td>{fmtNum(p.area_m2, 1)} m²</Td><Td><StatusBadge value={p.review_status} /></Td>
                    <Td>{hasRole("reviewer") && <span className="flex gap-1">
                      <Button variant="secondary" onClick={() => decide.mutate({ id: p.id, decision: "approve" })} disabled={p.review_status === "accepted"}>Accept</Button>
                      <Button variant="ghost" onClick={() => decide.mutate({ id: p.id, decision: "reject" })} disabled={p.review_status === "rejected"}>Reject</Button>
                    </span>}</Td></tr>
                );
              })}
            </Table>
            <ErrorBanner error={decide.error} />
          </div>
          <div className="h-96 overflow-hidden rounded border"><MapView layers={layers} /></div>
        </div>
      )}
    </Card>
  );
}

export function ChangesPage() {
  const pid = useProjectId();
  return <div className="space-y-4"><ChangeDetection pid={pid} /><BuildingExtraction pid={pid} /></div>;
}
