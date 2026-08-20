/**
 * Release manifest for the "What's new" dialog — oldest → newest.
 *
 * `CURRENT_VERSION` is the last entry's version. Adding a new release
 * means appending an entry here (and the corresponding i18n keys in
 * `tour`'s sibling namespace `whatsNew`); every user whose
 * `last_seen_version` predates it sees the dialog once.
 */
export interface ReleaseEntry {
  version: string // semver, monotonically increasing, e.g. '0.3.0'
  date: string // 'YYYY-MM-DD'
  titleKey: string // i18n key, e.g. 'whatsNew.0_3_0.title'
  featureKeys: string[] // i18n keys, one bullet per feature
}

export const RELEASES: ReleaseEntry[] = []

export const CURRENT_VERSION: string | null = RELEASES.length > 0 ? RELEASES[RELEASES.length - 1].version : null
