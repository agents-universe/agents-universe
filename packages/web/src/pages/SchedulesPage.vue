<template>
  <div class="schedules-page">
    <div class="schedules-page-inner">
      <header class="schedules-header">
        <div class="schedules-header-left">
          <span class="schedules-header-icon"><CalendarClock :size="20" /></span>
          <div>
            <h1 class="schedules-title">{{ t('scheduledTasks.title') }}</h1>
            <p class="schedules-sub">{{ t('scheduledTasks.subtitle') }}</p>
          </div>
        </div>
        <button class="btn-primary schedules-new" @click="openCreate">
          <Plus :size="15" />
          {{ t('scheduledTasks.newTask') }}
        </button>
      </header>

      <p v-if="message.text" class="schedules-message" :class="message.ok ? 'success' : 'error'">
        {{ message.ok ? '✓' : '✗' }} {{ message.text }}
      </p>

      <div v-if="store.loading && !store.tasks.length" class="schedules-state">
        <Loader2 :size="18" class="schedules-spin" />
        <span>{{ t('common.loading') }}</span>
      </div>

      <div v-else-if="store.error" class="schedules-message error">✗ {{ store.error }}</div>

      <div v-else-if="!store.tasks.length" class="schedules-empty">
        <span class="schedules-empty-icon"><CalendarClock :size="28" /></span>
        <p class="schedules-empty-title">{{ t('scheduledTasks.empty') }}</p>
        <button class="btn-primary" @click="openCreate">{{ t('scheduledTasks.newTask') }}</button>
      </div>

      <div v-else class="schedules-list">
        <div v-for="task in store.tasks" :key="task.schedule_id" class="schedule-card">
          <div class="schedule-card-main">
            <div class="schedule-card-head">
              <div class="schedule-card-heading">
                <span class="schedule-card-title">{{ task.name }}</span>
                <span class="schedule-target-chip" :class="task.target_type">
                  <component :is="targetIcon(task.target_type)" :size="11" />
                  {{ t(`scheduledTasks.target_${task.target_type}`) }}
                </span>
              </div>
              <span class="schedule-status-chip" :class="task.enabled ? 'on' : 'off'">
                <span class="schedule-status-dot" />
                {{ task.enabled ? t('scheduledTasks.enabled') : t('scheduledTasks.disabled') }}
              </span>
            </div>

            <p v-if="task.description" class="schedule-card-desc">{{ task.description }}</p>

            <div class="schedule-card-meta">
              <span class="schedule-meta-chip">
                <Repeat :size="12" />
                {{ task.schedule }}
                <code class="schedule-cron">{{ task.cron_expr }}</code>
              </span>
              <span class="schedule-meta-chip">
                <Globe :size="12" />
                {{ task.timezone }}
              </span>
              <span class="schedule-meta-chip">
                <Clock :size="12" />
                {{ t('scheduledTasks.nextRun') }}
                {{ task.next_run_at ? fmtInZone(task.next_run_at, task.timezone) : t('scheduledTasks.notScheduled') }}
              </span>
              <span v-if="task.last_run_at" class="schedule-meta-chip">
                <History :size="12" />
                {{ t('scheduledTasks.lastRun') }}
                {{ fmtInZone(task.last_run_at, task.timezone) }}
                <span :class="['schedule-run-status', task.last_status || '']">
                  {{ statusLabel(task.last_status) }}
                </span>
              </span>
              <span class="schedule-meta-chip">
                <Link2 :size="12" />
                {{ targetDetail(task) }}
              </span>
            </div>
          </div>

          <div class="schedule-card-actions">
            <div class="schedule-card-actions-left">
              <button class="btn-sm" :disabled="busyId === task.schedule_id" @click="runNow(task)">
                <Play :size="12" />
                {{ t('scheduledTasks.runNow') }}
              </button>
              <button class="btn-sm secondary" @click="toggleHistory(task)">
                <ListChecks :size="12" />
                {{ t('scheduledTasks.runs') }}
                <ChevronDown :size="12" class="schedule-caret" :class="{ flip: expandedId === task.schedule_id }" />
              </button>
            </div>

            <div class="schedule-card-actions-right">
              <label class="schedule-toggle" :title="task.enabled ? t('scheduledTasks.disable') : t('scheduledTasks.enable')">
                <span class="schedule-switch" :class="{ on: task.enabled }">
                  <input
                    type="checkbox"
                    :checked="task.enabled"
                    :disabled="busyId === task.schedule_id"
                    @change="toggleEnabled(task, $event)"
                  />
                  <span class="schedule-switch-knob" />
                </span>
              </label>
              <button class="btn-sm secondary" @click="openEdit(task)">
                <Pencil :size="12" />
                {{ t('scheduledTasks.edit') }}
              </button>
              <button class="btn-sm danger" :disabled="busyId === task.schedule_id" @click="removeTask(task)">
                <Trash2 :size="12" />
                {{ t('common.delete') }}
              </button>
            </div>
          </div>

          <!-- Run history -->
          <div v-if="expandedId === task.schedule_id" class="schedule-history">
            <div v-if="historyLoading" class="schedule-history-state">
              <Loader2 :size="14" class="schedules-spin" /> {{ t('common.loading') }}
            </div>
            <div v-else-if="!history.length" class="schedule-history-state">
              {{ t('scheduledTasks.noRuns') }}
            </div>
            <div v-for="run in history" :key="run.run_id" class="schedule-run-row">
              <span :class="['schedule-run-status', run.status]">{{ statusLabel(run.status) }}</span>
              <span class="schedule-run-time">{{ fmtInZone(run.created_at, task.timezone) }}</span>
              <span class="schedule-run-trigger">
                {{ run.trigger === 'manual' ? t('scheduledTasks.triggerManual') : t('scheduledTasks.triggerSchedule') }}
              </span>
              <span class="schedule-run-summary" :title="run.error || run.summary || ''">
                {{ run.error || run.summary || '' }}
              </span>
              <button v-if="run.script_run_id" class="btn-sm secondary" @click="openLog(task, run)">
                <Terminal :size="12" />
                {{ t('scheduledTasks.viewLog') }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Create / edit dialog -->
    <Teleport to="body">
      <div v-if="showForm" class="modal-overlay" @click.self="closeForm">
        <div class="modal-dialog schedule-form-modal">
          <div class="modal-header">
            <div class="modal-header-left">
              <span class="modal-header-icon"><CalendarClock :size="18" /></span>
              <h2 class="modal-title">
                {{ form.schedule_id ? t('scheduledTasks.editTitle') : t('scheduledTasks.createTitle') }}
              </h2>
            </div>
            <button class="modal-close" @click="closeForm" :title="t('common.close')">
              <X :size="16" />
            </button>
          </div>

          <div class="schedule-form">
            <label class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.name') }}</span>
              <input v-model="form.name" class="input" :placeholder="t('scheduledTasks.namePlaceholder')" />
            </label>

            <label class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.description') }}</span>
              <input v-model="form.description" class="input" :placeholder="t('scheduledTasks.descPlaceholder')" />
            </label>

            <label class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.targetType') }}</span>
              <select v-model="form.target_type" class="input">
                <option value="script">{{ t('scheduledTasks.target_script') }}</option>
                <option value="playwright">{{ t('scheduledTasks.target_playwright') }}</option>
                <option value="agent">{{ t('scheduledTasks.target_agent') }}</option>
              </select>
            </label>

            <label v-if="form.target_type === 'script'" class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.script') }}</span>
              <select v-model="form.script_id" class="input">
                <option value="" disabled>{{ t('scheduledTasks.chooseScript') }}</option>
                <option v-for="s in scripts" :key="s.script_id" :value="s.script_id">{{ s.name }}</option>
              </select>
            </label>

            <label v-else-if="form.target_type === 'playwright'" class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.spec') }}</span>
              <select v-model="form.spec_slug" class="input">
                <option value="" disabled>{{ t('scheduledTasks.chooseSpec') }}</option>
                <option v-for="s in specs" :key="s.slug" :value="s.slug">{{ s.title || s.slug }}</option>
              </select>
            </label>

            <template v-else>
              <label class="schedule-field">
                <span class="schedule-field-label">{{ t('scheduledTasks.agent') }}</span>
                <select v-model="form.agent_slug" class="input">
                  <option value="" disabled>{{ t('scheduledTasks.chooseAgent') }}</option>
                  <option v-for="a in agents" :key="a.slug" :value="a.slug">{{ a.label || a.slug }}</option>
                </select>
              </label>
              <label class="schedule-field">
                <span class="schedule-field-label">{{ t('scheduledTasks.prompt') }}</span>
                <textarea
                  v-model="form.prompt"
                  class="input schedule-prompt"
                  rows="4"
                  :placeholder="t('scheduledTasks.promptPlaceholder')"
                />
                <span class="schedule-field-hint">{{ t('scheduledTasks.promptHint') }}</span>
              </label>
            </template>

            <div class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.cron') }}</span>
              <div class="schedule-cron-row">
                <select class="input schedule-preset" :value="presetKey" @change="applyPreset($event)">
                  <option value="custom">{{ t('scheduledTasks.presetCustom') }}</option>
                  <option v-for="p in CRON_PRESETS" :key="p.cron" :value="p.cron">
                    {{ t(`scheduledTasks.${p.labelKey}`) }}
                  </option>
                </select>
                <input
                  v-model="form.cron_expr"
                  class="input schedule-cron-input"
                  spellcheck="false"
                  :placeholder="t('scheduledTasks.cronPlaceholder')"
                  @input="presetKey = 'custom'"
                />
              </div>
              <span v-if="previewError" class="schedule-field-hint error">{{ previewError }}</span>
              <span v-else-if="preview" class="schedule-field-hint">
                {{ preview.description }} ·
                {{ t('scheduledTasks.nextRuns') }}
                {{ preview.next_runs.map(r => fmtInZone(r, form.timezone)).join(' / ') }}
              </span>
              <span v-else class="schedule-field-hint">{{ t('scheduledTasks.cronHint') }}</span>
            </div>

            <label class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.timezone') }}</span>
              <select v-model="form.timezone" class="input">
                <option v-for="tz in TIMEZONES" :key="tz" :value="tz">{{ tz }}</option>
              </select>
            </label>

            <label class="schedule-field">
              <span class="schedule-field-label">{{ t('scheduledTasks.conversation') }}</span>
              <select v-model="form.conversation_id" class="input">
                <option value="">{{ t('scheduledTasks.noConversation') }}</option>
                <option v-for="c in conversations" :key="c.conversation_id" :value="c.conversation_id">
                  {{ c.title || c.conversation_id.slice(0, 8) }}
                </option>
              </select>
              <span class="schedule-field-hint">{{ t('scheduledTasks.conversationHint') }}</span>
            </label>

            <label class="schedule-field schedule-field-inline">
              <input v-model="form.enabled" type="checkbox" />
              <span class="schedule-field-label">{{ t('scheduledTasks.enabledHint') }}</span>
            </label>

            <p v-if="formMessage" class="schedules-message error">✗ {{ formMessage }}</p>

            <div class="schedule-form-actions">
              <button class="btn-primary" :disabled="saving || !canSubmit" @click="submit">
                {{ saving ? t('common.saving') : t('common.save') }}
              </button>
              <button class="btn-sm secondary" @click="closeForm">{{ t('common.cancel') }}</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Live log drawer -->
    <Teleport to="body">
      <div v-if="logOpen" class="modal-overlay" @click.self="closeLogDrawer">
        <div class="modal-dialog schedule-log-modal">
          <div class="modal-header">
            <div class="modal-header-left">
              <span class="modal-header-icon"><Terminal :size="18" /></span>
              <h2 class="modal-title">{{ t('scheduledTasks.logTitle', { name: logTitle }) }}</h2>
            </div>
            <button class="modal-close" @click="closeLogDrawer" :title="t('common.close')">
              <X :size="16" />
            </button>
          </div>
          <div class="script-log-output" ref="logPanel">
            <div v-for="(line, i) in logs" :key="i" class="script-log-line" :class="line.level">
              {{ line.text }}
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  CalendarClock, Plus, Loader2, Play, Pencil, Trash2, Terminal, ChevronDown,
  Clock, Globe, Repeat, Link2, ListChecks, Bot, FlaskConical, X, History,
} from 'lucide-vue-next'
import { apiFetch } from '@/api/client'
import { schedulesApi, type ScheduledTask, type ScheduledTaskRun, type ScheduleTargetType } from '@/api/schedules'
import { conversationsApi } from '@/api/conversations'
import { useSchedulesStore } from '@/stores/schedules'
import { useAgentStore } from '@/stores/agent'
import { useScriptRunLog } from '@/composables/useScriptRunLog'

