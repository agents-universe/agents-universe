import { apiFetch } from './client'
import type { ProjectMember } from '@/types'

export async function listProjectMembers(projectId: string): Promise<ProjectMember[]> {
  return apiFetch<ProjectMember[]>(`/api/projects/${projectId}/members`)
}

export async function addProjectMember(projectId: string, userId: string): Promise<ProjectMember> {
  return apiFetch<ProjectMember>(`/api/projects/${projectId}/members`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })
}

export async function removeProjectMember(projectId: string, userId: string): Promise<void> {
  await apiFetch(`/api/projects/${projectId}/members/${userId}`, { method: 'DELETE' }, 204)
}
