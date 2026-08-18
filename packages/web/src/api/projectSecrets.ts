import { apiFetch } from './client'
import type { ProjectSecret } from '@/types'

export async function listProjectSecrets(projectId: string): Promise<ProjectSecret[]> {
  return apiFetch<ProjectSecret[]>(`/api/projects/${projectId}/secrets`)
}

export async function createProjectSecret(
  projectId: string,
  data: { service_key: string; environment?: string | null; secret_name?: string; display_name?: string; value: string },
): Promise<{ secret_id: string; service_key: string; key_hint: string }> {
  return apiFetch(`/api/projects/${projectId}/secrets`, {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function updateProjectSecret(
  projectId: string,
  secretId: string,
  data: { display_name?: string; value?: string },
): Promise<{ secret_id: string; service_key: string; key_hint: string }> {
  return apiFetch(`/api/projects/${projectId}/secrets/${secretId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}

export async function deleteProjectSecret(projectId: string, secretId: string): Promise<void> {
  await apiFetch(`/api/projects/${projectId}/secrets/${secretId}`, { method: 'DELETE' })
}
