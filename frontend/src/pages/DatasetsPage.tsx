import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { api, errorMessage } from "../api/client";
import { projectPath, useApiMutation, useDatasets, useMappings, useVersions } from "../api/hooks";
import type { Dataset, DatasetVersion, Job, Mapping, UploadResult } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTracker } from "../components/JobTracker";
import {
  Badge, Button, Card, EmptyState, ErrorBanner, Field, Input, JsonView, LabeledInput, Loading, Notice, Select,
  StatusBadge, Table, Td, fmtBytes, fmtDate, fmtNum,
} from "../components/ui";
import { useProjectId } from "./ProjectLayout";

const KINDS = ["parcels", "buildings", "property_records", "utility_lines", "imagery", "other"] as const;
const CANONICAL = ["parcel_id", "ulpin", "property_id", "building_id", "land_use", "area_sqm", "surveyed_at",
  "updated_at", "owner_name", "ward", "village", "address", "building_levels", "height_m"];

const dsSchema = z.object({
  name: z.string().min(1, "Required"),
  kind: z.enum(KINDS),
  provenance: z.enum(["real", "synthetic", "derived"]),
  provider: z.string().optional(),
  license: z.string().optional(),
  source_url: z.string().optional(),
  attribution: z.string().optional(),
  reliability: z.coerce.number().min(0).max(1),
  positional_accuracy_m: z.union([z.literal(""), z.coerce.number().min(0)]).optional(),
  source_date: z.string().optional(),
});
type DsForm = z.infer<typeof dsSchema>;

function NewDatasetForm({ pid, onCreated }: { pid: number; onCreated: (d: Dataset) => void }) {
  const [err, setErr] = useState<unknown>(null);
  const { register, handleSubmit, formState, reset } = useForm<DsForm>({
    resolver: zodResolver(dsSchema), defaultValues: { kind: "parcels", provenance: "real", reliability: 0.5 },
  });
  const m = useApiMutation((b: Record<string, unknown>) => api.send<Dataset>("POST", `${projectPath(pid)}/datasets`, b),
    [["datasets", pid], ["metrics", pid]]);
  return (
    <form className="grid gap-2 sm:grid-cols-2" noValidate onSubmit={handleSubmit(async (v) => {
      setErr(null);
      try {
        const d = await m.mutateAsync({ ...v, positional_accuracy_m: v.positional_accuracy_m === "" ? null : v.positional_accuracy_m,
          source_date: v.source_date ? new Date(v.source_date).toISOString() : null });
        reset();
        onCreated(d);
      } catch (e) { setErr(e); }
    })}>
      <LabeledInput label="Name" {...register("name")} error={formState.errors.name?.message} />
      <Field label="Kind"><Select {...register("kind")}>{KINDS.map((k) => <option key={k}>{k}</option>)}</Select></Field>
      <Field label="Provenance" hint="Synthetic data is always labelled as such"><Select {...register("provenance")}>
        <option value="real">real</option><option value="synthetic">synthetic</option><option value="derived">derived</option></Select></Field>
      <LabeledInput label="Provider" {...register("provider")} />
      <LabeledInput label="Licence" {...register("license")} />
      <LabeledInput label="Source URL" {...register("source_url")} />
      <LabeledInput label="Attribution" {...register("attribution")} />
      <LabeledInput label="Reliability (0–1)" type="number" step="0.05" {...register("reliability")} error={formState.errors.reliability?.message} />
      <LabeledInput label="Positional accuracy (m)" type="number" step="0.1" {...register("positional_accuracy_m")} />
      <LabeledInput label="Source date" type="date" {...register("source_date")} />
      <div className="sm:col-span-2 space-y-2"><ErrorBanner error={err} /><Button type="submit" loading={m.isPending}>Create dataset</Button></div>
    </form>
  );
}