interface ScriptItem { script_id: string; name: string; script_type: string }
interface SpecItem { slug: string; title: string; file: string }

const route = useRoute()
const { t } = useI18n()
const store = useSchedulesStore()
const agentStore = useAgentStore()
const projectId = computed(() => route.params.projectId as string)

const { logs, logPanel, connectToRun, closeLog } = useScriptRunLog({
  onDone: () => { void reloadHistoryIfExpanded() },
})

/* ── Reference data for the dialog ────────────────────────────── */
const scripts = ref<ScriptItem[]>([])
const specs = ref<SpecItem[]>([])
const conversations = ref<{ conversation_id: string; title: string | null }[]>([])
const agents = computed(() => agentStore.agents)

async function loadReferenceData() {
  const pid = projectId.value
  if (!pid) return
  const [scriptList, specList, convList] = await Promise.all([
    apiFetch<ScriptItem[]>(`/api/projects/${encodeURIComponent(pid)}/scripts`).catch(() => []),
    apiFetch<SpecItem[]>(`/api/projects/${encodeURIComponent(pid)}/playwright/specs`).catch(() => []),
    conversationsApi.list(pid).catch(() => []),
  ])
  if (pid !== projectId.value) return
  // Playwright specs are scheduled with target_type=playwright, not as scripts.
  scripts.value = scriptList.filter((s) => s.script_type !== 'playwright')
  specs.value = specList
  conversations.value = convList.map((c) => ({ conversation_id: c.conversation_id, title: c.title }))
}

