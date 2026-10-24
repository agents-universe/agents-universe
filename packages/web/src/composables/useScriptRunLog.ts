import { ref, nextTick, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { apiBase } from '@/utils/basePath'
import { scriptsApi } from '@/api/scripts'

export interface LogLine { text: string; level: string }

export interface ScriptRunDone { runId: string; status: string; exitCode: number | null }

// Same-run reconnect ladder, then a slow poll of the run-detail endpoint so
// onDone still fires when the socket cannot be recovered. The server replays
// the full run log to every new subscriber, so reconnecting is lossless.
const RECONNECT_DELAYS_MS = [1000, 2000, 4000]
const POLL_INTERVAL_MS = 5000
const POLL_MAX_ATTEMPTS = 120

/**
 * Live log of a script/Playwright run over `/ws/script-runs/{run_id}`.
 *
 * Shared by the workspace executor (manual runs) and the scheduled-tasks page
 * (per-run logs from the history drawer): both connect to the same endpoint
 * and render the same lines.
 */
export function useScriptRunLog(opts: { onDone?: (done: ScriptRunDone) => void } = {}) {
  const { t } = useI18n()

  const activeRunId = ref<string | null>(null)
  const logs = ref<LogLine[]>([])
  const runError = ref<string | null>(null)
  const logPanel = ref<HTMLElement | null>(null)
  /** True once the server's authoritative "done" frame arrived. */
  const finished = ref(false)

  let ws: WebSocket | null = null
  // Component-lifetime guard: frames arriving after unmount must not touch
  // refs (and the socket itself is closed below).
  let alive = true
  // Set when the server's authoritative "done" frame arrives; suppresses the
  // "connection lost" warning on the close that follows it (the server closes
  // the socket right after sending done).
  let wsFinished = false
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectAttempt = 0
  let pollAttempts = 0

  function pushLog(line: LogLine) {
    const el = logPanel.value
    const nearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80
    logs.value.push(line)
    if (nearBottom) {
      void nextTick(() => {
        if (logPanel.value) logPanel.value.scrollTop = logPanel.value.scrollHeight
      })
    }
  }

  function clearTimers() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
  }

  /** Terminal state shared by the WS "done" frame and the poll fallback. */
  function markDone(runId: string, status: string, exitCode: number | null) {
    // wsFinished also suppresses the "connection lost" warning on the socket
    // close that follows the done frame.
    wsFinished = true
    finished.value = true
    const ok = status === 'completed'
    const statusText = ok
      ? status
      : `${status} (exit ${exitCode ?? '?'})`
    pushLog({ text: t('workspace.runFinished', { status: statusText }), level: ok ? 'info' : 'error' })
    // runId travels with the callback so the consumer can fetch the
    // authoritative result without re-deriving which run it was watching.
    opts.onDone?.({ runId, status, exitCode })
  }

  function startPolling(runId: string) {
    pollAttempts = 0
    pushLog({ text: t('workspace.runPollingFallback'), level: 'info' })
    const scheduleNext = () => {
      if (!alive || wsFinished || activeRunId.value !== runId) return
      pollTimer = setTimeout(tick, POLL_INTERVAL_MS)
    }
    const tick = () => {
      if (!alive || wsFinished || activeRunId.value !== runId) return
      pollAttempts += 1
      if (pollAttempts > POLL_MAX_ATTEMPTS) {
        pushLog({ text: t('workspace.runPollGaveUp'), level: 'error' })
        return
      }
      void scriptsApi.getRun(runId).then(
        (run) => {
          if (!alive || wsFinished || activeRunId.value !== runId) return
          if (run.status === 'completed' || run.status === 'failed') {
            markDone(runId, run.status, run.exit_code)
            return
          }
          scheduleNext()
        },
        // Transient fetch failure — keep polling until the attempt cap.
        scheduleNext,
      )
    }
    scheduleNext()
  }

  function scheduleReconnect(runId: string) {
    const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)]
    reconnectAttempt += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      if (!alive || activeRunId.value !== runId) return
      openSocket(runId)
    }, delay)
  }

  function openSocket(runId: string) {
    if (ws) { ws.close(); ws = null }
    wsFinished = false
    finished.value = false
    activeRunId.value = runId
    // A reconnect clears the buffer because the server replays the FULL log
    // to each new subscriber — appending the replay would duplicate every line.
    logs.value = []
    runError.value = null

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const myWs = new WebSocket(`${proto}://${location.host}${apiBase}/ws/script-runs/${runId}`)
    ws = myWs
    myWs.onmessage = (e) => {
      // ws !== myWs: a superseded socket must not write into the buffer its
      // replacement now owns (same-run reconnect keeps runId identical).
      if (!alive || ws !== myWs || activeRunId.value !== runId) return
      let msg: { type?: string; level?: string; text?: string; log?: string; status?: string; exit_code?: number | null }
      try {
        msg = JSON.parse(e.data as string) as typeof msg
      } catch {
        pushLog({ text: String(e.data), level: 'info' })
        return
      }
      if (msg.type === 'done') {
        // Authoritative final state — the server closes the socket right after
        // this frame.
        markDone(runId, msg.status ?? 'failed', msg.exit_code ?? null)
        return
      }
      pushLog({ text: msg.text ?? msg.log ?? String(e.data), level: msg.level ?? 'info' })
    }
    myWs.onclose = () => {
      // Identity guard: reconnecting to the SAME run resets the shared
      // wsFinished before the old socket's close event lands — without
      // ws !== myWs the stale close passed the runId check and pushed a
      // "connection lost" error into the new connection's fresh buffer.
      if (!alive || ws !== myWs || activeRunId.value !== runId) return
      // Close after the done frame is the normal end of a run. Only a close
      // WITHOUT done means the connection dropped before the server reported
      // the outcome — the run may still be executing server-side.
      if (wsFinished) return
      pushLog({ text: t('workspace.connectionLost'), level: 'error' })
      // A dropped socket used to be terminal for the live view: the done frame
      // and remaining output were lost and consumers' onDone never fired, so
      // the result card / history refresh was permanently skipped. Retry with
      // backoff, then fall back to polling the run-detail endpoint.
      if (reconnectAttempt < RECONNECT_DELAYS_MS.length) {
        scheduleReconnect(runId)
      } else {
        startPolling(runId)
      }
    }
  }

  function connectToRun(runId: string) {
    clearTimers()
    reconnectAttempt = 0
    pollAttempts = 0
    openSocket(runId)
  }

  /** Drop the current run's socket and log buffer (project switch, close). */
  function closeLog() {
    clearTimers()
    reconnectAttempt = 0
    pollAttempts = 0
    if (ws) { ws.close(); ws = null }
    wsFinished = false
    activeRunId.value = null
    logs.value = []
    runError.value = null
    finished.value = false
  }

  onBeforeUnmount(() => {
    alive = false
    clearTimers()
    ws?.close()
    ws = null
  })

  return { activeRunId, logs, runError, logPanel, finished, connectToRun, closeLog, pushLog }
}
