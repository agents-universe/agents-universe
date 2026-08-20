/**
 * Compile-check every message in both locales.
 *
 * vue-i18n parses `@`, `{`, `|` as message-format syntax at compile time; a
 * bare `@` (e.g. the @-mention tour step) throws "Invalid linked format"
 * while rendering - which silently kills whatever component shows the
 * message. Walking every key through t() fails the test before that class
 * of bug reaches a screen.
 */
import { describe, expect, it } from 'vitest'
import zhCN from './locales/zh-CN'
import enUS from './locales/en-US'
import { i18n } from './index'

function flatten(obj: Record<string, unknown>, prefix = ''): [string, string][] {
  const out: [string, string][] = []
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') out.push([key, v])
    else if (v && typeof v === 'object') out.push(...flatten(v as Record<string, unknown>, key))
  }
  return out
}

describe('locale messages', () => {
  it('every message in both locales compiles', () => {
    const bad: string[] = []
    for (const locale of ['zh-CN', 'en-US'] as const) {
      i18n.global.locale.value = locale
      const msgs = (locale === 'zh-CN' ? zhCN : enUS) as Record<string, unknown>
      for (const [key, msg] of flatten(msgs)) {
        try {
          i18n.global.t(key)
        } catch (e) {
          bad.push(`[${locale}] ${key}: "${msg}" -> ${e instanceof Error ? e.message.split('\n')[0] : e}`)
        }
      }
    }
    expect(bad).toEqual([])
  })

})
