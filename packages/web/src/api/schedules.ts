import { apiFetch } from './client'

const enc = encodeURIComponent

export type ScheduleTargetType = 'script' | 'playwright' | 'agent'
export type ScheduleRunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped'

export interface ScheduledTask {
  schedule_id: string
  project_id: string
  name: string
  description: string | null
  target_type: ScheduleTargetType
  script_id: string | null
  spec_slug: string | null
  agent_slug: string | null
  prompt: string | null
  env: Record<string, string>
  conversation_id: string | null
  cron_expr: string
  /** Human-readable form of cron_expr, produced by the backend cron module. */
  schedule: string
  timezone: string
  enabled: boolean
  notify: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  created_at: string | null
}

export interface ScheduledTaskRun {
  run_id: string
  schedule_id: string
  trigger: 'schedule' | 'manual'
  status: ScheduleRunStatus
  script_run_id: string | null
  conversation_id: string | null
  summary: string | null
  error: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string | null
}

export interface SchedulePayload {
  name: string
  description?: string | null
  target_type: ScheduleTargetType
  script_id?: string | null
  spec_slug?: string | null
  agent_slug?: string | null
  prompt?: string | null
  env?: Record<string, string>
  conversation_id?: string | null
  cron_expr: string
  timezone: string
  enabled?: boolean
}

export const schedulesApi = {
  list: (projectId: string) =>
    apiFetch<ScheduledTask[]>(`/api/projects/${enc(projectId)}/schedules`),

  create: (projectId: string, payload: SchedulePayload) =>
    apiFetch<ScheduledTask>(`/api/projects/${enc(projectId)}/schedules`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  preview: (projectId: string, cronExpr: string, timezone: string) =>
    apiFetch<{ next_runs: string[]; description: string }>(
      `/api/projects/${enc(projectId)}/schedules/preview`,
      { method: 'POST', body: JSON.stringify({ cron_expr: cronExpr, timezone }) },
    ),

  get: (scheduleId: string) => apiFetch<ScheduledTask>(`/api/schedules/${enc(scheduleId)}`),

  update: (scheduleId: string, patch: Partial<SchedulePayload>) =>
    apiFetch<ScheduledTask>(`/api/schedules/${enc(scheduleId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  remove: (scheduleId: string) =>
    apiFetch<{ deleted: boolean }>(`/api/schedules/${enc(scheduleId)}`, { method: 'DELETE' }),

  runNow: (scheduleId: string) =>
    apiFetch<{ run_id: string; status: string }>(`/api/schedules/${enc(scheduleId)}/run`, {
      method: 'POST',
    }),

  runs: (scheduleId: string, limit = 20) =>
    apiFetch<ScheduledTaskRun[]>(`/api/schedules/${enc(scheduleId)}/runs?limit=${limit}`),
}
