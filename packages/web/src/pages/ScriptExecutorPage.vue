<template>
  <div class="script-executor-page">
    <div class="script-executor-header">
      <h2 class="page-title">{{ t('scriptExecutor.title') }}</h2>
      <div class="executor-sections">
        <button
          class="executor-section"
          :class="{ active: section === 'scripts' }"
          data-tour-target="executor-section-scripts"
          @click="section = 'scripts'"
        >{{ t('scriptExecutor.sectionScripts') }}</button>
        <button
          class="executor-section"
          :class="{ active: section === 'playwright' }"
          data-tour-target="executor-section-playwright"
          @click="switchSection('playwright')"
        >{{ t('scriptExecutor.sectionPlaywright') }}</button>
        <button
          v-if="section === 'scripts'"
          class="executor-section executor-section-new"
          @click="showCreate = true"
        ><Plus :size="13" /> {{ t('scriptExecutor.newScript') }}</button>
      </div>
    </div>

    <div class="script-executor-body">
      <div class="script-list">
        <template v-if="section === 'scripts'">
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
        </template>
        <template v-else>
          <input
            v-model="baseUrl"
            class="input executor-base-url"
            type="url"
            spellcheck="false"
            :placeholder="t('scriptExecutor.baseUrlPlaceholder')"
            :title="t('scriptExecutor.baseUrlHint')"
          />
          <div
            v-for="spec in specs"
            :key="spec.slug"
            class="script-item"
            :class="{ active: activeRunId === spec.run_id }"
            @click="spec.run_id ? connectToRun(spec.run_id) : runSpec(spec.slug)"
          >
            <span class="script-name">{{ spec.title }}</span>
            <span class="script-type">playwright</span>
            <span class="script-status" :class="spec.status">{{ spec.status }}</span>
          </div>
          <div v-if="!specs.length" class="script-list-empty">{{ t('scriptExecutor.noPlaywrightSpecs') }}</div>
        </template>
      </div>

      <div class="script-log-panel">
        <div v-if="runError" class="script-run-error">{{ runError }}</div>
        <div v-if="!activeRunId" class="script-log-empty">{{ t('scriptExecutor.selectScript') }}</div>
        <div v-else class="script-log-output" ref="logPanel">
          <div
            v-for="(line, i) in logs"
            :key="i"
            class="script-log-line"
            :class="line.level"
          >{{ line.text }}</div>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-overlay" ref="overlayEl">
      <div class="modal-dialog executor-create-dialog">
        <div class="modal-header">
          <h3 class="modal-title">{{ t('scriptExecutor.createTitle') }}</h3>
          <button class="modal-close" @click="showCreate = false">✕</button>
        </div>
        <p class="modal-hint">{{ t('scriptExecutor.createHint') }}</p>
        <form class="executor-create-form" @submit.prevent="createScript">
          <label class="input-label">{{ t('scriptExecutor.createName') }}</label>
          <input
            v-model="createName"
            class="input"
            type="text"
            maxlength="255"
            :placeholder="t('scriptExecutor.createNamePlaceholder')"
          />
          <label class="input-label">{{ t('scriptExecutor.createType') }}</label>
          <select v-model="createType" class="input">
            <option value="python">Python</option>
            <option value="bash">Bash</option>
          </select>
          <label class="input-label">{{ t('scriptExecutor.createContent') }}</label>
          <textarea
            v-model="createContent"
            class="executor-create-content"
            spellcheck="false"
            :placeholder="t('scriptExecutor.createContentPlaceholder')"
          />
          <div v-if="createError" class="executor-create-error">{{ createError }}</div>
          <div class="executor-create-actions">
            <button type="button" class="btn-sm secondary" @click="showCreate = false">{{ t('scriptExecutor.createCancel') }}</button>
            <button type="submit" class="btn-sm" :disabled="creating">
              {{ creating ? t('scriptExecutor.creating') : t('scriptExecutor.createSubmit') }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { apiFetch } from '@/api/client'
import { apiBase } from '@/utils/basePath'
import { useClickOutside } from '@/composables/useClickOutside'

interface ScriptItem { script_id: string; run_id?: string; name: string; status: string; script_type: string }
interface SpecItem { slug: string; run_id?: string; title: string; file: string; status: string }
interface LogLine { text: string; level: string }
type ExecutorSection = 'scripts' | 'playwright'

const section = ref<ExecutorSection>('scripts')
const scripts = ref<ScriptItem[]>([])
const specs = ref<SpecItem[]>([])
const activeRunId = ref<string | null>(null)
const logs = ref<LogLine[]>([])
// A failed run POST previously vanished silently (catch {} left the row at
// idle) - surface it in the log panel instead.
const runError = ref<string | null>(null)
// Live output lands below the fold once the panel scrolls (max-height caps
// it) - keep the newest line visible, but only when the user is already at
// the bottom so reading history is never fought.
const logPanel = ref<HTMLElement | null>(null)
let ws: WebSocket | null = null
const mounted = ref(true)
const projectStore = useProjectStore()
const { t } = useI18n()

// Target-system URL for Playwright runs (APP_BASE_URL). Remembered per
// project so repeated runs do not re-enter it; empty = spec default.
const baseUrl = ref('')

// Create-script form
const showCreate = ref(false)
const overlayEl = ref<HTMLElement | null>(null)
useClickOutside(overlayEl, () => showCreate.value = false, true)
const createName = ref('')
const createType = ref<'python' | 'bash'>('python')
const createContent = ref('')
const createError = ref<string | null>(null)
const creating = ref(false)

// Monotonic guard: a slow in-flight load must not clobber the result of a
// newer one (rapid project switching) - same pattern as KnowledgeBrowserPage.
let scriptLoadSeq = 0
let specLoadSeq = 0

async function loadScripts() {
  const seq = ++scriptLoadSeq
  const pid = projectStore.currentProject?.project_id
  if (!pid) return
  try {
    const data = await apiFetch<{ script_id: string; name: string; script_type: string }[]>(
      `/api/projects/${pid}/scripts`
    )
    if (seq !== scriptLoadSeq) return  // stale response - a newer load owns the list
    const prevScripts = new Map(scripts.value.map((sc) => [sc.script_id, sc]))
    scripts.value = data.map((s) => {
      const old = prevScripts.get(s.script_id)
      return old?.run_id
        ? { ...s, run_id: old.run_id, status: old.status }
        : { ...s, status: 'idle' }
    })
  } catch {
    // non-critical
  }
}

async function loadSpecs() {
  const seq = ++specLoadSeq
  const pid = projectStore.currentProject?.project_id
  if (!pid) return
  try {
    const data = await apiFetch<{ slug: string; title: string; file: string }[]>(
      `/api/projects/${pid}/playwright/specs`
    )
    if (seq !== specLoadSeq) return
    // Merge, don't replace: a reload (tab switch) must keep the run_id/status
    // of a run that is still executing - a fresh 'idle' row would lose the
    // live connection and the done-frame update.
    const prev = new Map(specs.value.map((sp) => [sp.slug, sp]))
    specs.value = data.map((s) => {
      const old = prev.get(s.slug)
      return old?.run_id
        ? { ...s, run_id: old.run_id, status: old.status }
        : { ...s, status: 'idle' }
    })
  } catch {
    // non-critical
  }
}

function switchSection(next: ExecutorSection) {
  section.value = next
  if (next === 'playwright') loadSpecs()
}

async function runScript(scriptId: string) {
  // Capture the project at request time: if it switches while the POST is in
  // flight, the projectId watcher has already reset activeRunId - connecting
  // anyway would stream another project's run into this page.
  const pidAtStart = projectStore.currentProject?.project_id
  runError.value = null
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
  } catch (e) {
    const item = scripts.value.find((s) => s.script_id === scriptId)
    if (item) item.status = 'failed'
    runError.value = e instanceof Error ? e.message : t('scriptExecutor.runFailed')
  }
}

