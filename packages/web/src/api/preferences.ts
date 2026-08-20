import { apiFetch } from './client'

export interface UserPreferences {
  onboarding_completed: boolean
  onboarding_completed_at: string | null
  last_seen_version: string | null
}

export function patchPreferences(patch: {
  onboarding_completed?: boolean
  last_seen_version?: string
}): Promise<UserPreferences> {
  return apiFetch<UserPreferences>('/api/preferences', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}
