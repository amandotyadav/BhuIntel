import type { Feature, FeatureCollection, Geometry } from "./geojson";

export type Role = "viewer" | "analyst" | "reviewer" | "admin";

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
}

export interface Project {
  id: number;
  name: string;
  description: string;
  owner_id: number;
  working_crs: number;
  default_tolerance_m: number;
  settings: Record<string, unknown>;
  status: string;
  created_at: string;
}

export type DatasetKind =
  | "parcels"
  | "buildings"
  | "predicted_buildings"
  | "property_records"
  | "utility_lines"
  | "imagery"
  | "other";

export interface Dataset {
  id: number;
  project_id: number;
  name: string;
  kind: DatasetKind;
  provenance: "real" | "synthetic" | "derived";
  provider: string;
  source_url: string;
  license: string;
  attribution: string;
  description: string;
  reliability: number;
  positional_accuracy_m: number | null;
  source_date: string | null;
  status: string;
  created_at: string;
}

export interface FieldProfile {
  type: string;
  null_count: number;
  non_null_count: number;
  distinct_count: number;
  unique_ratio: number;
  sample_values: string[];
  numeric_parse_rate?: number;
  date_parse_rate?: number;
  min?: number;
  max?: number;
}

export interface LogEntry {
  ts?: string;
  step: string;
  [k: string]: unknown;
}

export interface DatasetVersion {
  id: number;
  dataset_id: number;
  version_no: number;
  file_sha256: string;
  original_filename: string;
  format: string;
  size_bytes: number;
  layer: string | null;
  source_crs: string | null;
  crs_confirmed: boolean;
  geometry_type: string | null;
  feature_count: number | null;
  schema: Record<string, string>;
  field_profile: Record<string, FieldProfile>;
  csv_options: Record<string, string>;
  status: string;
  error: string | null;
  processing_log: LogEntry[];
  stats: Record<string, unknown>;
  created_at: string;
  processed_at: string | null;
}

export interface UploadResult {
  version: DatasetVersion;
  duplicate: boolean;
  message?: string;
  inspection?: {
    warnings: string[];
    crs_name: string | null;
    crs_is_projected: boolean | null;
    crs_suggestion: string | null;
    layers: string[];
    extent_4326: number[] | null;
  };
}

export interface Mapping {
  id: number;
  dataset_id: number;
  source_field: string;
  target_field: string | null;
  method: string;
  score: number;
  evidence: Record<string, unknown>;
  ambiguous: boolean;
  status: "suggested" | "approved" | "rejected";
}

export interface Job {
  id: number;
  project_id: number | null;
  job_type: string;
  params: Record<string, unknown>;
  status: "queued" | "running" | "succeeded" | "failed" | "partial" | "cancelled";
  progress: number;
  message: string;
  result: Record<string, unknown>;
  error: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type MatchType = "building_building" | "parcel_parcel" | "building_parcel" | "record_feature";

export interface MatchRun {
  id: number;
  project_id: number;
  left_version_id: number;
  right_version_id: number;
  match_type: MatchType;
  method: string;
  model_version_id: number | null;
  params: Record<string, unknown>;
  stats: {
    candidates?: number;
    by_class?: Record<string, number>;
    by_relationship?: Record<string, number>;
    left_unmatched?: number;
    right_unmatched?: number;
    left_features?: number;
    right_features?: number;
    conflicts?: Record<string, number>;
    score_kind?: string;
  };
  status: string;
  created_at: string;
}

export interface Candidate {
  id: number;
  left_feature_id: number;
  right_feature_id: number;
  left_id: string | null;
  right_id: string | null;
  features: Record<string, unknown>;
  method: string;
  score: number;
  score_kind: string;
  match_class: "match" | "possible" | "non_match";
  relationship: string | null;
  explanation: string[];
  status: string;
  label: number | null;
}

export interface ConflictProps {
  id: number;
  category: string;
  severity: "low" | "medium" | "high" | "critical";
  priority: number;
  confidence: number;
  status: "pending" | "approved" | "rejected" | "deferred";
  legal_sensitive: boolean;
  feature_ids: number[];
  dataset_version_ids: number[];
  explanation: string;
  proposed_action: string;
  run_id: number | null;
}

export interface ConflictList extends FeatureCollection<ConflictProps> {
  numberMatched: number;
  summary: { category: string; status: string; n: number }[];
}

export interface Correction {
  id: number;
  kind: string;
  safety: "auto_safe" | "review_required" | "legal_no_auto";
  status: string;
  reason: string;
  method: string;
  metrics: Record<string, unknown>;
  original_attributes: Record<string, unknown>;
  proposed_attributes: Record<string, unknown>;
  integrated_feature_id: number | null;
  base_version: number | null;
  source_feature_id: number | null;
  original_geojson: string | null;
  proposed_geojson: string | null;
}

export interface SourceFeatureProps {
  id: number;
  source_id_raw: string | null;
  normalized_id: string | null;
  attributes: Record<string, unknown>;
  normalized_attributes: Record<string, unknown>;
  area_m2: number | null;
  is_valid_source: boolean | null;
  validity_reason: string | null;
  repair_status: string;
  dataset: string;
  dataset_id: number;
  kind: string;
  provenance: string;
  reliability: number;
  source_date: string | null;
  license: string;
  version_no: number;
}

export interface ConflictDetail {
  id: number;
  category: string;
  severity: string;
  priority: number;
  confidence: number;
  confidence_kind: string;
  status: string;
  legal_sensitive: boolean;
  explanation: string;
  proposed_action: string;
  evidence: Record<string, unknown>;
  run_id: number | null;
  geometry: Geometry | null;
  features: FeatureCollection<SourceFeatureProps>;
  corrections: Correction[];
  decisions: { id: number; decision: string; comment: string; reviewer_id: number; created_at: string }[];
}

export interface ModelVersion {
  id: number;
  name: string;
  version: number;
  algorithm: string;
  match_type: string;
  status: string;
  is_active: boolean;
  metrics: {
    model?: Metrics;
    deterministic_baseline?: Metrics & { rule: string };
    feature_importance?: Record<string, number>;
    evaluated_on?: string;
    probability_calibration?: string;
    test_class_distribution?: Record<string, number>;
  };
  training_info: Record<string, unknown>;
  created_at: string;
}

export interface Metrics {
  precision: number;
  recall: number;
  f1: number;
  support: number;
  confusion_matrix: { labels: number[]; matrix: number[][] };
}

export interface AcquisitionSource {
  key: string;
  name: string;
  provider: string;
  kind: string;
  access: string;
  automated: boolean;
  license: string;
  attribution: string | null;
  homepage: string;
  notes: string;
  format: string | null;
}

export interface Manifest {
  id: number;
  source_key: string;
  status: string;
  reachable: boolean | null;
  message: string;
  files: Record<string, unknown>[];
  source_meta: Record<string, unknown>;
  dataset_version_id: number | null;
  created_at: string;
}

export interface AuditEvent {
  id: number;
  ts: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  details: Record<string, unknown>;
  request_id: string | null;
  hash: string;
  actor: string | null;
}

export interface MetricsSummary {
  datasets: { kind: string; provenance: string; datasets: number; versions: number; features: number }[];
  conflicts: { category: string; status: string; n: number }[];
  jobs: { status: string; n: number }[];
  integrated: { feature_class: string; status: string; n: number }[];
  match_runs: { match_type: string; runs: number; candidates: number; matches: number }[];
  review_decisions: { subject_type: string; decision: string; n: number }[];
}

export type { Feature, FeatureCollection, Geometry };
