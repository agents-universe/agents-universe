import { describe, it, expect, beforeEach } from 'vitest'
import { setLocale } from '@/i18n'
import { resolveLocalized, localizedText, searchBlob } from './localize'

beforeEach(() => {
  setLocale('zh-CN')
})

describe('resolveLocalized', () => {
  it('returns the canonical value when overrides are absent', () => {
    expect(resolveLocalized('技术负责人', null)).toBe('技术负责人')
    expect(resolveLocalized('技术负责人', undefined)).toBe('技术负责人')
  })

  it('returns the locale override when present', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    setLocale('en-US')
    expect(resolveLocalized('技术负责人', overrides)).toBe('Tech Lead')
  })

  it('falls back to canonical when the locale has no override', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    // locale is zh-CN — no zh-CN key in the map, canonical wins
    expect(resolveLocalized('技术负责人', overrides)).toBe('技术负责人')
  })

  it('switches the resolved value reactively on locale change', () => {
    const overrides = { 'zh-CN': '技术负责人', 'en-US': 'Tech Lead' }
    expect(resolveLocalized('技术负责人', overrides)).toBe('技术负责人')
    setLocale('en-US')
    expect(resolveLocalized('技术负责人', overrides)).toBe('Tech Lead')
  })

  it('resolves non-string values (e.g. starter prompt lists)', () => {
    const prompts = ['帮我评审代码']
    const overrides = { 'en-US': ['Review my code'] }
    setLocale('en-US')
    expect(resolveLocalized(prompts, overrides)).toEqual(['Review my code'])
    setLocale('zh-CN')
    expect(resolveLocalized(prompts, overrides)).toEqual(prompts)
  })
})

describe('localizedText', () => {
  it('returns the localized string', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    setLocale('en-US')
    expect(localizedText('技术负责人', overrides)).toBe('Tech Lead')
  })

  it('applies the fallback when both canonical and overrides are empty', () => {
    expect(localizedText(null, null, 'fallback')).toBe('fallback')
    expect(localizedText(undefined, undefined, 'fallback')).toBe('fallback')
  })

  it('prefers canonical over the fallback when no locale override exists', () => {
    expect(localizedText('canonical', null, 'fallback')).toBe('canonical')
  })
})

describe('searchBlob', () => {
  it('includes canonical and every locale value, lowercased', () => {
    const blob = searchBlob('技术负责人', { 'en-US': 'Tech Lead' })
    expect(blob).toContain('技术负责人')
    expect(blob).toContain('tech lead')
  })

  it('handles absent canonical and overrides', () => {
    expect(searchBlob(null, null)).toBe('')
    expect(searchBlob(undefined, undefined)).toBe('')
  })

  it('matches a query in either language', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    const blob = searchBlob('技术负责人', overrides)
    expect(blob.includes('tech lead')).toBe(true)
    expect(blob.includes('技术负责人')).toBe(true)
    expect(blob.includes('design')).toBe(false)
  })
})
