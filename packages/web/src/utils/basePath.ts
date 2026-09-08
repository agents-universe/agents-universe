function detect(): string {
  try {
    const url = new URL(import.meta.url)
    const idx = url.pathname.indexOf('/assets/')
    if (idx > 0) return url.pathname.slice(0, idx)
  } catch { /* dev mode or unsupported */ }
  return ''
}

export const basePath = detect()

export const apiBase = import.meta.env.VITE_API_BASE_PATH || basePath

export function withBase(path: string): string {
  return `${basePath}${path}`
}

export function withApi(path: string): string {
  // Tool-produced media URLs may already be absolute (APP_BASE_URL injected
  // server-side so the agent can quote a working download address). Prefixing
  // again would produce apiBase + 'https://...' and break the link.
  if (/^https?:\/\//.test(path)) return path
  return `${apiBase}${path}`
}
