import { useState } from "react";
import { api } from "../api/client";
import { projectPath, useApiMutation, useMlSchema, useModels } from "../api/hooks";
import type { Job, Metrics, MatchType } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTracker } from "../components/JobTracker";
import { Badge, Button, Card, EmptyState, ErrorBanner, Field, Input, Loading, Notice, Select, StatusBadge, Table, Td, fmtDate, fmtNum } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

function MetricsTable({ m, base }: { m?: Metrics; base?: Metrics & { rule: string } }) {
  if (!m) return null;
  const row = (label: string, x?: Metrics) => x && (
    <tr><Td>{label}</Td><Td>{fmtNum(x.precision, 3)}</Td><Td>{fmtNum(x.recall, 3)}</Td><Td>{fmtNum(x.f1, 3)}</Td><Td>{x.support}</Td>
      <Td className="font-mono text-xs">{JSON.stringify(x.confusion_matrix.matrix)}</Td></tr>
  );
  return (
    <Table head={["Predictor", "Precision", "Recall", "F1", "Test n", "Confusion [[TN,FP],[FN,TP]]"]}>
      {row("Random Forest", m)}{row(`Deterministic baseline (${base?.rule ?? ""})`, base)}
    </Table>
  );
}

export function ModelsPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const schema = useMlSchema(pid);
  const models = useModels(pid);
  const [mt, setMt] = useState<MatchType>("building_building");
  const [jobId, setJobId] = useState<number | null>(null);
  const [labels, setLabels] = useState<File | null>(null);
  const train = useApiMutation(() => api.send<Job>("POST", `${projectPath(pid)}/ml/train`, { match_type: mt }), [["jobs", pid]]);
  const activate = useApiMutation((id: number) => api.send("POST", `${projectPath(pid)}/ml/models/${id}/activate`), [["models", pid]]);
  const importLabels = useApiMutation(() => { const fd = new FormData(); fd.append("file", labels!);
    return api.upload<{ imported: number; errors: unknown[] }>(`${projectPath(pid)}/ml/labels/import`, fd); }, [["ml-schema", pid]]);
  return (
    <div className="space-y-4">
      <Card title="Training data">
        <Notice>Labels come from reviewer decisions on match candidates (accept = 1, reject = 0) or from an imported CSV
          (<code>candidate_id,label</code>). A model is trained only when at least {schema.data?.min_labels ?? "…"} labels with
          {" "}{schema.data?.min_per_class ?? "…"} per class exist; otherwise deterministic matching stays in use. Splits are grouped by
          spatial cells to avoid leakage. Probabilities are uncalibrated.</Notice>
        {schema.isLoading && <Loading />}
        {schema.data && (
          <Table head={["Match type", "Features", "Labelled match", "Labelled non-match", "Export"]}>
            {Object.entries(schema.data.feature_schema).map(([k, feats]) => (
              <tr key={k}><Td>{k}</Td><Td className="text-xs">{feats.join(", ")}</Td><Td>{schema.data!.labelled_counts[k]?.match ?? 0}</Td>
                <Td>{schema.data!.labelled_counts[k]?.non_match ?? 0}</Td>
                <Td><Button variant="ghost" onClick={() => api.download(`${projectPath(pid)}/ml/labels.csv?match_type=${k}`, `labels_${k}.csv`)}>CSV</Button></Td></tr>
            ))}
          </Table>
        )}
        {hasRole("reviewer") && (
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <Field label="Import labels CSV"><Input type="file" accept=".csv" onChange={(e) => setLabels(e.target.files?.[0] ?? null)} /></Field>
            <Button variant="secondary" disabled={!labels} loading={importLabels.isPending} onClick={() => importLabels.mutate(undefined)}>Import</Button>
            {importLabels.data && <span className="text-sm">Imported {importLabels.data.imported}; errors {importLabels.data.errors.length}</span>}
            <ErrorBanner error={importLabels.error} />
          </div>
        )}
      </Card>
      {hasRole("reviewer") && (
        <Card title="Train Random Forest">
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Match type"><Select value={mt} onChange={(e) => setMt(e.target.value as MatchType)}>
              {["building_building", "parcel_parcel", "building_parcel", "record_feature"].map((x) => <option key={x}>{x}</option>)}</Select></Field>
            <Button onClick={() => train.mutate(undefined, { onSuccess: (j) => setJobId(j.id) })} loading={train.isPending}>Train</Button>
          </div>
          <ErrorBanner error={train.error} />
          <div className="mt-2"><JobTracker projectId={pid} jobId={jobId} invalidate={[["models", pid]]} /></div>
        </Card>
      )}
      <Card title="Model versions">
        {models.isLoading && <Loading />}
        {models.data?.length === 0 && <EmptyState title="No model versions">Until a model is trained and activated, matching uses the deterministic baseline.</EmptyState>}
        <div className="space-y-3">
          {models.data?.map((m) => (
            <div key={m.id} className="rounded border border-slate-200 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{m.name} v{m.version}</span><StatusBadge value={m.status} />
                {m.is_active && <Badge tone="green">active</Badge>}
                <span className="text-xs text-slate-500">{fmtDate(m.created_at)}</span>
                {hasRole("admin") && m.status === "trained" && !m.is_active &&
                  <Button variant="secondary" onClick={() => activate.mutate(m.id)}>Activate</Button>}
              </div>
              {m.status === "insufficient_data" && <p className="text-sm text-amber-800">{String(m.training_info.message ?? "")} Labels: {JSON.stringify(m.training_info.class_distribution)}</p>}
              {m.metrics.model && (
                <>
                  <p className="mt-1 text-xs text-slate-600">Evaluated on {m.metrics.evaluated_on}; split: {String(m.training_info.split)}; calibration: {m.metrics.probability_calibration}</p>
                  {m.metrics.evaluated_on?.includes("synthetic") && <Notice tone="amber">Metrics computed on synthetic fixture labels: not evidence of real-world accuracy.</Notice>}
                  <MetricsTable m={m.metrics.model} base={m.metrics.deterministic_baseline} />
                  <p className="mt-1 text-xs">Feature importance: {Object.entries(m.metrics.feature_importance ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 5)
                    .map(([k, v]) => `${k} ${v.toFixed(2)}`).join(" · ")}</p>
                </>
              )}
            </div>
          ))}
        </div>
        <ErrorBanner error={activate.error} />
      </Card>
    </div>
  );
}
