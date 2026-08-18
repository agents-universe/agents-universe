import { withApi } from '@/utils/basePath'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public deletionId?: string,
    public retryable?: boolean,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

// the router guard checks /api/me only once per load, so a session
// expiring mid-use (24h TTL, server-side logout) left every apiFetch failing
// with the user stranded on a dead session. Route 401s to the SSO login —
// but never for /api/me itself, which reports the auth state and must not
// bounce (login page calls it to decide whether to show the login form).
// Exact match only: startsWith('/api/me') also exempted /api/media/* from
// the login redirect.
let redirectingToLogin = false

function redirectToLogin(): void {
  if (redirectingToLogin) return
  redirectingToLogin = true
  window.location.href = withApi('/auth/login')
}

export async function apiFetch<T>(url: string, options?: RequestInit, expectedStatus?: number): Promise<T> {
  const headers: Record<string, string> = { ...options?.headers as Record<string, string> }
  if (options?.body && !(options.body instanceof FormData)
      && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json'
  }
  const res = await fetch(withApi(url), {
    ...options,
    credentials: 'include',
    headers,
  })

  if (!res.ok || (expectedStatus !== undefined && res.status !== expectedStatus)) {
    let payload: unknown
    try { payload = await res.json() } catch { /* empty or non-JSON error */ }
    const body = payload && typeof payload === 'object' ? payload as Record<string, unknown> : undefined
    const detail = body?.detail
    const detailObject = detail && typeof detail === 'object' ? detail as Record<string, unknown> : undefined
    const source = detailObject ?? body
    const message = typeof detail === 'string'
      ? detail
      : typeof source?.message === 'string'
        ? source.message
        : `${res.status} ${res.statusText}`
    if (res.status === 401 && url !== '/api/me') {
      redirectToLogin()
    }
    throw new ApiError(
      res.status,
      message,
      typeof source?.code === 'string' ? source.code : undefined,
      typeof source?.deletion_id === 'string' ? source.deletion_id : undefined,
      typeof source?.retryable === 'boolean' ? source.retryable : undefined,
    )
  }

  // 204 No Content
  if (res.status === 204) return undefined as T

  return res.json() as Promise<T>
}
