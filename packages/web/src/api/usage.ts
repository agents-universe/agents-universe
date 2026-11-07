import { apiFetch } from './client'

const enc = encodeURIComponent

export interface UsageSummary {
  total_tokens: number
  run_count: number
  user_count: number
  conversation_count: number
}

export interface UsageTopUser {
  rank: number
  user_id: string
  /** Human label from the server-side name mirror; null → show user_id. */
  display_name: string | null
  tokens: number
  run_count: number
  conversation_count: number
}

export interface UsageDailyBucket {
  /** UTC calendar date, YYYY-MM-DD, ascending, zero-filled. */
  date: string
  tokens: number
  run_count: number
}

export interface UsageDashboard {
  days: number
  since: string
  generated_at: string
  summary: UsageSummary
  top_users: UsageTopUser[]
  daily: UsageDailyBucket[]
}

export const usageApi = {
  async getUsage(projectId: string, days: number): Promise<UsageDashboard> {
    return apiFetch<UsageDashboard>(
      `/api/projects/${enc(projectId)}/usage?days=${days}`,
    )
  },
}
