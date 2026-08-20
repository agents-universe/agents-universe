import { describe, expect, it } from 'vitest'
import { compareVersions, entriesToShow } from './whatsNew'
import type { ReleaseEntry } from './releases'

const manifest: ReleaseEntry[] = [
  { version: '0.2.0', date: '2026-08-01', titleKey: 'a', featureKeys: [] },
  { version: '0.3.0', date: '2026-08-10', titleKey: 'b', featureKeys: [] },
  { version: '0.4.0', date: '2026-08-20', titleKey: 'c', featureKeys: [] },
]

describe('entriesToShow', () => {
  it('null last-seen shows every entry, newest first', () => {
    expect(entriesToShow(null, manifest).map((e) => e.version)).toEqual(['0.4.0', '0.3.0', '0.2.0'])
  })

  it('current version shows nothing', () => {
    expect(entriesToShow('0.4.0', manifest)).toEqual([])
  })

  it('older version shows the newer subset', () => {
    expect(entriesToShow('0.1.0', manifest).map((e) => e.version)).toEqual(['0.4.0', '0.3.0', '0.2.0'])
    expect(entriesToShow('0.2.0', manifest).map((e) => e.version)).toEqual(['0.4.0', '0.3.0'])
    expect(entriesToShow('0.3.0', manifest).map((e) => e.version)).toEqual(['0.4.0'])
  })

  it('empty manifest returns empty regardless of last-seen', () => {
    expect(entriesToShow(null, [])).toEqual([])
    expect(entriesToShow('9.9.9', [])).toEqual([])
  })
})

describe('compareVersions', () => {
  it('compares numerically, not lexicographically', () => {
    expect(compareVersions('0.10.0', '0.9.0')).toBeGreaterThan(0)
    expect(compareVersions('0.2.0', '0.10.0')).toBeLessThan(0)
  })

  it('ignores a leading v prefix', () => {
    expect(compareVersions('v1.2.3', '1.2.3')).toBe(0)
    expect(compareVersions('v0.4.0', '0.3.0')).toBeGreaterThan(0)
  })

  it('equal versions return 0', () => {
    expect(compareVersions('1.2.3', '1.2.3')).toBe(0)
    expect(compareVersions('0.4.0', '0.4')).toBe(0)
  })

  it('malformed segments count as 0', () => {
    expect(compareVersions('abc', '0.0.0')).toBe(0)
    expect(compareVersions('1.x.0', '1.0.0')).toBe(0)
    expect(compareVersions('', '0')).toBe(0)
  })
})