/* ── List state ───────────────────────────────────────────────── */
const busyId = ref<string | null>(null)
const message = reactive({ text: '', ok: true })
const expandedId = ref<string | null>(null)
const history = ref<ScheduledTaskRun[]>([])
const historyLoading = ref(false)
// Timers fire after a run was launched (the row updates a moment later).
const timers: ReturnType<typeof setTimeout>[] = []

function later(fn: () => void, ms: number) {
  timers.push(setTimeout(fn, ms))
}

function setMessage(text: string, ok = true) {
  message.text = text
  message.ok = ok
}

/* ── Formatting ───────────────────────────────────────────────── */
function fmtInZone(iso: string | null, tz: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: tz, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).format(d)
  } catch {
    return d.toLocaleString()
  }
}

function statusLabel(status: string | null): string {
  if (!status) return ''
  return t(`scheduledTasks.status_${status}`)
}

function targetIcon(type: ScheduleTargetType) {
  return type === 'script' ? Terminal : type === 'playwright' ? FlaskConical : Bot
}

function targetDetail(task: ScheduledTask): string {
  if (task.target_type === 'script') {
    return scripts.value.find((s) => s.script_id === task.script_id)?.name ?? t('scheduledTasks.targetMissing')
  }
  if (task.target_type === 'playwright') return task.spec_slug ?? t('scheduledTasks.targetMissing')
  const agent = agents.value.find((a) => a.slug === task.agent_slug)
  return agent?.label || task.agent_slug || t('scheduledTasks.targetMissing')
}

