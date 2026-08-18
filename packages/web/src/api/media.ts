import { apiFetch } from './client'
import type { AttachmentRecord } from '@/types'

export const mediaApi = {
  /**
   * Upload a user attachment (image or file). The server reads the bytes into
   * an in-memory store — nothing is written to disk — and returns the
   * attachment record to reference from a WS message frame. The store entry
   * is consumed by the agent turn and expires via TTL.
   */
  upload: (projectId: string, conversationId: string, file: File, signal?: AbortSignal): Promise<AttachmentRecord> => {
    const form = new FormData()
    form.append('file', file)
    return apiFetch<AttachmentRecord>(
      `/api/media/${encodeURIComponent(projectId)}/${encodeURIComponent(conversationId)}`,
      { method: 'POST', body: form, signal },
    )
  },
}
