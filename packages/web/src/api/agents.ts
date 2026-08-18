import { apiFetch } from './client'
import type { AgentInfo, ModelConfig } from '@/types'

interface AgentApiResponse {
  agent_id: string
  slug: string
  display_name: string
  description?: string | null
  category?: string | null
  project_id?: string | null
  skills: Array<{ slug: string; description: string }>
  workflows: Array<{ slug: string; description: string }>
  tools?: string[]
}

export const agentsApi = {
  getAgents: async (projectId?: string): Promise<AgentInfo[]> => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
    const data = await apiFetch<AgentApiResponse[]>(`/api/agents${query}`)
    return data.map((a) => ({
      agent_id: a.agent_id,
      slug: a.slug,
      label: a.display_name,
      description: a.description ?? '',
      category: a.category ?? 'agile-development',
      project_id: a.project_id ?? null,
      skills: a.skills ?? [],
      workflows: a.workflows ?? [],
      tools: a.tools ?? [],
    }))
  },

  syncAgents: async (projectId: string): Promise<{ synced: string[]; removed: string[] }> => {
    return await apiFetch(`/api/agents/sync?project_id=${encodeURIComponent(projectId)}`, {
      method: 'POST',
    })
  },

  getModelConfigs: async (): Promise<ModelConfig[]> => {
    return await apiFetch<ModelConfig[]>('/api/model-configs')
  },
}
