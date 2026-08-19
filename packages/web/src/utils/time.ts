import { i18n } from '@/i18n'

export function relativeTime(value: string | number | null | undefined): string {
  if (!value) return ''
  // also accepts epoch-millis numbers (Message.timestamp) - the
  // caller no longer pre-normalizes with new Date(ts).toISOString(), which
  // threw RangeError on invalid input.
  const ts = typeof value === 'number' ? value : new Date(value).getTime()
  if (isNaN(ts)) return ''
  const { t } = i18n.global
  const diff = Date.now() - ts
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return t('common.time.justNow')
  if (minutes < 60) return t('common.time.minutesAgo', { n: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t('common.time.hoursAgo', { n: hours })
  const days = Math.floor(hours / 24)
  if (days < 30) return t('common.time.daysAgo', { n: days })
  return new Date(value).toLocaleDateString(i18n.global.locale.value, { month: 'short', day: 'numeric' })
}
