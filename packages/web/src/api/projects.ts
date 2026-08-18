import { apiFetch } from './client'
import type { Project, ProjectCategory } from '@/types'

export const projectsApi = {
  getProjects: () =>
    apiFetch<Project[]>('/api/projects'),

  getCategories: () =>
    apiFetch<ProjectCategory[]>('/api/projects/categories'),

  createProject: (displayName: string, category = 'software') =>
    apiFetch<Project>('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ display_name: displayName, category }),
    }),

  deleteProject: (projectId: string, confirmation: string) =>
    apiFetch<void>(`/api/projects/${projectId}`, {
      method: 'DELETE',
      body: JSON.stringify({ confirmation }),
    }, 204),

  updateProjectVisibility: (projectId: string, visibility: 'public' | 'private') =>
    apiFetch<Project>(`/api/projects/${projectId}`, {
      method: 'PATCH',
      body: JSON.stringify({ visibility }),
    }),
}