/* ── List actions ─────────────────────────────────────────────── */
async function toggleHistory(task: ScheduledTask) {
  if (expandedId.value === task.schedule_id) {
    expandedId.value = null
    history.value = []
    return
  }
  expandedId.value = task.schedule_id
  history.value = []
  historyLoading.value = true
  try {
    const runs = await schedulesApi.runs(task.schedule_id)
    if (expandedId.value === task.schedule_id) history.value = runs
  } catch (e) {
    setMessage(e instanceof Error ? e.message : t('scheduledTasks.loadRunsFailed'), false)
  } finally {
    historyLoading.value = false
  }
}

async function reloadHistoryIfExpanded() {
  const id = expandedId.value
  if (!id) return
  try {
    const runs = await schedulesApi.runs(id)
    if (expandedId.value === id) history.value = runs
  } catch { /* history is a read-only view — a failed refresh is not fatal */ }
}

async function toggleEnabled(task: ScheduledTask, event: Event) {
  const enabled = (event.target as HTMLInputElement).checked
  busyId.value = task.schedule_id
  try {
    await store.setEnabled(projectId.value, task.schedule_id, enabled)
  } catch (e) {
    setMessage(e instanceof Error ? e.message : t('scheduledTasks.updateFailed'), false)
    await store.load(projectId.value)
  } finally {
    busyId.value = null
  }
}

