import { apiFetch } from './client'

const enc = encodeURIComponent

export interface ArtifactEntry {
  name: string
  rel_path: string
  kind: 'screenshot' | 'video' | 'trace' | 'report' | 'file'
  mime: string
  size_bytes: number
  test_title: string
}

export interface RunCounts {
  total: number
  passed: number
  failed: number
  flaky: number
  skipped: number
}

export interface RunFailedTest {
  title: string
  file: string
  line: number
  error: string
}

export interface RunTestRow {
  title: string
  file: string
  line: number
  status: string
  duration_ms: number
  retries: number
  error: string
}

/** Normalized verdict of a run - from Playwright's JSON report, or parsed
 *  from the run output when no report exists (then `partial` is set). */
export interface RunResult {
  source: 'json' | 'log'
  partial: boolean
  status: string
  counts: RunCounts
  duration_ms: number
  failed_tests: RunFailedTest[]
  tests: RunTestRow[]
  truncated: boolean
}

/** Compact projection carried on history rows: verdict and counts only. */
export interface RunSummary {
  status: string | null
  source: string | null
  partial: boolean
  counts: RunCounts | null
  duration_ms: number | null
  failed_count: number
}

export interface RunRow {
  run_id: string
  script_id: string
  spec_slug: string | null
  status: string
  exit_code: number | null
  triggered_by: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string | null
  summary: RunSummary | null
}

export interface RunDetail extends RunRow {
  result: RunResult | null
  artifacts: ArtifactEntry[]
  artifacts_truncated: boolean
  report_url: string | null
  log: string
}

/** API-relative URL of one artifact - wrap with withApi() before fetching. */
export function artifactPath(runId: string, relPath: string): string {
  const segments = relPath.split('/').filter(Boolean).map(enc)
  return `/api/scripts/runs/${enc(runId)}/artifacts/${segments.join('/')}`
}

export const scriptsApi = {
  runScript: (scriptId: string) =>
    apiFetch<{ run_id: string; status: string }>(
      `/api/scripts/${enc(scriptId)}/run`,
      { method: 'POST' },
    ),

  runSpec: (projectId: string, slug: string, env: Record<string, string>) =>
    apiFetch<{ run_id: string; status: string }>(
      `/api/projects/${enc(projectId)}/playwright/specs/${enc(slug)}/run`,
      { method: 'POST', body: JSON.stringify({ env }) },
    ),

  getRun: (runId: string) => apiFetch<RunDetail>(`/api/scripts/runs/${enc(runId)}`),

  /** Past runs of one spec (Playwright runs share the project's anchor script,
   *  so the spec slug is what scopes the history). */
  listSpecRuns: (projectId: string, slug: string) =>
    apiFetch<RunRow[]>(
      `/api/projects/${enc(projectId)}/playwright/specs/${enc(slug)}/runs`,
    ),

  listScriptRuns: (scriptId: string) =>
    apiFetch<RunRow[]>(`/api/scripts/${enc(scriptId)}/runs`),
}
