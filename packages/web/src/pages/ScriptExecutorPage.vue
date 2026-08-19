<template>
  <div class="script-executor-page">
    <div class="script-executor-header">
      <h2 class="page-title">{{ t('scriptExecutor.title') }}</h2>
    </div>

    <div class="script-executor-body">
      <div class="script-list">
        <div
          v-for="script in scripts"
          :key="script.script_id"
          class="script-item"
          :class="{ active: activeRunId === script.run_id }"
          @click="script.run_id ? connectToRun(script.run_id) : runScript(script.script_id)"
        >
          <span class="script-name">{{ script.name }}</span>
          <span class="script-type">{{ script.script_type }}</span>
          <span class="script-status" :class="script.status">{{ script.status }}</span>
        </div>
        <div v-if="!scripts.length" class="script-list-empty">{{ t('scriptExecutor.noScripts') }}</div>
      </div>

      <div class="script-log-panel">
        <div v-if="!activeRunId" class="script-log-empty">{{ t('scriptExecutor.selectScript') }}</div>
        <div v-else class="script-log-output">
          <div
            v-for="(line, i) in logs"
            :key="i"
            class="script-log-line"
            :class="line.level"
          >{{ line.text }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useProjectStore } from '@/stores/project'
import { apiFetch } from '@/api/client'
import { apiBase } from '@/utils/basePath'

interface ScriptItem { script_id: string; run_id?: string; name: string; status: string; script_type: string }
interface LogLine { text: string; level: string }
const scripts = ref<ScriptItem[]>([])
const activeRunId = ref<string | null>(null)
const logs = ref<LogLine[]>([])
let ws: WebSocket | null = null
const mounted = ref(true)
const projectStore = useProjectStore()
const { t } = useI18n()

// Monotonic guard: a slow in-flight load must not clobber the result of a
// newer one (rapid project switching) — same pattern as KnowledgeBrowserPage.
let scriptLoadSeq = 0

async function loadScripts() {
  const seq = ++scriptLoadSeq
  const pid = projectStore.currentProject?.project_id
  if (!pid) return
  try {
    const data = await apiFetch<{ script_id: string; name: string; script_type: string }[]>(
      `/api/projects/${pid}/scripts`
    )
    if (seq !== scriptLoadSeq) return  // stale response — a newer load owns the list
    scripts.value = data.map((s) => ({ ...s, status: 'idle' }))
  } catch {
    // non-critical
  }
}

async function runScript(scriptId: string) {
  // Capture the project at request time: if it switches while the POST is in
  // flight, the projectId watcher has already reset activeRunId — connecting
  // anyway would stream another project's run into this page.
  const pidAtStart = projectStore.currentProject?.project_id
  try {
    const result = await apiFetch<{ run_id: string; status: string }>(
      `/api/scripts/${scriptId}/run`,
      { method: 'POST' }
    )
    if (projectStore.currentProject?.project_id !== pidAtStart) return
    const script = scripts.value.find((s) => s.script_id === scriptId)
    if (script) {
      script.run_id = result.run_id
      script.status = result.status
    }
    connectToRun(result.run_id)
  } catch {
    // non-critical
  }
}

onMounted(loadScripts)

// Project switched while this page stays mounted (route param changes):
// drop any socket tied to the previous project's run and reload the list —
// otherwise logs from project A's run keep streaming into project B's view.
watch(() => projectStore.currentProject?.project_id, () => {
  ws?.close()
  ws = null
  activeRunId.value = null
  logs.value = []
  loadScripts()
})

function connectToRun(runId: string) {
  if (ws) { ws.close(); ws = null }
  activeRunId.value = runId
  logs.value = []

  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}${apiBase}/ws/script-runs/${runId}`)
  ws.onmessage = (e) => {
    // A stale socket from a previous run may still deliver in-flight frames
    // after ws.close() — never let them pollute the active run's log.
    if (!mounted.value || activeRunId.value !== runId) return
    let msg: { type?: string; level?: string; text?: string; log?: string; status?: string }
    try {
      msg = JSON.parse(e.data as string) as typeof msg
    } catch {
      // Server may emit plain-text frames (ping/raw error) — never let a
      // parse failure kill the message handler for all subsequent frames.
      logs.value.push({ text: String(e.data), level: 'info' })
      return
    }
    if (msg.type === 'done') {
      // Authoritative final state from the server (completed OR failed with
      // the subprocess exit code). The old handler dropped this frame and
      // stamped 'completed' on close — failed scripts showed green.
      const script = scripts.value.find((s) => s.run_id === runId)
      if (script) script.status = msg.status ?? 'completed'
      return
    }
    logs.value.push({ text: msg.text ?? msg.log ?? String(e.data), level: msg.level ?? 'info' })
  }
  ws.onclose = () => {
    const script = scripts.value.find((s) => s.run_id === runId)
    // Only touch the row when this socket still belongs to the active run —
    // a manual close (run switched) must not mark a stale state.
    if (!script || !mounted.value || activeRunId.value !== runId) return
    // Closed WITHOUT a done frame → abnormal (network drop / server restart).
    // The run may still be executing server-side; never stamp 'completed'.
    if (script.status === 'running' || script.status === 'pending') {
      script.status = 'failed'
      logs.value.push({ text: t('scriptExecutor.connectionLost'), level: 'error' })
    }
  }
}

onBeforeUnmount(() => {
  mounted.value = false
  ws?.close()
})
</script>