async function runNow(task: ScheduledTask) {
  busyId.value = task.schedule_id
  try {
    await store.runNow(task.schedule_id)
    setMessage(t('scheduledTasks.runStarted', { name: task.name }))
    // The run registers in the DB a moment later; refresh the views that
    // show its outcome instead of leaving a stale row on screen.
    later(() => { if (expandedId.value === task.schedule_id) void reloadHistoryIfExpanded() }, 1500)
    later(() => { void store.load(projectId.value) }, 4000)
  } catch (e) {
    setMessage(e instanceof Error ? e.message : t('scheduledTasks.runFailed'), false)
  } finally {
    busyId.value = null
  }
}

async function removeTask(task: ScheduledTask) {
  if (!window.confirm(t('scheduledTasks.deleteConfirm', { name: task.name }))) return
  busyId.value = task.schedule_id
  try {
    await store.remove(projectId.value, task.schedule_id)
    if (expandedId.value === task.schedule_id) {
      expandedId.value = null
      history.value = []
    }
  } catch (e) {
    setMessage(e instanceof Error ? e.message : t('scheduledTasks.deleteFailed'), false)
  } finally {
    busyId.value = null
  }
}

/* ── Log drawer ───────────────────────────────────────────────── */
const logOpen = ref(false)
const logTitle = ref('')

function openLog(task: ScheduledTask, run: ScheduledTaskRun) {
  if (!run.script_run_id) return
  logTitle.value = task.name
  logOpen.value = true
  connectToRun(run.script_run_id)
}

function closeLogDrawer() {
  logOpen.value = false
  closeLog()
}

/* ── Create / edit dialog ─────────────────────────────────────── */
const TIMEZONES = [
  'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Singapore',
  'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'UTC',
]

const CRON_PRESETS = [
  { cron: '*/15 * * * *', labelKey: 'presetEvery15' },
  { cron: '0 * * * *', labelKey: 'presetHourly' },
  { cron: '0 9 * * *', labelKey: 'presetDaily9' },
  { cron: '0 0 * * *', labelKey: 'presetDaily0' },
  { cron: '0 9 * * 1-5', labelKey: 'presetWeekdays9' },
  { cron: '0 9 * * 1', labelKey: 'presetWeeklyMon' },
  { cron: '0 0 1 * *', labelKey: 'presetMonthly' },
]