async function runSpec(slug: string) {
  const pidAtStart = projectStore.currentProject?.project_id
  if (!pidAtStart) return
  runError.value = null
  const env = baseUrl.value.trim() ? { APP_BASE_URL: baseUrl.value.trim() } : {}
  try {
    const result = await apiFetch<{ run_id: string; status: string }>(
      `/api/projects/${pidAtStart}/playwright/specs/${encodeURIComponent(slug)}/run`,
      { method: 'POST', body: JSON.stringify({ env }) }
    )
    if (projectStore.currentProject?.project_id !== pidAtStart) return
    const spec = specs.value.find((s) => s.slug === slug)
    if (spec) {
      spec.run_id = result.run_id
      spec.status = result.status
    }
    connectToRun(result.run_id)
  } catch (e) {
    const spec = specs.value.find((sp) => sp.slug === slug)
    if (spec) spec.status = 'failed'
    runError.value = e instanceof Error ? e.message : t('scriptExecutor.runFailed')
  }
}

async function createScript() {
  const pid = projectStore.currentProject?.project_id
  if (!pid) return
  if (!createName.value.trim()) {
    createError.value = t('scriptExecutor.invalidName')
    return
  }
  if (!createContent.value.trim()) {
    createError.value = t('scriptExecutor.invalidContent')
    return
  }
  creating.value = true
  createError.value = null
  try {
    await apiFetch(`/api/projects/${pid}/scripts`, {
      method: 'POST',
      body: JSON.stringify({
        name: createName.value.trim(),
        script_type: createType.value,
        content: createContent.value,
      }),
    })
    showCreate.value = false
    createName.value = ''
    createContent.value = ''
    await loadScripts()
  } catch (e) {
    createError.value = e instanceof Error ? e.message : t('scriptExecutor.createFailed')
  } finally {
    creating.value = false
  }
}

