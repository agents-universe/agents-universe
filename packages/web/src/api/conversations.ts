import { apiFetch } from './client'
import type { CompressResult, ConversationItem, DbMessage, DbTask } from '@/types'

const enc = encodeURIComponent

export const conversationsApi = {
  list: (projectId: string, agentSlug: string) =>
    apiFetch<ConversationItem[]>(`/api/projects/${enc(projectId)}/conversations?agent_slug=${enc(agentSlug)}`),

  getMessages: (conversationId: string) =>
    apiFetch<DbMessage[]>(`/api/conversations/${enc(conversationId)}/messages`),

  getTasks: (conversationId: string) =>
    apiFetch<DbTask[]>(`/api/conversations/${enc(conversationId)}/tasks`),

  delete: (conversationId: string) =>
    apiFetch<void>(`/api/conversations/${enc(conversationId)}`, { method: 'DELETE' }),

  compress: (conversationId: string) =>
    apiFetch<CompressResult>(`/api/conversations/${enc(conversationId)}/compress`, { method: 'POST' }),

  getLatest: (projectId: string, agentSlug: string) =>
    apiFetch<{ conversation_id: string; tokens_used: number; token_budget: number } | null>(
      `/api/projects/${enc(projectId)}/conversations/latest?agent_slug=${enc(agentSlug)}`,
    ),

  create: (projectId: string, agentSlug: string | null) =>
    apiFetch<{ conversation_id: string; project_id: string; token_budget: number }>(
      `/api/projects/${enc(projectId)}/conversations`,
      { method: 'POST', body: JSON.stringify({ agent_id: agentSlug }) },
    ),
}
