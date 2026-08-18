import { apiFetch } from './client'
import type { UserTokenEntry } from '@/types'

export const userTokensApi = {
  list: () =>
    apiFetch<UserTokenEntry[]>('/api/tokens'),

  upsert: (serviceKey: string, data: { value?: string; display_name?: string; base_url?: string | null }) =>
    apiFetch<{ service_key: string; key_hint: string }>(`/api/tokens/${encodeURIComponent(serviceKey)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  remove: (serviceKey: string) =>
    apiFetch<{ deleted: string }>(`/api/tokens/${encodeURIComponent(serviceKey)}`, {
      method: 'DELETE',
    }),
}
