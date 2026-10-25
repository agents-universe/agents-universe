import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { translateApiError, translateApiErrorWith } from './apiError'
import { setLocale } from '@/i18n'

afterEach(() => setLocale('zh-CN'))

describe('translateApiError', () => {
  it('translates a known code by locale', () => {
    setLocale('zh-CN')
    expect(translateApiError(new ApiError(404, 'Secret not found', 'SECRET_NOT_FOUND')))
      .toBe('密钥不存在。')
    setLocale('en-US')
    expect(translateApiError(new ApiError(404, 'Secret not found', 'SECRET_NOT_FOUND')))
      .toBe('Secret not found.')
  })

  it('interpolates {detail} with the server message', () => {
    setLocale('en-US')
    const msg = translateApiError(new ApiError(422, 'Unknown timezone: Mars/Base', 'SCHEDULE_TIMEZONE_INVALID'))
    expect(msg).toContain('Unknown timezone: Mars/Base')
  })

  it('falls back to the server message for unknown codes', () => {
    const e = new ApiError(400, 'Something brand new', 'FANCY_NEW_CODE')
    expect(translateApiError(e)).toBe('Something brand new')
  })

  it('falls back to the message when the code is missing', () => {
    const e = new ApiError(500, 'plain message')
    expect(translateApiError(e)).toBe('plain message')
  })

  it('stringifies non-Error values', () => {
    expect(translateApiError('boom')).toBe('boom')
    expect(translateApiError(undefined)).toBe('undefined')
  })
})

describe('translateApiErrorWith', () => {
  it('uses the fallback for non-Error values', () => {
    expect(translateApiErrorWith(null, 'fallback')).toBe('fallback')
  })

  it('translates Error values through translateApiError', () => {
    setLocale('zh-CN')
    expect(translateApiErrorWith(new ApiError(404, 'x', 'TOKEN_NOT_FOUND'), 'fallback'))
      .toBe('令牌不存在。')
  })
})