interface FormState {
  schedule_id: string | null
  name: string
  description: string
  target_type: ScheduleTargetType
  script_id: string
  spec_slug: string
  agent_slug: string
  prompt: string
  conversation_id: string
  cron_expr: string
  timezone: string
  enabled: boolean
}

const showForm = ref(false)
const saving = ref(false)
const formMessage = ref('')
const presetKey = ref('custom')
const preview = ref<{ description: string; next_runs: string[] } | null>(null)
const previewError = ref('')

const form = reactive<FormState>({
  schedule_id: null,
  name: '',
  description: '',
  target_type: 'script',
  script_id: '',
  spec_slug: '',
  agent_slug: '',
  prompt: '',
  conversation_id: '',
  cron_expr: '0 9 * * *',
  timezone: 'Asia/Shanghai',
  enabled: true,
})

const canSubmit = computed(() => {
  if (!form.name.trim() || !form.cron_expr.trim()) return false
  if (form.target_type === 'script') return !!form.script_id
  if (form.target_type === 'playwright') return !!form.spec_slug
  return !!form.agent_slug && !!form.prompt.trim() && !!form.conversation_id
})

function resetForm() {
  form.schedule_id = null
  form.name = ''
  form.description = ''
  form.target_type = 'script'
  form.script_id = ''
  form.spec_slug = ''
  form.agent_slug = ''
  form.prompt = ''
  form.conversation_id = ''
  form.cron_expr = '0 9 * * *'
  form.timezone = 'Asia/Shanghai'
  form.enabled = true
  presetKey.value = '0 9 * * *'
  formMessage.value = ''
  preview.value = null
  previewError.value = ''
}

async function openCreate() {
  resetForm()
  // Default the delivery target to the newest conversation of the project.
  form.conversation_id = conversations.value[0]?.conversation_id ?? ''
  showForm.value = true
  await loadReferenceData()
}

async function openEdit(task: ScheduledTask) {
  resetForm()
  form.schedule_id = task.schedule_id
  form.name = task.name
  form.description = task.description ?? ''
  form.target_type = task.target_type
  form.script_id = task.script_id ?? ''
  form.spec_slug = task.spec_slug ?? ''
  form.agent_slug = task.agent_slug ?? ''
  form.prompt = task.prompt ?? ''
  form.conversation_id = task.conversation_id ?? ''
  form.cron_expr = task.cron_expr
  form.timezone = task.timezone
  form.enabled = task.enabled
  presetKey.value = CRON_PRESETS.some((p) => p.cron === task.cron_expr) ? task.cron_expr : 'custom'
  showForm.value = true
  await loadReferenceData()
}

function closeForm() {
  showForm.value = false
  formMessage.value = ''
}

function applyPreset(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  presetKey.value = value
  if (value !== 'custom') form.cron_expr = value
}

// Debounced "next three fire times" preview — the backend owns cron parsing,
// so the dialog never has to guess whether an expression is valid.
let previewTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => [form.cron_expr, form.timezone] as const,
  () => {
    if (!showForm.value) return
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = setTimeout(async () => {
      const pid = projectId.value
      const expr = form.cron_expr.trim()
      if (!pid || !expr) return
      try {
        const result = await schedulesApi.preview(pid, expr, form.timezone)
        if (expr === form.cron_expr.trim()) {
          preview.value = result
          previewError.value = ''
        }
      } catch (e) {
        preview.value = null
        previewError.value = e instanceof Error ? e.message : t('scheduledTasks.invalidCron')
      }
    }, 400)
  },
)