// Runs are looked up across both lists: a done frame for a Playwright run
// must update the spec row even while the scripts list is rendered.
function findRunItem(runId: string): { run_id?: string; status: string } | undefined {
  return scripts.value.find((s) => s.run_id === runId)
    ?? specs.value.find((s) => s.run_id === runId)
}

onMounted(loadScripts)

// Project switched while this page stays mounted (route param changes):
// drop any socket tied to the previous project's run and reload the lists -
// otherwise logs from project A's run keep streaming into project B's view.
watch(() => projectStore.currentProject?.project_id, (pid) => {
  ws?.close()
  ws = null
  activeRunId.value = null
  logs.value = []
  runError.value = null
  loadScripts()
  loadSpecs()
  baseUrl.value = pid ? localStorage.getItem(`pw-base-url:${pid}`) ?? '' : ''
})

watch(baseUrl, (value) => {
  const pid = projectStore.currentProject?.project_id
  if (pid) localStorage.setItem(`pw-base-url:${pid}`, value)
})

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
  activeRunId.value = runId
  logs.value = []
  runError.value = null

  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}${apiBase}/ws/script-runs/${runId}`)
  ws.onmessage = (e) => {
    // A stale socket from a previous run may still deliver in-flight frames
    // after ws.close() - never let them pollute the active run's log.
    if (!mounted.value || activeRunId.value !== runId) return
    let msg: { type?: string; level?: string; text?: string; log?: string; status?: string }
    try {
      msg = JSON.parse(e.data as string) as typeof msg
    } catch {
      // Server may emit plain-text frames (ping/raw error) - never let a
      // parse failure kill the message handler for all subsequent frames.
      pushLog({ text: String(e.data), level: 'info' })
      return
    }
    if (msg.type === 'done') {
      // Authoritative final state from the server (completed OR failed with
      // the subprocess exit code). The old handler dropped this frame and
      // stamped 'completed' on close - failed scripts showed green.
      const item = findRunItem(runId)
      if (item) item.status = msg.status ?? 'completed'
      return
    }
    pushLog({ text: msg.text ?? msg.log ?? String(e.data), level: msg.level ?? 'info' })
  }
  ws.onclose = () => {
    const item = findRunItem(runId)
    // Only touch the row when this socket still belongs to the active run -
    // a manual close (run switched) must not mark a stale state.
    if (!item || !mounted.value || activeRunId.value !== runId) return
    // Closed WITHOUT a done frame -> abnormal (network drop / server restart).
    // The run may still be executing server-side; never stamp 'completed'.
    if (item.status === 'running' || item.status === 'pending') {
      item.status = 'failed'
      pushLog({ text: t('scriptExecutor.connectionLost'), level: 'error' })
    }
  }
}

onBeforeUnmount(() => {
  mounted.value = false
  ws?.close()
})
</script>
