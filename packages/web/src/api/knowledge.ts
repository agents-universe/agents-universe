import { apiFetch } from './client'
import type { KnowledgeItem, KnowledgeChildItem, KnowledgeAncestor, CategoryCompleteness } from '@/types'

const enc = encodeURIComponent

export interface KnowledgeFileDetail {
  slug: string
  title: string
  content: string
  tags: string[]
  cross_references: string[]
  parent_slug: string | null
  children_slugs: string[]
  depth: number
}

export const knowledgeApi = {
  getItems: (projectId: string, rootOnly = false, signal?: AbortSignal) =>
    apiFetch<KnowledgeItem[]>(`/api/projects/${enc(projectId)}/knowledge${rootOnly ? '?root_only=true' : ''}`, { signal }),

  getCompleteness: (projectId: string, signal?: AbortSignal) =>
    apiFetch<CategoryCompleteness>(`/api/projects/${enc(projectId)}/knowledge/completeness`, { signal }),

  getFile: (projectId: string, slug: string) =>
    apiFetch<KnowledgeFileDetail>(`/api/projects/${enc(projectId)}/knowledge/${enc(slug)}`),

  getChildren: (projectId: string, slug: string) =>
    apiFetch<KnowledgeChildItem[]>(`/api/projects/${enc(projectId)}/knowledge/${enc(slug)}/children`),

  getAncestors: (projectId: string, slug: string) =>
    apiFetch<KnowledgeAncestor[]>(`/api/projects/${enc(projectId)}/knowledge/${enc(slug)}/ancestors`),

  saveFile: (projectId: string, slug: string, content: string) =>
    apiFetch<void>(`/api/projects/${enc(projectId)}/knowledge/${enc(slug)}`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
}
