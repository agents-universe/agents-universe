import { apiFetch } from './client'

const enc = encodeURIComponent

export interface WorkspaceEntry {
  name: string
  path: string
  type: 'dir' | 'file'
  size_bytes: number
  mtime: number
}

export interface WorkspaceFileDetail {
  path: string
  content: string
  size_bytes: number
}

export const workspaceApi = {
  listDir: (projectId: string, path = '') =>
    apiFetch<{ path: string; entries: WorkspaceEntry[] }>(
      `/api/projects/${enc(projectId)}/workspace/files?path=${enc(path)}`,
    ),

  readFile: (projectId: string, path: string) =>
    apiFetch<WorkspaceFileDetail>(
      `/api/projects/${enc(projectId)}/workspace/file?path=${enc(path)}`,
    ),

  saveFile: (projectId: string, path: string, content: string) =>
    apiFetch<{ saved: boolean; bytes_written: number }>(
      `/api/projects/${enc(projectId)}/workspace/file?path=${enc(path)}`,
      { method: 'PUT', body: JSON.stringify({ content }) },
    ),
}
