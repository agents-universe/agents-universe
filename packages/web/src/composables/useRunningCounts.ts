import { apiBase } from '@/utils/basePath'
import { redirectToLogin } from '@/api/client'
import { useRunningCountsStore } from '@/stores/runningCounts'
import type { RunningCounts } from '@/types'

// Reconnect ladder for the single user-level socket (mirrors the conversation
// socket's fast→slow shape; the server replays a full snapshot on every open,
// so reconnecting is lossless).
const BACKOFF_MS = [1000, 2000, 4000]
const SLOW_BACKOFF_MS = [8000, 15_000, 30_000]
const PING_INTERVAL_MS = 30_000

// Module singleton: one `/ws/user` socket for the whole sidebar, opened on
// first useRunningCounts() call (AppLayout setup) and never counted against
// the conversation sockets' MAX_CONNECTIONS budget.
let ws: WebSocket | null = null
let started = false
let retries = 0
let pingTimer: ReturnType<typeof setInterval> | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function clearTimers() {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
}

function scheduleReconnect(open: () => void) {
  const delay = retries < BACKOFF_MS.length
    ? BACKOFF_MS[retries]
    : SLOW_BACKOFF_MS[Math.min(retries - BACKOFF_MS.length, SLOW_BACKOFF_MS.length - 1)]
  retries += 1
  reconnectTimer = setTimeout(() => { reconnectTimer = null; open() }, delay)
}

function openSocket() {
  const store = useRunningCountsStore()
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const myWs = new WebSocket(`${proto}://${location.host}${apiBase}/ws/user`)
  ws = myWs

  myWs.onopen = () => {
    if (ws !== myWs) return
    retries = 0
    if (pingTimer) clearInterval(pingTimer)
    pingTimer = setInterval(() => {
      if (ws === myWs && myWs.readyState === WebSocket.OPEN) {
        myWs.send(JSON.stringify({ type: 'ping' }))
      }
    }, PING_INTERVAL_MS)
  }

  myWs.onmessage = (e) => {
    if (ws !== myWs) return
    let msg: { type?: string; by_project?: Record<string, number>; by_agent?: Record<string, number> }
    try {
      msg = JSON.parse(e.data as string) as typeof msg
    } catch {
      return
    }
    if (msg.type === 'running_counts') {
      const counts: RunningCounts = {
        by_project: msg.by_project ?? {},
        by_agent: msg.by_agent ?? {},
      }
      store.setCounts(counts)
    }
  }

  myWs.onclose = (ev) => {
    if (ws !== myWs) return
    ws = null
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
    // Session expired — same contract as the conversation sockets.
    if (ev.code === 4001) { redirectToLogin(); return }
    scheduleReconnect(openSocket)
  }
}

/** Open the sidebar's user-level counts channel (idempotent). */
export function useRunningCounts(): void {
  if (started) return
  started = true
  const store = useRunningCountsStore()
  // Initial load covers the no-WS case; the socket's open-frame snapshot
  // then keeps it authoritative.
  void store.fetch()
  openSocket()
}

/** Tear the channel down (tests / full app teardown). */
export function closeRunningCounts(): void {
  started = false
  retries = 0
  clearTimers()
  if (ws) { ws.close(); ws = null }
}