function UploadForm({ pid, dataset, onUploaded }: { pid: number; dataset: Dataset; onUploaded: (r: UploadResult) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [layer, setLayer] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const m = useApiMutation(async () => {
    const fd = new FormData();
    fd.append("file", file!);
    if (layer) fd.append("layer", layer);
    return api.upload<UploadResult>(`${projectPath(pid)}/datasets/${dataset.id}/versions`, fd);
  }, [["versions", pid, dataset.id], ["mappings", pid, dataset.id], ["all-versions", pid]]);
  return (
    <form className="flex flex-wrap items-end gap-2" onSubmit={async (e) => {
      e.preventDefault();
      setErr(null);
      try { onUploaded(await m.mutateAsync(undefined)); setFile(null); } catch (x) { setErr(x); }
    }}>
      <Field label="File (.geojson, .gpkg, .zip shapefile, .csv, .parquet, .tif)">
        <Input type="file" aria-label="Dataset file" onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          accept=".geojson,.json,.gpkg,.zip,.csv,.parquet,.geoparquet,.tif,.tiff,.kml,.fgb,.gml" />
      </Field>
      <LabeledInput label="Layer (optional)" value={layer} onChange={(e) => setLayer(e.target.value)} className="w-32" />
      <Button type="submit" disabled={!file} loading={m.isPending}>Upload new version</Button>
      <div className="w-full"><ErrorBanner error={err} title="Upload rejected" /></div>
    </form>
  );
}

function CrsForm({ pid, v, suggestion }: { pid: number; v: DatasetVersion; suggestion?: string | null }) {
  const [crs, setCrs] = useState(suggestion ?? "");
  const [why, setWhy] = useState("");
  const m = useApiMutation(() => api.send("POST", `${projectPath(pid)}/versions/${v.id}/crs`, { crs, justification: why }),
    [["versions", pid, v.dataset_id]]);
  return (
    <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3">
      <p className="text-sm text-amber-900"><strong>Coordinate reference system required.</strong> It will not be guessed.
        Confirm it from supplier metadata or survey control.{suggestion && <> Suggested from coordinate ranges: <code>{suggestion}</code> (verify!).</>}</p>
      <div className="grid gap-2 sm:grid-cols-[180px_1fr_auto] sm:items-end">
        <LabeledInput label="CRS" placeholder="EPSG:32643" value={crs} onChange={(e) => setCrs(e.target.value)} />
        <LabeledInput label="Evidence / justification" value={why} onChange={(e) => setWhy(e.target.value)}
          placeholder="e.g. supplier metadata sheet states WGS84" />
        <Button onClick={() => m.mutate(undefined)} loading={m.isPending} disabled={!crs || why.length < 3}>Confirm CRS</Button>
      </div>
      <ErrorBanner error={m.error} />
    </div>
  );
}

function MappingTable({ pid, did }: { pid: number; did: number }) {
  const maps = useMappings(pid, did);
  const { hasRole } = useAuth();
  const [err, setErr] = useState<string | null>(null);
  const decide = useApiMutation(({ m, status, target, confirm }: { m: Mapping; status: string; target?: string | null; confirm?: boolean }) =>
    api.send("PATCH", `${projectPath(pid)}/mappings/${m.id}`, { status, target_field: target ?? undefined, confirm_ambiguous: confirm }),
    [["mappings", pid, did]]);
  if (maps.isLoading) return <Loading />;
  if (!maps.data?.length) return <EmptyState title="No fields to map" />;
  const act = (vars: Parameters<typeof decide.mutate>[0]) => {
    setErr(null);
    decide.mutate(vars, { onError: (e) => setErr(errorMessage(e)) });
  };
  return (
    <div className="space-y-2">
      <p className="text-xs text-slate-500">Exact alias matches are approved by a deterministic rule; everything else requires a decision.
        Ambiguous suggestions must be explicitly confirmed. Approved mappings are reused for future datasets in this project.</p>
      {err && <ErrorBanner error={new Error(err)} />}
      <Table head={["Source field", "Suggested target", "Score", "Method", "Status", "Decision"]}>
        {maps.data.map((m) => (
          <tr key={m.id}>
            <Td className="font-mono text-xs">{m.source_field}</Td>
            <Td>
              <Select aria-label={`Target for ${m.source_field}`} defaultValue={m.target_field ?? ""} disabled={!hasRole("analyst")}
                onChange={(e) => act({ m, status: "approved", target: e.target.value || null })}>
                <option value="">— none —</option>
                {CANONICAL.map((c) => <option key={c} value={c}>{c}</option>)}
              </Select>
            </Td>
            <Td>{fmtNum(m.score)}</Td>
            <Td>{m.method}{m.ambiguous && <> <Badge tone="amber">ambiguous</Badge></>}</Td>
            <Td><StatusBadge value={m.status} /></Td>
            <Td className="whitespace-nowrap">
              {hasRole("analyst") && (
                <span className="flex gap-1">
                  <Button variant="secondary" disabled={!m.target_field || m.status === "approved"}
                    onClick={() => act({ m, status: "approved", confirm: m.ambiguous })}>{m.ambiguous ? "Confirm" : "Approve"}</Button>
                  <Button variant="ghost" disabled={m.status === "rejected"} onClick={() => act({ m, status: "rejected" })}>Reject</Button>
                </span>
              )}
            </Td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

function VersionPanel({ pid, v, suggestion }: { pid: number; v: DatasetVersion; suggestion?: string | null }) {
  const [jobId, setJobId] = useState<number | null>(null);
  const [filterSA, setFilterSA] = useState(false);
  const process = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/versions/${v.id}/process`,
    { study_area_filter: filterSA }), [["versions", pid, v.dataset_id]]);
  const stats = v.stats as Record<string, unknown>;
  const warnings = ((stats.inspection as { warnings?: string[] } | undefined)?.warnings ?? []).concat((stats.warnings as string[]) ?? []);
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <dt className="text-slate-500">File</dt><dd className="truncate" title={v.original_filename}>{v.original_filename}</dd>
        <dt className="text-slate-500">Format</dt><dd>{v.format}{v.layer ? ` · ${v.layer}` : ""}</dd>
        <dt className="text-slate-500">Size</dt><dd>{fmtBytes(v.size_bytes)}</dd>
        <dt className="text-slate-500">SHA-256</dt><dd className="truncate font-mono text-xs" title={v.file_sha256}>{v.file_sha256.slice(0, 16)}…</dd>
        <dt className="text-slate-500">Source CRS</dt><dd>{v.source_crs ?? <Badge tone="amber">unknown</Badge>}{v.crs_confirmed && <> <Badge tone="blue">confirmed</Badge></>}</dd>
        <dt className="text-slate-500">Geometry</dt><dd>{v.geometry_type ?? "none (table)"}</dd>
        <dt className="text-slate-500">Features</dt><dd>{v.feature_count?.toLocaleString() ?? "—"}</dd>
        <dt className="text-slate-500">Status</dt><dd><StatusBadge value={v.status} /></dd>
      </dl>
      {warnings.length > 0 && <Notice tone="amber"><ul className="list-disc pl-4">{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></Notice>}
      {v.error && <ErrorBanner error={new Error(v.error)} title="Processing error" />}
      {v.status === "needs_crs" && <CrsForm pid={pid} v={v} suggestion={suggestion} />}
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => process.mutate(undefined, { onSuccess: (j) => setJobId(j.id) })} loading={process.isPending}
          disabled={["queued", "processing", "needs_crs"].includes(v.status)}>
          {v.status === "processed" ? "Re-process" : "Run preprocessing"}
        </Button>
        <label className="flex items-center gap-1 text-sm"><input type="checkbox" checked={filterSA} onChange={(e) => setFilterSA(e.target.checked)} />
          Keep only features intersecting the study area</label>
        <a className="text-sm text-brand-700 underline" href="#" onClick={(e) => { e.preventDefault();
          void api.download(`${projectPath(pid)}/versions/${v.id}/download`, v.original_filename); }}>Download original</a>
      </div>
      <ErrorBanner error={process.error} />
      <JobTracker projectId={pid} jobId={jobId} invalidate={[["versions", pid, v.dataset_id], ["all-versions", pid]]} />
      {v.status === "processed" && !!stats.features_loaded && (
        <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
          {(["features_loaded", "invalid_geometries", "auto_repaired", "repairs_need_review", "exact_duplicates",
            "null_geometries", "excluded_outside_study_area"] as const).map((k) => (
            <div key={k} className="rounded bg-slate-50 p-2"><p className="text-xs text-slate-500">{k.replace(/_/g, " ")}</p><p className="font-semibold">{String(stats[k] ?? 0)}</p></div>
          ))}
        </div>
      )}
      {Object.keys(v.field_profile).length > 0 && (
        <details>
          <summary className="cursor-pointer text-sm font-medium">Schema & field profile ({Object.keys(v.schema).length} fields)</summary>
          <Table head={["Field", "Type", "Nulls", "Distinct", "Unique ratio", "Samples"]}>
            {Object.entries(v.field_profile).map(([f, p]) => (
              <tr key={f}><Td className="font-mono text-xs">{f}</Td><Td>{p.type}</Td><Td>{p.null_count}</Td><Td>{p.distinct_count}</Td>
                <Td>{fmtNum(p.unique_ratio)}</Td><Td className="max-w-xs truncate text-xs">{p.sample_values.join(", ")}</Td></tr>
            ))}
          </Table>
        </details>
      )}
      <details>
        <summary className="cursor-pointer text-sm font-medium">Processing history ({v.processing_log.length} steps)</summary>
        <JsonView value={v.processing_log} />
      </details>
    </div>
  );
}

export function DatasetsPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const datasets = useDatasets(pid);
  const [selected, setSelected] = useState<number | null>(null);
  const [lastUpload, setLastUpload] = useState<UploadResult | null>(null);
  const ds = datasets.data?.find((d) => d.id === selected) ?? null;
  const versions = useVersions(pid, selected);
  const archive = useApiMutation((mode: string) => api.send("DELETE", `${projectPath(pid)}/datasets/${selected}?mode=${mode}`),
    [["datasets", pid], ["metrics", pid]]);

  return (
    <div className="grid gap-4 xl:grid-cols-[340px_1fr]">
      <div className="space-y-4">
        <Card title="Datasets">
          {datasets.isLoading && <Loading />}
          <ErrorBanner error={datasets.error} />
          {datasets.data?.length === 0 && <EmptyState title="No datasets yet" />}
          <ul className="space-y-1">
            {datasets.data?.map((d) => (
              <li key={d.id}>
                <button onClick={() => { setSelected(d.id); setLastUpload(null); }}
                  className={`w-full rounded px-2 py-1.5 text-left text-sm ${d.id === selected ? "bg-brand-50 ring-1 ring-brand-500" : "hover:bg-slate-50"}`}>
                  <span className="font-medium">{d.name}</span>
                  <span className="ml-1 text-xs text-slate-500">{d.kind}</span> <StatusBadge value={d.provenance === "real" ? null : d.provenance} />
                </button>
              </li>
            ))}
          </ul>
        </Card>
        {hasRole("analyst") && <Card title="Register dataset"><NewDatasetForm pid={pid} onCreated={(d) => setSelected(d.id)} /></Card>}
      </div>
      <div className="space-y-4">
        {!ds ? <EmptyState title="Select a dataset">Choose a dataset to upload files, inspect metadata and process versions.</EmptyState> : (
          <>
            <Card title={ds.name} actions={hasRole("reviewer") && (
              <Button variant="secondary" onClick={() => archive.mutate("archive", { onSuccess: () => setSelected(null) })}>Archive</Button>)}>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
                <dt className="text-slate-500">Kind</dt><dd>{ds.kind}</dd>
                <dt className="text-slate-500">Provenance</dt><dd><StatusBadge value={ds.provenance} /></dd>
                <dt className="text-slate-500">Provider</dt><dd>{ds.provider || "—"}</dd>
                <dt className="text-slate-500">Licence</dt><dd>{ds.license || "—"}</dd>
                <dt className="text-slate-500">Reliability</dt><dd>{ds.reliability}</dd>
                <dt className="text-slate-500">Accuracy</dt><dd>{ds.positional_accuracy_m != null ? `${ds.positional_accuracy_m} m` : "unknown"}</dd>
                <dt className="text-slate-500">Source date</dt><dd>{fmtDate(ds.source_date)}</dd>
                <dt className="text-slate-500">Source URL</dt><dd className="truncate">{ds.source_url || "—"}</dd>
              </dl>
              <ErrorBanner error={archive.error} />
              {hasRole("analyst") && <div className="mt-3"><UploadForm pid={pid} dataset={ds} onUploaded={setLastUpload} /></div>}
              {lastUpload?.duplicate && <div className="mt-2"><Notice>{lastUpload.message}</Notice></div>}
            </Card>
            {versions.isLoading && <Loading />}
            {[...(versions.data ?? [])].reverse().map((v) => (
              <Card key={v.id} title={<span className="text-sm font-semibold">Version {v.version_no} <span className="font-normal text-slate-500">· id {v.id} · {fmtDate(v.created_at)}</span></span>}>
                <VersionPanel pid={pid} v={v} suggestion={lastUpload?.version.id === v.id ? lastUpload.inspection?.crs_suggestion : undefined} />
              </Card>
            ))}
            {ds.kind !== "imagery" && versions.data && versions.data.length > 0 && (
              <Card title="Attribute mapping"><MappingTable pid={pid} did={ds.id} /></Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}
