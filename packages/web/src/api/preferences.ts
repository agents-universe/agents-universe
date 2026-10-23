import { apiFetch } from './client'
import type { AppLocale } from '@/i18n'

export interface UserPreferences {
  onboarding_completed: boolean
  onboarding_completed_at: string | null
  locale: AppLocale | null
}

export function patchPreferences(patch: {
  onboarding_completed?: boolean
  locale?: AppLocale
}): Promise<UserPreferences> {
  return apiFetch<UserPreferences>('/api/preferences', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}