async function submit() {
  saving.value = true
  formMessage.value = ''
  const payload = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    target_type: form.target_type,
    script_id: form.target_type === 'script' ? form.script_id : null,
    spec_slug: form.target_type === 'playwright' ? form.spec_slug : null,
    agent_slug: form.target_type === 'agent' ? form.agent_slug : null,
    prompt: form.target_type === 'agent' ? form.prompt : null,
    conversation_id: form.conversation_id || null,
    cron_expr: form.cron_expr.trim(),
    timezone: form.timezone,
    enabled: form.enabled,
  }
  try {
    if (form.schedule_id) {
      await store.update(projectId.value, form.schedule_id, payload)
      setMessage(t('scheduledTasks.updated', { name: payload.name }))
    } else {
      await store.create(projectId.value, payload)
      setMessage(t('scheduledTasks.created', { name: payload.name }))
    }
    showForm.value = false
  } catch (e) {
    formMessage.value = e instanceof Error ? e.message : t('scheduledTasks.saveFailed')
  } finally {
    saving.value = false
  }
}

/* ── Lifecycle ────────────────────────────────────────────────── */
onMounted(async () => {
  if (!agentStore.agents.length) await agentStore.fetchAgents(projectId.value).catch(() => undefined)
  await loadReferenceData()
  await store.load(projectId.value)
})

watch(projectId, async (pid) => {
  if (!pid) return
  closeLogDrawer()
  expandedId.value = null
  history.value = []
  message.text = ''
  await loadReferenceData()
  await store.load(pid)
})

onBeforeUnmount(() => {
  for (const timer of timers) clearTimeout(timer)
  if (previewTimer) clearTimeout(previewTimer)
})
</script>

<style scoped>
/* Fills the AppLayout center panel (.center-content is a flex column with
   overflow hidden), keeping the card list at a readable width inside it. */
.schedules-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 22px 48px;
}

.schedules-page-inner {
  max-width: 940px;
  margin: 0 auto;
}

/* ── Header ─────────────────────────────────────────────────────────── */
.schedules-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.schedules-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.schedules-header-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--accent-gradient);
  color: #fff;
  box-shadow: var(--accent-glow);
}

.schedules-title {
  margin: 0;
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.02em;
}

.schedules-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
}

.schedules-new {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

/* ── Messages & states ──────────────────────────────────────────────── */
.schedules-message {
  padding: 9px 13px;
  border-radius: 8px;
  font-size: 12.5px;
  margin: 0 0 14px;
}
.schedules-message.success { background: rgba(74, 222, 128, 0.12); color: #4ade80; }
.schedules-message.error { background: rgba(248, 113, 113, 0.12); color: #f87171; }

.schedules-state {
  padding: 48px 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 13px;
}

.schedules-spin { animation: schedules-spin 0.9s linear infinite; }

@keyframes schedules-spin { to { transform: rotate(360deg); } }

.schedules-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 56px 0;
  text-align: center;
  color: var(--text-muted);
}

.schedules-empty-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: var(--accent-dim);
  color: var(--accent);
}

.schedules-empty-title {
  margin: 0;
  font-size: 13.5px;
  max-width: 420px;
  line-height: 1.6;
}

/* ── Cards ──────────────────────────────────────────────────────────── */
.schedules-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.schedule-card {
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.32);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.schedule-card:hover {
  border-color: var(--border-strong);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4), 0 0 20px rgba(107, 159, 255, 0.04);
}

.schedule-card-main { padding: 16px 18px 12px; }

