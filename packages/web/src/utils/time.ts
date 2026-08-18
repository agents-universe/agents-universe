export function relativeTime(value: string | number | null | undefined): string {
  if (!value) return ''
  // also accepts epoch-millis numbers (Message.timestamp) — the
  // caller no longer pre-normalizes with new Date(ts).toISOString(), which
  // threw RangeError on invalid input.
  const ts = typeof value === 'number' ? value : new Date(value).getTime()
  if (isNaN(ts)) return ''
  const diff = Date.now() - ts
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes}分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}天前`
  return new Date(value).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
