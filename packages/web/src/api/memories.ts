import { apiFetch } from './client'
import type { PersonalMemory, EpisodicMemory } from '@/types'

export const memoriesApi = {
  getPersonal: (projectId: string, signal?: AbortSignal) =>
    apiFetch<PersonalMemory[]>(`/api/projects/${projectId}/memories/personal`, { signal }),

  getEpisodic: (projectId: string, signal?: AbortSignal) =>
    apiFetch<EpisodicMemory[]>(`/api/projects/${projectId}/memories/episodic`, { signal }),

  createPersonal: (projectId: string, content: string, tags: string[]) =>
    apiFetch<PersonalMemory>(`/api/projects/${projectId}/memories/personal`, {
      method: 'POST',
      body: JSON.stringify({ content, tags }),
    }),

  archivePersonal: (projectId: string, memoryId: string) =>
    apiFetch<void>(`/api/projects/${projectId}/memories/personal/${memoryId}`, {
      method: 'DELETE',
    }),
}
