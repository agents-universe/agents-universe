/**
 * Version comparison + which release entries a user has not seen yet.
 * Pure functions — no DOM, no i18n.
 */
import type { ReleaseEntry } from './releases'

/**
 * Numeric-segment semver compare: '0.10.0' > '0.9.0'.
 * A leading 'v' is ignored; malformed segments count as 0.
 * Returns a negative/zero/positive number like a comparator.
 */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string) =>
    v
      .trim()
      .replace(/^v/i, '')
      .split('.')
      .map((seg) => {
        const n = Number.parseInt(seg, 10)
        return Number.isNaN(n) ? 0 : n
      })
  const as = parse(a)
  const bs = parse(b)
  const len = Math.max(as.length, bs.length)
  for (let i = 0; i < len; i++) {
    const diff = (as[i] ?? 0) - (bs[i] ?? 0)
    if (diff !== 0) return diff
  }
  return 0
}

/**
 * Release entries newer than `lastSeenVersion`, newest first.
 * A null last-seen version (never saw any release note) returns all entries.
 */
export function entriesToShow(lastSeenVersion: string | null, releases: ReleaseEntry[]): ReleaseEntry[] {
  if (releases.length === 0) return []
  if (lastSeenVersion == null) return [...releases].reverse()
  return releases.filter((r) => compareVersions(r.version, lastSeenVersion) > 0).reverse()
}
