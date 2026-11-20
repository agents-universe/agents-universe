import { apiFetch } from './client'
import type { CompressResult, ConversationItem, ConversationRun, DbMessage, DbTask, RunningCounts } from '@/types'

const enc = encodeURIComponent

export const conversationsApi = {
  /** Conversations of a project. Omit agentSlug to list every agent's
   *  (used by search); q filters by title OR user-message body, with
   *  space-separated keywords AND-ed together server-side (passed verbatim). */
  list: (projectId: string, agentSlug?: string | null, q?: string) => {
    const params = new URLSearchParams()
    if (agentSlug) params.set('agent_slug', agentSlug)
    if (q) params.set('q', q)
    const qs = params.toString()
    return apiFetch<ConversationItem[]>(
      `/api/projects/${enc(projectId)}/conversations${qs ? `?${qs}` : ''}`,
    )
  },

  rename: (conversationId: string, title: string) =>
    apiFetch<{ conversation_id: string; title: string }>(
      `/api/conversations/${enc(conversationId)}`,
      { method: 'PATCH', body: JSON.stringify({ title }) },
    ),

  getMessages: (conversationId: string) =>
    apiFetch<DbMessage[]>(`/api/conversations/${enc(conversationId)}/messages`),

  getTasks: (conversationId: string) =>
    apiFetch<DbTask[]>(`/api/conversations/${enc(conversationId)}/tasks`),

  getLatestRun: (conversationId: string) =>
    apiFetch<ConversationRun | null>(`/api/conversations/${enc(conversationId)}/runs/latest`),

  delete: (conversationId: string) =>
    apiFetch<void>(`/api/conversations/${enc(conversationId)}`, { method: 'DELETE' }),

  compress: (conversationId: string) =>
    apiFetch<CompressResult>(`/api/conversations/${enc(conversationId)}/compress`, { method: 'POST' }),

  getLatest: (projectId: string, agentSlug: string) =>
    apiFetch<
      | {
          conversation_id: string
          tokens_used: number
          token_budget: number
          context_tokens?: number | null
          context_window?: number | null
        }
      | null
    >(`/api/projects/${enc(projectId)}/conversations/latest?agent_slug=${enc(agentSlug)}`),

  /** Current user's running-turn counts grouped by project / agent slug. */
  getRunningCounts: () => apiFetch<RunningCounts>('/api/running-counts'),

  create: (projectId: string, agentSlug: string | null) =>
    apiFetch<{ conversation_id: string; project_id: string; token_budget: number }>(
      `/api/projects/${enc(projectId)}/conversations`,
      { method: 'POST', body: JSON.stringify({ agent_id: agentSlug }) },
    ),
}
