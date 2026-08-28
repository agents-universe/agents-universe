import { apiFetch, ApiError } from './client'
import { withApi } from '@/utils/basePath'

// ── Types ────────────────────────────────────────────────────────────────────

export interface PublishItem {
  publish_id: string
  agent_slug: string
  project_id: string
  model_config_id: string
  title: string | null
  description: string | null
  page_enabled: boolean
  api_enabled: boolean
  created_at: string
  updated_at: string | null
}

export interface PublishCreatePayload {
  agent_slug: string
  project_id: string
  model_config_id: string
  title?: string | null
  description?: string | null
}

export interface PublishUpdatePayload {
  title?: string | null
  description?: string | null
  page_enabled?: boolean
  api_enabled?: boolean
  model_config_id?: string
}

export interface PublishKeyItem {
  key_id: string
  name: string | null
  key_hint: string | null
  is_active: boolean
  created_at: string
  revoked_at: string | null
}

export interface PublishKeyCreateResult extends PublishKeyItem {
  /** Shown exactly once — the raw key plaintext. */
  key: string
}

export interface PublishAgentInfo {
  slug: string
  display_name: string
  description: string | null
  project_id: string | null
}

/** Payload of the embedded page (SSO) session endpoint. */
export interface PublishSession {
  publish_id: string
  conversation_id: string
  agent: PublishAgentInfo | null
  project_id: string
  title: string | null
  description: string | null
  has_conversation: boolean
  /** Viewer token bound to (publish, logged-in user) — used by the run call. */
  token: string
}

/** Message row in the frontend's serialized shape. */
export interface PublishMessage {
  message_id: string
  role: string
  content: string
  agent_slug: string | null
  model_name: string | null
  tool_calls: unknown[]
  images: unknown[] | null
  attachments: unknown[] | null
  interrupted: boolean
  error: boolean
  sequence_num: number
  created_at: string
}

export const publishApi = {
  list: async (): Promise<PublishItem[]> => {
    return await apiFetch<PublishItem[]>('/api/publishes')
  },

  get: async (publishId: string): Promise<PublishItem> => {
    return await apiFetch<PublishItem>(`/api/publishes/${publishId}`)
  },

  create: async (body: PublishCreatePayload): Promise<PublishItem> => {
    return await apiFetch<PublishItem>('/api/publishes', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  update: async (publishId: string, body: PublishUpdatePayload): Promise<{ updated: boolean }> => {
    return await apiFetch<{ updated: boolean }>(`/api/publishes/${publishId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
  },

  remove: async (publishId: string): Promise<void> => {
    await apiFetch(`/api/publishes/${publishId}`, { method: 'DELETE' })
  },

  listKeys: async (publishId: string): Promise<PublishKeyItem[]> => {
    return await apiFetch<PublishKeyItem[]>(`/api/publishes/${publishId}/keys`)
  },

  createKey: async (publishId: string, name?: string): Promise<PublishKeyCreateResult> => {
    return await apiFetch<PublishKeyCreateResult>(`/api/publishes/${publishId}/keys`, {
      method: 'POST',
      body: JSON.stringify({ name: name ?? null }),
    })
  },

  revokeKey: async (publishId: string, keyId: string): Promise<void> => {
    await apiFetch(`/api/publishes/${publishId}/keys/${keyId}`, { method: 'DELETE' })
  },

  // ── Embedded page (SSO session) ─────────────────────────────────────

  /** First call of the page: cookie-authenticated, returns payload + token. */
  getPage: (publishId: string): Promise<PublishSession> =>
    apiFetch<PublishSession>(`/api/p/${publishId}/page`),

  getSessionMessages: (publishId: string, token: string): Promise<PublishMessage[]> =>
    apiFetch<PublishMessage[]>(`/api/p/${publishId}/session/messages?token=${encodeURIComponent(token)}`),

  /** Abort the running turn of an embedded publish. */
  abortSession: async (publishId: string, token: string): Promise<{ aborted: boolean }> => {
    const res = await fetch(
      withApi(`/api/p/${publishId}/session/abort`),
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      },
    )
    if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`)
    return await res.json()
  },

  /** Stream one turn of the embedded publish. Resolves the full SSE body text.
   *
   * Frames are parsed incrementally so `onDelta` fires per `stream_delta`
   * frame in real time (the page renders them live, like the main chat).
   */
  runSession: async (
    publishId: string,
    token: string,
    message: string,
    onDelta?: (delta: string) => void,
  ): Promise<string> => {
    const res = await fetch(
      withApi(`/api/p/${publishId}/session/run`),
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, message }),
      },
    )
    if (!res.ok || !res.body) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const body = await res.json() as { detail?: string }
        if (typeof body.detail === 'string') detail = body.detail
      } catch { /* non-JSON error body */ }
      throw new ApiError(res.status, detail)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let jsonText = ''

    // One "data: {json}" SSE frame. Malformed frames are skipped, not fatal.
    const consumeFrame = (frame: string): void => {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue
        try {
          const evt = JSON.parse(line.slice(6)) as { type?: string; delta?: string; message?: string }
          if (evt.type === 'stream_delta' && typeof evt.delta === 'string') {
            jsonText += evt.delta
            onDelta?.(evt.delta)
          } else if (evt.type === 'error' && evt.message) {
            throw new ApiError(500, evt.message)
          }
        } catch (e) {
          if (e instanceof ApiError) throw e
          // ignore malformed frames
        }
      }
    }

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // Frames are blank-line separated; parse complete frames as they land.
      let idx = buffer.indexOf('\n\n')
      while (idx !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        consumeFrame(frame)
        idx = buffer.indexOf('\n\n')
      }
    }
    // Flush a trailing frame with no blank-line terminator.
    if (buffer.trim()) consumeFrame(buffer)
    return jsonText
  },
}
