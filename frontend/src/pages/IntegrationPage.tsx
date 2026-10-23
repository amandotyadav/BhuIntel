import { useState } from "react";
import { api } from "../api/client";
import { projectPath, useAllVersions, useApiMutation, useIntegrated, useJobs, useRuns } from "../api/hooks";
import type { Job } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTracker } from "../components/JobTracker";
import { Button, Card, ErrorBanner, Field, JsonView, Notice, Select, Stat, StatusBadge, Table, Td, fmtDate } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

export function IntegrationPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const versions = useAllVersions(pid);
  const runs = useRuns(pid);
  const jobs = useJobs(pid);
  const parcels = useIntegrated(pid, "parcel");
  const buildings = useIntegrated(pid, "building");
  const [form, setForm] = useState<Record<string, string>>({});
  const [buildJob, setBuildJob] = useState<number | null>(null);
  const [exportJob, setExportJob] = useState<number | null>(null);
  const [fmt, setFmt] = useState("gpkg");
  const set = (k: string) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const parcelVersions = (versions.data ?? []).filter((v) => v.dataset.kind === "parcels" && v.status === "processed");
  const runsOf = (t: string) => (runs.data ?? []).filter((r) => r.match_type === t && r.status === "succeeded");
  const num = (k: string) => (form[k] ? Number(form[k]) : undefined);
  const build = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/integration/build`, {
    primary_parcel_version_id: num("primary"), parcel_match_run_id: num("pp"), record_match_run_id: num("rec"),
    building_match_run_id: num("bb"), building_parcel_run_id: num("bp") }), [["jobs", pid]]);
  const exp = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/exports`, { format: fmt }), [["jobs", pid]]);
  const exportsDone = (jobs.data ?? []).filter((j) => j.job_type === "export");
  const runSelect = (k: string, t: string, label: string) => (
    <Field label={label}><Select aria-label={label} value={form[k] ?? ""} onChange={set(k)}>
      <option value="">(none)</option>{runsOf(t).map((r) => <option key={r.id} value={r.id}>Run #{r.id} ({r.stats.by_class?.match ?? 0} matches)</option>)}</Select></Field>
  );
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Integrated parcels" value={parcels.data?.numberMatched ?? "…"} />
        <Stat label="Integrated buildings" value={buildings.data?.numberMatched ?? "…"} />
      </div>
      {hasRole("reviewer") && (
        <Card title="Build / synchronise integrated layer">
          <Notice>The integrated layer is derived and versioned: unchanged features keep their version, changed ones get a new version with
            history, and disappeared ones are retired (never deleted). Approved corrections are re-applied. Personal data (owner names) is not copied.</Notice>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
            <Field label="Primary parcel version"><Select aria-label="Primary parcel version" value={form.primary ?? ""} onChange={set("primary")}>
              <option value="">Select…</option>{parcelVersions.map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
            {runSelect("pp", "parcel_parcel", "Parcel match run")}
            {runSelect("rec", "record_feature", "Record match run")}
            {runSelect("bb", "building_building", "Building match run")}
            {runSelect("bp", "building_parcel", "Building→parcel run")}
          </div>
          <Button className="mt-3" disabled={!form.primary} loading={build.isPending}
            onClick={() => build.mutate(undefined, { onSuccess: (j) => setBuildJob(j.id) })}>Build integrated layer</Button>
          <ErrorBanner error={build.error} />
          <div className="mt-2"><JobTracker projectId={pid} jobId={buildJob} invalidate={[["integrated", pid]]} /></div>
        </Card>
      )}
      <Card title="Export">
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Format"><Select value={fmt} onChange={(e) => setFmt(e.target.value)}><option value="gpkg">GeoPackage</option><option value="geojson">GeoJSON</option></Select></Field>
          <Button disabled={!hasRole("analyst")} loading={exp.isPending} onClick={() => exp.mutate(undefined, { onSuccess: (j) => setExportJob(j.id) })}>Create export</Button>
        </div>
        <ErrorBanner error={exp.error} />
        <div className="mt-2"><JobTracker projectId={pid} jobId={exportJob} invalidate={[["jobs", pid]]} /></div>
        <p className="mt-2 text-xs text-slate-500">Every export is reloaded with GDAL after writing and validated (feature counts, CRS, geometry validity,
          required lineage/provenance attributes).</p>
        <Table head={["Job", "Status", "Format", "Counts", "Reload validation", "Created", ""]} empty={exportsDone.length === 0}>
          {exportsDone.map((j) => {
            const r = j.result as { format?: string; counts?: Record<string, number>; validation?: { ok: boolean; errors: string[] }; filename?: string };
            return (
              <tr key={j.id}><Td>#{j.id}</Td><Td><StatusBadge value={j.status} /></Td><Td>{r.format ?? String(j.params.format)}</Td>
                <Td className="text-xs">{r.counts ? JSON.stringify(r.counts) : "—"}</Td>
                <Td>{r.validation ? (r.validation.ok ? <StatusBadge value="approved" /> : <span className="text-xs text-rose-700">{r.validation.errors.join("; ")}</span>) : "—"}</Td>
                <Td className="text-xs">{fmtDate(j.created_at)}</Td>
                <Td>{j.status === "succeeded" && <Button variant="secondary" onClick={() => api.download(`${projectPath(pid)}/exports/${j.id}/download`, r.filename ?? "export")}>Download</Button>}</Td></tr>
            );
          })}
        </Table>
      </Card>
      {parcels.data && parcels.data.features.length > 0 && (
        <Card title="Sample integrated parcel (lineage)"><JsonView value={parcels.data.features[0].properties} /></Card>
      )}
    </div>
  );
}
