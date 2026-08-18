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
  return `${apiBase}${path}`
}
