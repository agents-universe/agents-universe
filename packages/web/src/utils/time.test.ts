import { beforeEach, describe, expect, it } from 'vitest'

import { relativeTime } from './time'
import { i18n } from '@/i18n'

describe('relativeTime', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('returns empty string for null / undefined / empty', () => {
    expect(relativeTime(null)).toBe('')
    expect(relativeTime(undefined)).toBe('')
    expect(relativeTime('')).toBe('')
  })

  it('returns empty string for invalid dates', () => {
    expect(relativeTime('not-a-date')).toBe('')
  })

  it('returns 刚刚 for under a minute', () => {
    expect(relativeTime(new Date(Date.now() - 30_000).toISOString())).toBe('刚刚')
  })

  it('formats minutes ago', () => {
    expect(relativeTime(new Date(Date.now() - 5 * 60_000).toISOString())).toBe('5分钟前')
  })

  it('formats hours ago', () => {
    expect(relativeTime(new Date(Date.now() - 3 * 3_600_000).toISOString())).toBe('3小时前')
  })

  it('formats days ago', () => {
    expect(relativeTime(new Date(Date.now() - 2 * 86_400_000).toISOString())).toBe('2天前')
  })

  it('formats relative times in en-US when locale switches', () => {
    i18n.global.locale.value = 'en-US'
    expect(relativeTime(new Date(Date.now() - 30_000).toISOString())).toBe('just now')
    expect(relativeTime(new Date(Date.now() - 5 * 60_000).toISOString())).toBe('5m ago')
  })
})
