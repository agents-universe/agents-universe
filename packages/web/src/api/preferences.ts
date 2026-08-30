import { apiFetch } from './client'

export interface UserPreferences {
  onboarding_completed: boolean
  onboarding_completed_at: string | null
}

export function patchPreferences(patch: {
  onboarding_completed?: boolean
}): Promise<UserPreferences> {
  return apiFetch<UserPreferences>('/api/preferences', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}