.schedule-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.schedule-card-heading {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.schedule-card-title {
  font-size: 15.5px;
  font-weight: 650;
  letter-spacing: -0.01em;
  overflow-wrap: anywhere;
}

.schedule-target-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  font-weight: 500;
  padding: 3px 8px;
  border-radius: 999px;
  background: var(--accent-dim);
  color: var(--accent);
  white-space: nowrap;
}
.schedule-target-chip.playwright { background: rgba(167, 139, 250, 0.14); color: #a78bfa; }
.schedule-target-chip.agent { background: rgba(56, 189, 248, 0.14); color: #38bdf8; }

.schedule-status-chip {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-weight: 500;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.schedule-status-chip.on { background: rgba(74, 222, 128, 0.12); color: #4ade80; }
.schedule-status-chip.off { background: rgba(127, 127, 127, 0.12); color: var(--text-muted); }

.schedule-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.schedule-status-chip.on .schedule-status-dot { box-shadow: 0 0 6px currentColor; }

.schedule-card-desc {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.schedule-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-top: 12px;
}

.schedule-meta-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--text-muted);
  min-width: 0;
}

.schedule-cron {
  font-family: var(--font-mono, 'SF Mono', 'Cascadia Code', monospace);
  font-size: 10.5px;
  color: var(--text-secondary);
  background: var(--bg-tertiary, rgba(127, 127, 127, 0.12));
  padding: 1px 5px;
  border-radius: 5px;
}

.schedule-card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 18px;
  border-top: 1px solid var(--glass-border);
  flex-wrap: wrap;
}

.schedule-card-actions-left,
.schedule-card-actions-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.schedule-card-actions-left button,
.schedule-card-actions-right button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.schedule-caret { transition: transform 0.15s; }
.schedule-caret.flip { transform: rotate(180deg); }

.schedule-switch {
  position: relative;
  display: inline-block;
  width: 34px;
  height: 19px;
  border-radius: 999px;
  background: var(--bg-tertiary, rgba(127, 127, 127, 0.25));
  transition: background 0.15s;
  cursor: pointer;
}
.schedule-switch.on { background: var(--accent); }
.schedule-switch input {
  position: absolute;
  inset: 0;
  opacity: 0;
  margin: 0;
  cursor: pointer;
}
.schedule-switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.15s;
  pointer-events: none;
}
.schedule-switch.on .schedule-switch-knob { transform: translateX(15px); }

/* ── Run history ────────────────────────────────────────────────────── */
.schedule-history {
  border-top: 1px solid var(--glass-border);
  padding: 10px 18px 14px;
  background: rgba(0, 0, 0, 0.12);
}

.schedule-history-state {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 0;
  font-size: 12px;
  color: var(--text-muted);
}

.schedule-run-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  font-size: 12px;
  border-bottom: 1px dashed var(--glass-border);
}
.schedule-run-row:last-child { border-bottom: none; }

.schedule-run-status {
  flex: none;
  font-size: 10.5px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(127, 127, 127, 0.14);
  color: var(--text-muted);
}
.schedule-run-status.completed { background: rgba(74, 222, 128, 0.12); color: #4ade80; }
.schedule-run-status.failed { background: rgba(248, 113, 113, 0.12); color: #f87171; }
.schedule-run-status.running { background: rgba(56, 189, 248, 0.14); color: #38bdf8; }
.schedule-run-status.skipped { background: rgba(251, 191, 36, 0.14); color: #fbbf24; }

.schedule-run-time,
.schedule-run-trigger { flex: none; color: var(--text-muted); }

.schedule-run-summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

/* ── Dialog ─────────────────────────────────────────────────────────── */
.schedule-form-modal,
.schedule-log-modal {
  width: min(620px, 92vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
}

.schedule-log-modal { width: min(760px, 92vw); }

.schedule-form {
  padding: 16px 20px 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.schedule-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.schedule-field-inline {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}

.schedule-field-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.schedule-field-hint {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.5;
}
.schedule-field-hint.error { color: #f87171; }

.schedule-prompt { resize: vertical; font-family: inherit; }

.schedule-cron-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.schedule-preset { flex: 0 0 42%; }

.schedule-cron-input {
  flex: 1;
  font-family: var(--font-mono, 'SF Mono', 'Cascadia Code', monospace);
}

.schedule-form-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
}

.schedule-log-modal .script-log-output {
  flex: 1;
  min-height: 260px;
  max-height: 62vh;
  margin: 0 20px 20px;
}

@media (max-width: 720px) {
  .schedule-cron-row { flex-direction: column; align-items: stretch; }
  .schedule-preset { flex: none; }
  .schedule-card-actions { flex-direction: column; align-items: stretch; }
}
</style>
