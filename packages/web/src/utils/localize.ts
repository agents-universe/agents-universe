import { i18n } from '@/i18n'
import type { LocaleOverrides } from '@/types'

/**
 * Resolve a canonical value against the current locale's overrides.
 *
 * Data payloads carry the zh canonical in the top-level fields plus a
 * per-locale map for the non-canonical languages — `overrides[locale] ??
 * canonical`. Reading `i18n.global.locale.value` here makes every caller
 * inside a computed re-evaluate on a locale switch, so the UI updates
 * instantly without refetching.
 */
export function resolveLocalized<T>(
  canonical: T | null | undefined,
  overrides: LocaleOverrides<T> | null | undefined,
): T | undefined {
  if (!overrides) return canonical ?? undefined
  const locale = i18n.global.locale.value as string
  return (overrides as Record<string, T | undefined>)[locale] ?? canonical ?? undefined
}

/** String variant of resolveLocalized with an explicit fallback. */
export function localizedText(
  canonical: string | null | undefined,
  overrides: LocaleOverrides<string> | null | undefined,
  fallback = '',
): string {
  return resolveLocalized(canonical, overrides) ?? fallback
}

/**
 * Search text across every language of a field plus the canonical value.
 *
 * The picker filter must match what a user types in EITHER language — an
 * en-US user searching "Tech Lead" has to find the agent whose canonical
 * display_name is 技术负责人.
 */
export function searchBlob(
  canonical: string | null | undefined,
  overrides: LocaleOverrides<string> | null | undefined,
): string {
  const parts: string[] = [canonical ?? '']
  if (overrides) parts.push(...Object.values(overrides))
  return parts.join('\n').toLowerCase()
}
