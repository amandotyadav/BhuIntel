import { useMemo, useState } from "react";
import { api } from "../api/client";
import { projectPath, useAllVersions, useApiMutation, useCandidates, useRuns } from "../api/hooks";
import type { Candidate, MatchRun, MatchType } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, EmptyState, ErrorBanner, Field, Input, Loading, Select, StatusBadge, Table, Td, fmtDate, fmtNum } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

const TYPES: { value: MatchType; label: string; left: string[]; right: string[] }[] = [
  { value: "parcel_parcel", label: "Parcels ↔ parcels", left: ["parcels"], right: ["parcels"] },
  { value: "building_building", label: "Buildings ↔ buildings", left: ["buildings", "predicted_buildings"], right: ["buildings", "predicted_buildings"] },
  { value: "building_parcel", label: "Buildings → parcels", left: ["buildings", "predicted_buildings"], right: ["parcels"] },
  { value: "record_feature", label: "Property records → parcels", left: ["property_records"], right: ["parcels"] },
];

function NewRunForm({ pid }: { pid: number }) {
  const versions = useAllVersions(pid);
  const [type, setType] = useState<MatchType>("parcel_parcel");
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");
  const [tol, setTol] = useState("");
  const [method, setMethod] = useState("deterministic");
  const spec = TYPES.find((t) => t.value === type)!;
  const processed = (versions.data ?? []).filter((v) => v.status === "processed");
  const opts = (kinds: string[]) => processed.filter((v) => kinds.includes(v.dataset.kind));
  const m = useApiMutation(() => api.send<MatchRun>("POST", `${projectPath(pid)}/match-runs`, {
    left_version_id: Number(left), right_version_id: Number(right), match_type: type, method,
    tolerance_m: tol ? Number(tol) : undefined,
  }), [["runs", pid]]);
  return (
    <form className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6 lg:items-end" onSubmit={(e) => { e.preventDefault(); m.mutate(undefined); }}>
      <Field label="Match type"><Select value={type} onChange={(e) => { setType(e.target.value as MatchType); setLeft(""); setRight(""); }}>
        {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}</Select></Field>
      <Field label="Left version"><Select aria-label="Left version" value={left} onChange={(e) => setLeft(e.target.value)}>
        <option value="">Select…</option>{opts(spec.left).map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
      <Field label="Right version"><Select aria-label="Right version" value={right} onChange={(e) => setRight(e.target.value)}>
        <option value="">Select…</option>{opts(spec.right).map((v) => <option key={v.id} value={v.id}>{v.dataset.name} v{v.version_no}</option>)}</Select></Field>
      <Field label="Tolerance (m)" hint="blank = project default"><Input type="number" step="0.1" min="0" value={tol} onChange={(e) => setTol(e.target.value)} /></Field>
      <Field label="Method"><Select value={method} onChange={(e) => setMethod(e.target.value)}>
        <option value="deterministic">Deterministic (explainable)</option><option value="random_forest">Random Forest (active model)</option></Select></Field>
      <Button type="submit" disabled={!left || !right} loading={m.isPending}>Run matching</Button>
      <div className="lg:col-span-6"><ErrorBanner error={m.error} /></div>
    </form>
  );
}

function CandidateRow({ c, pid, runId }: { c: Candidate; pid: number; runId: number }) {
  const [open, setOpen] = useState(false);
  const { hasRole } = useAuth();
  const decide = useApiMutation((decision: string) => api.send("POST", `${projectPath(pid)}/match-candidates/${c.id}/decision`,
    { decision }), [["candidates", pid, runId]]);
  const f = c.features as Record<string, number | null>;
  return (
    <>
      <tr>
        <Td><button className="text-brand-700 underline" onClick={() => setOpen(!open)} aria-expanded={open}>{c.left_id ?? c.left_feature_id}</button></Td>
        <Td>{c.right_id ?? c.right_feature_id}</Td>
        <Td><span className="font-semibold">{fmtNum(c.score)}</span> <span className="text-xs text-slate-500" title={c.score_kind}>{c.score_kind === "heuristic_match_score" ? "heuristic" : "model (uncal.)"}</span></Td>
        <Td><StatusBadge value={c.match_class} /></Td>
        <Td>{c.relationship?.replace(/_/g, " ") ?? "—"}</Td>
        <Td className="text-xs">{f.iou != null && `IoU ${fmtNum(f.iou)} · `}{f.centroid_distance_m != null && `Δ ${fmtNum(f.centroid_distance_m, 1)} m · `}
          {f.building_coverage != null && `cover ${fmtNum(f.building_coverage)} · `}{f.id_similarity != null && `id ${fmtNum(f.id_similarity)}`}</Td>
        <Td><StatusBadge value={c.status} />{c.label != null && <> <Badge tone="violet">label {c.label}</Badge></>}</Td>
        <Td className="whitespace-nowrap">{hasRole("reviewer") && (
          <span className="flex gap-1">
            <Button variant="secondary" onClick={() => decide.mutate("approve")} disabled={c.status === "accepted"}>Accept</Button>
            <Button variant="ghost" onClick={() => decide.mutate("reject")} disabled={c.status === "rejected"}>Reject</Button>
          </span>)}
          {decide.error && <ErrorBanner error={decide.error} />}
        </Td>
      </tr>
      {open && (
        <tr><Td className="bg-slate-50" /><td colSpan={7} className="bg-slate-50 px-3 py-2 text-xs">
          <ul className="list-disc space-y-0.5 pl-4">{c.explanation.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </td></tr>
      )}
    </>
  );
}

export function MatchingPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const runs = useRuns(pid);
  const [runId, setRunId] = useState<number | null>(null);
  const [cls, setCls] = useState("");
  const [offset, setOffset] = useState(0);
  const filters = useMemo(() => ({ ...(cls ? { match_class: cls } : {}), limit: 100, offset }), [cls, offset]);
  const cands = useCandidates(pid, runId, filters);
  const run = runs.data?.find((r) => r.id === runId);
  return (
    <div className="space-y-4">
      {hasRole("analyst") && <Card title="New match run"><NewRunForm pid={pid} /></Card>}
      <Card title="Match runs">
        {runs.isLoading && <Loading />}
        {runs.data?.length === 0 && <EmptyState title="No runs yet" />}
        {!!runs.data?.length && (
          <Table head={["Run", "Type", "Method", "Status", "Candidates", "Matches", "Unmatched L/R", "Conflicts", "Created"]}>
            {runs.data.map((r) => (
              <tr key={r.id} className={r.id === runId ? "bg-brand-50" : ""}>
                <Td><button className="text-brand-700 underline" onClick={() => { setRunId(r.id); setOffset(0); }}>#{r.id}</button></Td>
                <Td>{r.match_type}</Td><Td>{r.method}</Td><Td><StatusBadge value={r.status} /></Td>
                <Td>{r.stats.candidates ?? "—"}</Td><Td>{r.stats.by_class?.match ?? 0}</Td>
                <Td>{r.stats.left_unmatched ?? "—"} / {r.stats.right_unmatched ?? "—"}</Td>
                <Td className="text-xs">{Object.entries(r.stats.conflicts ?? {}).map(([k, n]) => `${k.replace(/_/g, " ")}: ${n}`).join(", ") || "—"}</Td>
                <Td className="text-xs">{fmtDate(r.created_at)}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      {run && (
        <Card title={`Candidates for run #${run.id}`} actions={
          <Select aria-label="Filter class" value={cls} onChange={(e) => { setCls(e.target.value); setOffset(0); }} className="w-40">
            <option value="">All classes</option><option value="match">match</option><option value="possible">possible</option><option value="non_match">non-match</option>
          </Select>}>
          <p className="mb-2 text-xs text-slate-500">Tolerance {String(run.params.effective_tolerance_m ?? run.params.tolerance_m)} m (effective). Scores are
            {run.stats.score_kind === "heuristic_match_score" ? " heuristic weighted-evidence scores, not probabilities." : " uncalibrated model probabilities."} Click an id for the explanation.</p>
          {cands.isLoading && <Loading />}
          <ErrorBanner error={cands.error} />
          {cands.data && (
            <>
              <Table head={["Left", "Right", "Score", "Class", "Relationship", "Evidence", "Status", "Review"]} empty={cands.data.items.length === 0}>
                {cands.data.items.map((c) => <CandidateRow key={c.id} c={c} pid={pid} runId={run.id} />)}
              </Table>
              <div className="mt-2 flex items-center gap-2 text-sm">
                <Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</Button>
                <span>{offset + 1}–{Math.min(offset + 100, cands.data.total)} of {cands.data.total}</span>
                <Button variant="secondary" disabled={offset + 100 >= cands.data.total} onClick={() => setOffset(offset + 100)}>Next</Button>
              </div>
            </>
          )}
        </Card>
      )}
    </div>
  );
}
