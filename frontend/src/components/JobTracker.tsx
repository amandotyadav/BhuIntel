import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useJob } from "../api/hooks";
import { Progress, StatusBadge } from "./ui";

/** Shows live progress for a background job (values reported by the job itself) and refreshes data when done. */
export function JobTracker({ projectId, jobId, invalidate = [], onDone }: { projectId: number; jobId: number | null;
  invalidate?: unknown[][]; onDone?: (status: string, result: Record<string, unknown>) => void }) {
  const { data: job } = useJob(projectId, jobId);
  const qc = useQueryClient();
  const finished = job && !["queued", "running"].includes(job.status);
  useEffect(() => {
    if (!finished || !job) return;
    invalidate.forEach((k) => qc.invalidateQueries({ queryKey: k }));
    qc.invalidateQueries({ queryKey: ["jobs", projectId] });
    qc.invalidateQueries({ queryKey: ["metrics", projectId] });
    onDone?.(job.status, job.result);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished, job?.id]);
  if (!jobId || !job) return null;
  return (
    <div className="space-y-1 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs" aria-live="polite">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-slate-700">Job #{job.id} · {job.job_type.replace(/_/g, " ")}</span>
        <StatusBadge value={job.status} />
      </div>
      {!finished && <Progress value={job.progress} label={`job ${job.id} progress`} />}
      <p className="text-slate-600">{job.error ?? job.message}</p>
    </div>
  );
}
