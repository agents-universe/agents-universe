import { apiFetch } from './client'
import type { ModelConfig } from '@/types'

interface ModelConfigResponse {
  config_id: string
  provider: string
  model_id: string
  key_hint: string | null
  base_url: string | null
  url_mode: 'base_url' | 'full_url'
  is_system: boolean
}

export interface ModelConfigCreatePayload {
  provider: string
  model_id: string
  api_key?: string
  base_url?: string
  url_mode?: string
}

export interface ModelConfigUpdatePayload {
  model_id?: string
  api_key?: string
  base_url?: string
  url_mode?: string
}

export const modelConfigsApi = {
  list: async (): Promise<ModelConfig[]> => {
    const data = await apiFetch<ModelConfigResponse[]>('/api/model-configs')
    return data
  },

  create: async (body: ModelConfigCreatePayload): Promise<ModelConfig> => {
    return await apiFetch<ModelConfig>('/api/model-configs', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  update: async (configId: string, body: ModelConfigUpdatePayload): Promise<ModelConfig> => {
    return await apiFetch<ModelConfig>(`/api/model-configs/${configId}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    })
  },

  remove: async (configId: string): Promise<void> => {
    await apiFetch(`/api/model-configs/${configId}`, { method: 'DELETE' })
  },

  test: async (configId: string): Promise<{ ok: boolean; error?: string }> => {
    return await apiFetch(`/api/model-configs/${configId}/test`, { method: 'POST' })
  },

  testConnection: async (payload: { provider: string; model_id: string; api_key: string; base_url?: string; url_mode?: string }): Promise<{ ok: boolean; error?: string }> => {
    return await apiFetch('/api/model-configs/test-connection', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
}
