// Thin fetch wrapper. Types come from `schema.d.ts`, which is GENERATED from the API's
// OpenAPI schema — CI fails if the committed types drift (04 rule 5, 14 §6b).
//
// This file contains no verdict logic and no thresholds. The frontend computes nothing:
// every verdict is decided once, in SQL, and recorded in the state store, which is what
// makes the dashboard, Slack, Jira and the PR comment all say the same thing (12 §7).

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // A non-JSON error body is still an error; keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

// ── Shapes mirrored from the API models. Replaced by generated types at build time. ──

export type CheckState = "ok" | "degraded" | "error" | "skipped";

export interface DiagnosticResult {
  name: string;
  state: CheckState;
  detail: string;
  consequence: string | null;
  duration_ms: number;
}

export interface Diagnostics {
  project: string;
  tool_version: string;
  healthy: boolean;
  results: DiagnosticResult[];
  pending_confirmations: string[];
}

export interface HopSummary {
  id: string;
  hop: string;
  lane: string;
  source: string;
  target: string;
  relation: string;
  contract_confirmed: boolean;
  unverified_measures: string[];
}

export interface NotCheckedItem {
  subject: string;
  reason: string;
}

export interface ProjectSummary {
  project: string;
  team: string;
  go_live: boolean;
  shadow_mode: boolean;
  tables: number;
  hop_count: number;
  checks_total: number;
  checks_active: number;
  config_sha: string;
}

export interface ProjectDetail extends ProjectSummary {
  hops: HopSummary[];
  not_checked: NotCheckedItem[];
}

export interface RunSummary {
  run_id: string;
  lane: string;
  trigger_kind: string;
  started_at: string | null;
  ended_at: string | null;
  status: string;
  shadow_mode: boolean;
  warehouse_seconds: number | null;
  config_sha: string | null;
  scope_predicate: string | null;
}

export interface Result {
  result_id: string;
  check_id: string;
  status: "PASS" | "FAIL" | "INCONCLUSIVE" | "UNVALIDATED";
  failing_row_count: number | null;
  evaluated_row_count: number | null;
  columns_compared: string[];
  columns_excluded: string[];
  scope_predicate: string | null;
  duration_ms: number | null;
  suppressed_by: string | null;
  error_message: string | null;
  sample_rows: Record<string, unknown>[];
}

export interface Coverage {
  table_name: string;
  table_type: string | null;
  layer: string | null;
  hop_id: string | null;
  hop_relation: string | null;
  contract_confirmed: boolean;
  unverified_measures: string[];
  checks_active: number;
  checks_expected: number;
  grain_confirmed: boolean;
  unvalidated: boolean;
}

export interface RunDetail {
  run: RunSummary;
  results: Result[];
  coverage: Coverage[];
  counts: Record<string, number>;
}

export interface Check {
  check_id: string;
  lane: string;
  template: string;
  table_name: string;
  relationship: string | null;
  hop_id: string | null;
  layer: string | null;
  severity: string;
  status: string;
  grain_confirmed_by: string | null;
  gates: Record<string, boolean>;
  activatable: boolean;
}

export interface ConnectionTestResult {
  ok: boolean;
  detail: string;
  remedy: string | null;
  schemas: string[];
}

export interface DiscoveredTable {
  fqn: string;
  columns: string[];
  schema_name: string;
  name: string;
  kind: string;
  row_count: number | null;
  column_count: number;
  suggested_type: string;
  suggested_grain: string[];
  reason: string;
  already_monitored: boolean;
}

export interface DiscoveryResult {
  project: string;
  tables: DiscoveredTable[];
  error: string | null;
}

export interface MonitorTable {
  fqn: string;
  columns?: string[];
  table_type: string;
  grain: string[];
  grain_confirmed_by: string | null;
  scd2_business_key?: string[];
  scd2_surrogate_key?: string | null;
  scd2_valid_from?: string | null;
  scd2_valid_to?: string | null;
  scd2_is_current?: string | null;
  scd2_tracked_columns?: string[];
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: body
      ? { Accept: "application/json", "Content-Type": "application/json" }
      : { Accept: "application/json" },
    credentials: "same-origin",
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  listProjects: () => request<ProjectSummary[]>("/projects"),
  invalidProjects: () => request<Record<string, string>>("/projects/invalid"),
  getProject: (name: string) => request<ProjectDetail>(`/projects/${name}`),
  // Fast by default. `deep` adds the full role-hierarchy grant audit, which takes tens
  // of seconds against a real warehouse, so it is a deliberate action rather than the
  // cost of opening the page.
  getDiagnostics: (name: string, deep = false) =>
    request<Diagnostics>(`/projects/${name}/diagnostics${deep ? "?deep=true" : ""}`),
  listRuns: (name: string) => request<RunSummary[]>(`/projects/${name}/runs`),
  latestRun: (name: string) => request<RunDetail>(`/projects/${name}/runs/latest`),
  listChecks: (name: string) => request<Check[]>(`/projects/${name}/checks`),
  triggerRun: (name: string) => post<{ run_id: string; counts: Record<string, number> }>(
    `/projects/${name}/runs`,
  ),

  // ── onboarding ──
  testSnowflake: (body: unknown) =>
    post<ConnectionTestResult>("/connections/test/snowflake", body),
  testPostgres: (body: unknown) => post<ConnectionTestResult>("/connections/test/postgres", body),
  createProject: (body: unknown) =>
    post<{ project: string; path: string; warnings: string[] }>("/projects", body),
  discover: (name: string) => request<DiscoveryResult>(`/projects/${name}/discover`),
  setMonitored: (name: string, body: unknown) =>
    post<{ tables_written: number; hops_written: number; notes: string[] }>(
      `/projects/${name}/monitor`,
      body,
    ),
};
