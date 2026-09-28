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
  /** Optional: true = global framework knowledge (read-only, no delete). */
  is_global?: boolean
}

/** A system template offered for copying into the project — disk-scanned
 * from knowledge/_template/, never a DB row. */
export interface KnowledgeTemplate {
  slug: string
  title: string
  category: string
  summary: string
  /** The project already has this slug: copy is disabled (never overwrites). */
  exists: boolean
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

  listTemplates: (projectId: string) =>
    apiFetch<KnowledgeTemplate[]>(`/api/projects/${enc(projectId)}/knowledge/_templates`),

  copyTemplate: (projectId: string, slug: string) =>
    apiFetch<{ slug: string; created: boolean }>(
      `/api/projects/${enc(projectId)}/knowledge/_templates/copy`,
      { method: 'POST', body: JSON.stringify({ slug }) },
    ),

  deleteFile: (projectId: string, slug: string) =>
    apiFetch<{ deleted: boolean; slug: string; file_deleted: boolean; db_row: string }>(
      `/api/projects/${enc(projectId)}/knowledge/${enc(slug)}`,
      { method: 'DELETE' },
    ),
}
