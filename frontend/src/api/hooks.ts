import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { FeatureCollection } from "./geojson";
import type {
  AcquisitionSource, AuditEvent, Candidate, ConflictDetail, ConflictList, Correction, Dataset, DatasetVersion, Job,
  Manifest, Mapping, MatchRun, MetricsSummary, ModelVersion, Project,
} from "./types";

const P = (pid: number) => `/api/v1/projects/${pid}`;

export const useProjects = () => useQuery({ queryKey: ["projects"], queryFn: () => api.get<Project[]>("/api/v1/projects") });
export const useProject = (pid: number) =>
  useQuery({ queryKey: ["project", pid], queryFn: () => api.get<Project>(P(pid)) });
export const useMetrics = (pid: number) =>
  useQuery({ queryKey: ["metrics", pid], queryFn: () => api.get<MetricsSummary>(`${P(pid)}/metrics`) });
export const useStudyAreas = (pid: number) =>
  useQuery({ queryKey: ["study-areas", pid], queryFn: () => api.get<FeatureCollection>(`${P(pid)}/study-areas`) });
export const useDatasets = (pid: number) =>
  useQuery({ queryKey: ["datasets", pid], queryFn: () => api.get<Dataset[]>(`${P(pid)}/datasets`) });
export const useVersions = (pid: number, did: number | null) =>
  useQuery({
    queryKey: ["versions", pid, did],
    queryFn: () => api.get<DatasetVersion[]>(`${P(pid)}/datasets/${did}/versions`),
    enabled: did != null,
    refetchInterval: (q) =>
      q.state.data?.some((v) => ["queued", "processing"].includes(v.status)) ? 1500 : false,
  });
export const useMappings = (pid: number, did: number | null) =>
  useQuery({
    queryKey: ["mappings", pid, did],
    queryFn: () => api.get<Mapping[]>(`${P(pid)}/datasets/${did}/mappings`),
    enabled: did != null,
  });

/** All processed versions across datasets (for selectors). */
export function useAllVersions(pid: number) {
  const ds = useDatasets(pid);
  return useQuery({
    queryKey: ["all-versions", pid, ds.data?.map((d) => d.id).join(",")],
    enabled: !!ds.data,
    queryFn: async () => {
      const lists = await Promise.all(
        (ds.data ?? []).map(async (d) => (await api.get<DatasetVersion[]>(`${P(pid)}/datasets/${d.id}/versions`)).map(
          (v) => ({ ...v, dataset: d }))),
      );
      return lists.flat();
    },
  });
}

export const useJobs = (pid: number) =>
  useQuery({
    queryKey: ["jobs", pid],
    queryFn: () => api.get<Job[]>(`${P(pid)}/jobs`),
    refetchInterval: (q) => (q.state.data?.some((j) => ["queued", "running"].includes(j.status)) ? 1500 : 10000),
  });

export const useJob = (pid: number, jobId: number | null) =>
  useQuery({
    queryKey: ["job", pid, jobId],
    queryFn: () => api.get<Job>(`${P(pid)}/jobs/${jobId}`),
    enabled: jobId != null,
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false),
  });

export const useRuns = (pid: number) =>
  useQuery({
    queryKey: ["runs", pid],
    queryFn: () => api.get<MatchRun[]>(`${P(pid)}/match-runs`),
    refetchInterval: (q) => (q.state.data?.some((r) => ["queued", "running"].includes(r.status)) ? 1500 : false),
  });

export const useCandidates = (pid: number, runId: number | null, filters: Record<string, string | number>) =>
  useQuery({
    queryKey: ["candidates", pid, runId, filters],
    queryFn: () => api.get<{ total: number; items: Candidate[] }>(`${P(pid)}/match-runs/${runId}/candidates`, filters),
    enabled: runId != null,
  });

export const useConflicts = (pid: number, filters: Record<string, string | number | undefined>) =>
  useQuery({ queryKey: ["conflicts", pid, filters], queryFn: () => api.get<ConflictList>(`${P(pid)}/conflicts`, filters) });

export const useConflict = (pid: number, id: number | null) =>
  useQuery({
    queryKey: ["conflict", pid, id],
    queryFn: () => api.get<ConflictDetail>(`${P(pid)}/conflicts/${id}`),
    enabled: id != null,
  });

export const useCorrections = (pid: number, status?: string) =>
  useQuery({
    queryKey: ["corrections", pid, status],
    queryFn: () => api.get<FeatureCollection<Correction>>(`${P(pid)}/corrections`, { status }),
  });

export const useQueue = (pid: number) =>
  useQuery({
    queryKey: ["queue", pid],
    queryFn: () => api.get<{ conflicts_by_status: { status: string; n: number }[];
      corrections_by_status: { status: string; safety: string; n: number }[];
      predictions_by_status: { status: string; n: number }[] }>(`${P(pid)}/review/queue`),
  });

export const useFeatures = (pid: number, versionId: number | null, enabled = true) =>
  useQuery({
    queryKey: ["features", pid, versionId],
    queryFn: () => api.get<FeatureCollection>(`${P(pid)}/versions/${versionId}/features`, { limit: 5000 }),
    enabled: enabled && versionId != null,
  });

export const useIntegrated = (pid: number, featureClass?: string) =>
  useQuery({
    queryKey: ["integrated", pid, featureClass],
    queryFn: () => api.get<FeatureCollection>(`${P(pid)}/integrated`, { feature_class: featureClass, limit: 5000 }),
  });

export const useChanges = (pid: number, params: Record<string, number | undefined>) =>
  useQuery({
    queryKey: ["changes", pid, params],
    queryFn: () => api.get<FeatureCollection & { summary: Record<string, number> }>(`${P(pid)}/changes`, params),
  });

export const usePredictions = (pid: number) =>
  useQuery({ queryKey: ["predictions", pid], queryFn: () => api.get<FeatureCollection>(`${P(pid)}/vision/predictions`) });

export const useModels = (pid: number) =>
  useQuery({ queryKey: ["models", pid], queryFn: () => api.get<ModelVersion[]>(`${P(pid)}/ml/models`) });

export const useMlSchema = (pid: number) =>
  useQuery({
    queryKey: ["ml-schema", pid],
    queryFn: () => api.get<{ feature_schema: Record<string, string[]>; min_labels: number; min_per_class: number;
      labelled_counts: Record<string, { match: number; non_match: number }> }>(`${P(pid)}/ml/schema`),
  });

export const useSources = (pid: number) =>
  useQuery({ queryKey: ["sources", pid], queryFn: () => api.get<AcquisitionSource[]>(`${P(pid)}/acquisition/sources`) });

export const useManifests = (pid: number) =>
  useQuery({ queryKey: ["manifests", pid], queryFn: () => api.get<Manifest[]>(`${P(pid)}/acquisition/manifests`) });

export const useAudit = (pid: number) =>
  useQuery({ queryKey: ["audit", pid], queryFn: () => api.get<AuditEvent[]>(`${P(pid)}/audit`, { limit: 300 }) });

/** Generic mutation that invalidates the given query-key prefixes on success. */
export function useApiMutation<TVars, TRes = unknown>(fn: (v: TVars) => Promise<TRes>, invalidate: unknown[][]) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => invalidate.forEach((k) => qc.invalidateQueries({ queryKey: k })),
  });
}

export { P as projectPath };
