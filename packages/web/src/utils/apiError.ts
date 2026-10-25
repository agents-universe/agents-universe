import { i18n } from '@/i18n'
import { ApiError } from '@/api/client'

/**
 * Localized message for an API/WS error.
 *
 * Backend errors carry a stable machine-readable `code` (SCREAMING_SNAKE,
 * domain-prefixed) plus an English canonical `message`. The `errors`
 * i18n namespace mirrors every code; unknown codes (older server, new
 * code not yet translated) fall back to the raw English message.
 *
 * Variable-detail codes embed `{detail}` — filled with the server message
 * so specifics (the offending slug, the size cap…) survive translation.
 */
export function translateApiError(e: unknown): string {
  if (e instanceof ApiError && e.code && i18n.global.te(`errors.${e.code}`)) {
    return i18n.global.t(`errors.${e.code}`, { detail: e.message })
  }
  return e instanceof Error ? e.message : String(e)
}

/** translateApiError with a caller-supplied fallback for non-Error values. */
export function translateApiErrorWith(e: unknown, fallback: string): string {
  if (e instanceof Error) return translateApiError(e)
  return fallback
}

/**
 * Localized text for a WS/SSE `{code?, message}` frame — same errors
 * namespace as HTTP errors; frames without a code (legacy) fall back to
 * the raw message.
 */
export function translateErrorCode(code: unknown, message: unknown): string {
  const raw = typeof message === 'string' && message ? message : ''
  if (typeof code === 'string' && code && i18n.global.te(`errors.${code}`)) {
    return i18n.global.t(`errors.${code}`, { detail: raw })
  }
  return raw
}
