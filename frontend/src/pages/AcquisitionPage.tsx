import { useState } from "react";
import { api } from "../api/client";
import { projectPath, useApiMutation, useManifests, useSources } from "../api/hooks";
import type { Job } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTracker } from "../components/JobTracker";
import { Badge, Button, Card, ErrorBanner, JsonView, Loading, Notice, StatusBadge, Table, Td, fmtDate } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

export function AcquisitionPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const sources = useSources(pid);
  const manifests = useManifests(pid);
  const [jobId, setJobId] = useState<number | null>(null);
  const run = useApiMutation((source_key: string) => api.send<Job>("POST", `${projectPath(pid)}/acquisition`, { source_key }), [["jobs", pid]]);
  return (
    <div className="space-y-4">
      <Notice>Only open, documented sources are fetched automatically, and only the tiles/area needed for the active study area.
        Sources requiring authorisation are never accessed automatically: obtain authorised exports and upload them on the Datasets page.
        If a source is unreachable the platform reports it; it never substitutes fake data.</Notice>
      <Card title="Sources">
        {sources.isLoading && <Loading />}
        <Table head={["Source", "Access", "Licence", "Notes", ""]}>
          {sources.data?.map((s) => (
            <tr key={s.key}>
              <Td><a className="font-medium text-brand-700 underline" href={s.homepage} target="_blank" rel="noreferrer">{s.name}</a>
                <p className="text-xs text-slate-500">{s.provider} · {s.kind}</p></Td>
              <Td><Badge tone={s.access === "open" ? "green" : s.access === "credentials" ? "blue" : "amber"}>{s.access.replace(/_/g, " ")}</Badge>
                {s.automated ? <p className="text-xs text-slate-500">automated</p> : <p className="text-xs text-slate-500">manual import</p>}</Td>
              <Td className="text-xs">{s.license}</Td>
              <Td className="max-w-md text-xs">{s.notes}</Td>
              <Td>{hasRole("analyst") && <Button variant="secondary" onClick={() => run.mutate(s.key, { onSuccess: (j) => setJobId(j.id) })}>
                {s.automated ? "Acquire" : "Record request"}</Button>}</Td>
            </tr>
          ))}
        </Table>
        <ErrorBanner error={run.error} />
        <div className="mt-2"><JobTracker projectId={pid} jobId={jobId} invalidate={[["manifests", pid], ["datasets", pid]]} /></div>
      </Card>
      <Card title="Acquisition manifests">
        <Table head={["#", "Source", "Status", "Reachable", "Message", "Files", "Created"]} empty={manifests.data?.length === 0}>
          {manifests.data?.map((m) => (
            <tr key={m.id}><Td>{m.id}</Td><Td>{m.source_key}</Td><Td><StatusBadge value={m.status} /></Td>
              <Td>{m.reachable == null ? "—" : m.reachable ? "yes" : "no"}</Td><Td className="max-w-md text-xs">{m.message}</Td>
              <Td>{m.files.length ? <details><summary className="cursor-pointer text-xs">{m.files.length} file(s)</summary><JsonView value={m.files} /></details> : "—"}</Td>
              <Td className="text-xs">{fmtDate(m.created_at)}</Td></tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
