import { api } from "../api/client";
import { projectPath, useApiMutation, useJobs } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { Button, Card, ErrorBanner, JsonView, Loading, Progress, StatusBadge, Table, Td, fmtDate } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

export function JobsPage() {
  const pid = useProjectId();
  const { hasRole } = useAuth();
  const jobs = useJobs(pid);
  const retry = useApiMutation((id: number) => api.send("POST", `${projectPath(pid)}/jobs/${id}/retry`), [["jobs", pid]]);
  const cancel = useApiMutation((id: number) => api.send("POST", `${projectPath(pid)}/jobs/${id}/cancel`), [["jobs", pid]]);
  return (
    <Card title="Processing jobs">
      {jobs.isLoading && <Loading />}
      <ErrorBanner error={jobs.error || retry.error || cancel.error} />
      <Table head={["#", "Type", "Status", "Progress", "Message / error", "Attempts", "Created", "Finished", ""]} empty={jobs.data?.length === 0}>
        {jobs.data?.map((j) => (
          <tr key={j.id}>
            <Td>{j.id}</Td><Td>{j.job_type.replace(/_/g, " ")}</Td><Td><StatusBadge value={j.status} /></Td>
            <Td className="w-32">{["queued", "running"].includes(j.status) ? <Progress value={j.progress} /> : `${Math.round(j.progress * 100)}%`}</Td>
            <Td className="max-w-md text-xs">{j.error ? <span className="text-rose-700">{j.error}</span> : j.message}
              {Object.keys(j.result).length > 0 && <details><summary className="cursor-pointer">result</summary><JsonView value={j.result} /></details>}</Td>
            <Td>{j.attempts}</Td><Td className="text-xs">{fmtDate(j.created_at)}</Td><Td className="text-xs">{fmtDate(j.finished_at)}</Td>
            <Td>{hasRole("analyst") && (["failed", "partial", "cancelled"].includes(j.status) ?
              <Button variant="secondary" onClick={() => retry.mutate(j.id)}>Retry</Button> :
              j.status === "queued" ? <Button variant="ghost" onClick={() => cancel.mutate(j.id)}>Cancel</Button> : null)}</Td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}
