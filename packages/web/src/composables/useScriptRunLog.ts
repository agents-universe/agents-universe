import { ref, nextTick, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { apiBase } from '@/utils/basePath'

export interface LogLine { text: string; level: string }

export interface ScriptRunDone { status: string; exitCode: number | null }

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

  function connectToRun(runId: string) {
    if (ws) { ws.close(); ws = null }
    wsFinished = false
    finished.value = false
    activeRunId.value = runId
    logs.value = []
    runError.value = null

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}${apiBase}/ws/script-runs/${runId}`)
    ws.onmessage = (e) => {
      if (!alive || activeRunId.value !== runId) return
      let msg: { type?: string; level?: string; text?: string; log?: string; status?: string; exit_code?: number | null }
      try {
        msg = JSON.parse(e.data as string) as typeof msg
      } catch {
        pushLog({ text: String(e.data), level: 'info' })
        return
      }
      if (msg.type === 'done') {
        // Authoritative final state — the server closes the socket right after
        // this frame, so mark the run as finished to suppress the spurious
        // "connection lost" warning on close.
        wsFinished = true
        finished.value = true
        const ok = msg.status === 'completed'
        const statusText = ok
          ? (msg.status ?? 'completed')
          : `${msg.status ?? 'failed'} (exit ${msg.exit_code ?? '?'})`
        pushLog({ text: t('workspace.runFinished', { status: statusText }), level: ok ? 'info' : 'error' })
        opts.onDone?.({ status: msg.status ?? 'failed', exitCode: msg.exit_code ?? null })
        return
      }
      pushLog({ text: msg.text ?? msg.log ?? String(e.data), level: msg.level ?? 'info' })
    }
    ws.onclose = () => {
      if (!alive || activeRunId.value !== runId) return
      // Close after the done frame is the normal end of a run. Only a close
      // WITHOUT done means the connection dropped before the server reported
      // the outcome — the run may still be executing server-side.
      if (!wsFinished) {
        pushLog({ text: t('workspace.connectionLost'), level: 'error' })
      }
    }
  }

  /** Drop the current run's socket and log buffer (project switch, close). */
  function closeLog() {
    if (ws) { ws.close(); ws = null }
    wsFinished = false
    activeRunId.value = null
    logs.value = []
    runError.value = null
    finished.value = false
  }

  onBeforeUnmount(() => {
    alive = false
    ws?.close()
    ws = null
  })

  return { activeRunId, logs, runError, logPanel, finished, connectToRun, closeLog, pushLog }
}
