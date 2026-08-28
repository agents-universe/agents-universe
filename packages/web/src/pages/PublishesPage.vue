<template>
  <div class="publishes-page">
    <header class="publishes-header">
      <div class="publishes-header-left">
        <span class="publishes-header-icon"><Rocket :size="20" /></span>
        <div>
          <h1 class="publishes-title">{{ t('publishesPage.title') }}</h1>
          <p class="publishes-sub">{{ t('publishesPage.subtitle') }}</p>
        </div>
      </div>
      <button class="btn-primary publishes-new" @click="showCreate = true">
        <Plus :size="15" />
        {{ t('publishesPage.newPublish') }}
      </button>
    </header>

    <p v-if="message.text" class="publishes-message" :class="message.ok ? 'success' : 'error'">
      {{ message.ok ? '✓' : '✗' }} {{ message.text }}
    </p>

    <div v-if="loading" class="publishes-state">
      <Loader2 :size="18" class="publishes-spin" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <div v-else-if="!publishes.length" class="publishes-empty">
      <span class="publishes-empty-icon"><Rocket :size="28" /></span>
      <p class="publishes-empty-title">{{ t('publishesPage.empty') }}</p>
      <button class="btn-primary" @click="showCreate = true">{{ t('publishesPage.newPublish') }}</button>
    </div>

    <div v-else class="publishes-list">
      <div v-for="p in publishes" :key="p.publish_id" class="publish-card">
        <div class="publish-card-main">
          <div class="publish-card-head">
            <div class="publish-card-heading">
              <span class="publish-card-title">{{ p.title || p.agent_slug }}</span>
              <code class="publish-card-slug">/p/{{ p.publish_id }}</code>
            </div>
            <div class="publish-card-status">
              <span :class="['publish-status-chip', p.page_enabled ? 'on' : 'off']">
                <span class="publish-status-dot" />
                {{ t('publishesPage.pageToggle') }}
              </span>
              <span :class="['publish-status-chip', p.api_enabled ? 'on' : 'off']">
                <span class="publish-status-dot" />
                {{ t('publishesPage.apiToggle') }}
              </span>
            </div>
          </div>

          <p v-if="p.description" class="publish-card-desc">{{ p.description }}</p>

          <div class="publish-card-meta">
            <span class="publish-meta-chip">
              <Bot :size="12" />
              {{ agentLabel(p.agent_slug) }}
            </span>
            <span class="publish-meta-chip">
              <Folder :size="12" />
              {{ projectLabel(p.project_id) }}
            </span>
            <span class="publish-meta-chip">
              <Cpu :size="12" />
              {{ modelLabel(p.model_config_id) }}
            </span>
            <span class="publish-meta-chip">
              <Clock :size="12" />
              {{ fmtDate(p.created_at) }}
            </span>
          </div>

          <!-- Keys -->
          <div class="publish-keys">
            <div v-if="!keysByPublish[p.publish_id]?.length" class="publish-keys-empty">
              <KeyRound :size="12" />
              {{ t('publishesPage.noKeys') }}
            </div>
            <div v-for="k in keysByPublish[p.publish_id]" :key="k.key_id" class="publish-key-row">
              <span class="publish-key-icon"><KeyRound :size="12" /></span>
              <code class="publish-key-hint">{{ k.key_hint }}</code>
              <span v-if="k.name" class="publish-key-name">{{ k.name }}</span>
              <span :class="['publish-key-status', k.is_active ? 'on' : 'off']">
                {{ k.is_active ? t('publishesPage.active') : t('publishesPage.revoked') }}
              </span>
              <button
                v-if="k.is_active"
                class="btn-sm danger"
                @click="revokeKey(p, k)"
                :disabled="busy"
              >{{ t('publishesPage.revoke') }}</button>
            </div>
            <div class="publish-key-add">
              <input
                v-model="newKeyNames[p.publish_id]"
                class="input publish-key-name-input"
                :placeholder="t('publishesPage.keyNamePlaceholder')"
                @keydown.enter.prevent="createKey(p)"
              />
              <button class="btn-sm" @click="createKey(p)" :disabled="busy">
                <Plus :size="12" />
                {{ t('publishesPage.newKey') }}
              </button>
              <span v-if="freshKeys[p.publish_id]" class="publish-fresh-key">
                {{ t('publishesPage.keyCopied') }}
              </span>
            </div>
          </div>

          <!-- Copy once -->
          <div v-if="pendingKeys[p.publish_id]" class="publish-copy-once">
            <AlertTriangle :size="13" />
            <code class="publish-raw-key">{{ pendingKeys[p.publish_id] }}</code>
            <button class="btn-sm publish-copy-btn" @click="copyKey(p.publish_id)">
              <Copy :size="12" />
              {{ t('publishesPage.copyKey') }}
            </button>
          </div>
        </div>

        <div class="publish-card-actions">
          <div class="publish-card-actions-left">
            <button class="btn-sm" @click="openPage(p)" :disabled="!p.page_enabled">
              <ExternalLink :size="12" />
              {{ t('publishesPage.openPage') }}
            </button>
            <button class="btn-sm secondary" @click="copyPageLink(p)" :disabled="!p.page_enabled">
              <Link :size="12" />
              {{ t('publishesPage.copyLink') }}
            </button>
          </div>

          <div class="publish-card-actions-right">
            <label class="publish-toggle" :title="t('publishesPage.pageToggle')">
              <span class="publish-toggle-label">{{ t('publishesPage.pageToggle') }}</span>
              <span class="publish-switch" :class="{ on: p.page_enabled }">
                <input type="checkbox" :checked="p.page_enabled" @change="togglePage(p, $event)" :disabled="busy" />
                <span class="publish-switch-knob" />
              </span>
            </label>
            <label class="publish-toggle" :title="t('publishesPage.apiToggle')">
              <span class="publish-toggle-label">{{ t('publishesPage.apiToggle') }}</span>
              <span class="publish-switch" :class="{ on: p.api_enabled }">
                <input type="checkbox" :checked="p.api_enabled" @change="toggleApi(p, $event)" :disabled="busy" />
                <span class="publish-switch-knob" />
              </span>
            </label>
            <button class="btn-sm danger" @click="remove(p)" :disabled="busy">
              <Trash2 :size="12" />
              {{ t('common.delete') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Create modal -->
    <Teleport to="body">
      <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
        <div class="modal-dialog publish-create-modal">
          <div class="modal-header">
            <div class="modal-header-left">
              <span class="modal-header-icon"><Rocket :size="18" /></span>
              <h2 class="modal-title">{{ t('publishesPage.createTitle') }}</h2>
            </div>
            <button class="modal-close" @click="showCreate = false" :title="t('common.close')">
              <X :size="16" />
            </button>
          </div>

          <div class="publish-form">
            <label class="publish-field">
              <span class="publish-field-label">{{ t('publishesPage.project') }}</span>
              <select v-model="form.project_id" class="input" @change="onProjectChange">
                <option value="" disabled>{{ t('publishesPage.chooseProject') }}</option>
                <option v-for="proj in manageableProjects" :key="proj.project_id" :value="proj.project_id">
                  {{ proj.display_name }}
                </option>
              </select>
            </label>

            <label class="publish-field">
              <span class="publish-field-label">{{ t('publishesPage.agent') }}</span>
              <select v-model="form.agent_slug" class="input" :disabled="!form.project_id">
                <option value="" disabled>{{ t('publishesPage.chooseAgent') }}</option>
                <option v-for="a in scopedAgents" :key="a.slug" :value="a.slug">
                  {{ a.label || a.slug }}
                </option>
              </select>
            </label>

            <label class="publish-field">
              <span class="publish-field-label">{{ t('publishesPage.model') }}</span>
              <select v-model="form.model_config_id" class="input">
                <option value="" disabled>{{ t('publishesPage.chooseModel') }}</option>
                <option v-for="c in userModelConfigs" :key="c.config_id" :value="c.config_id">
                  {{ c.model_id }} ({{ providerLabel(c.provider) }})
                </option>
              </select>
            </label>

            <label class="publish-field">
              <span class="publish-field-label">{{ t('publishesPage.formTitle') }}</span>
              <input v-model="form.title" class="input" :placeholder="t('publishesPage.titlePlaceholder')" />
            </label>

            <label class="publish-field">
              <span class="publish-field-label">{{ t('publishesPage.description') }}</span>
              <textarea v-model="form.description" class="input" rows="2" :placeholder="t('publishesPage.descPlaceholder')" />
            </label>

            <p v-if="formMessage.text" class="publishes-message" :class="formMessage.ok ? 'success' : 'error'">
              {{ formMessage.ok ? '✓' : '✗' }} {{ formMessage.text }}
            </p>

            <div class="publish-form-actions">
              <button class="btn-primary" @click="create" :disabled="creating || !form.agent_slug || !form.project_id || !form.model_config_id">
                {{ creating ? t('common.creating') : t('common.create') }}
              </button>
              <button class="btn-sm secondary" @click="showCreate = false">{{ t('common.cancel') }}</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  Rocket, X, Plus, Bot, Folder, Cpu, Clock, KeyRound, Copy, Link,
  ExternalLink, Trash2, Loader2, AlertTriangle,
} from 'lucide-vue-next'
import { publishApi } from '@/api/publish'
import { projectsApi } from '@/api/projects'
import { agentsApi } from '@/api/agents'
import { useAgentStore } from '@/stores/agent'
import { withBase } from '@/utils/basePath'
import type { PublishItem, PublishKeyItem, PublishKeyCreateResult } from '@/api/publish'
import type { Project } from '@/types'

const router = useRouter()
const { t } = useI18n()
const agentStore = useAgentStore()

const loading = ref(true)
const busy = ref(false)
const publishes = ref<PublishItem[]>([])
const keysByPublish = reactive<Record<string, PublishKeyItem[]>>({})
const newKeyNames = reactive<Record<string, string>>({})
// Raw key shown exactly once after creation, then copied.
const pendingKeys = reactive<Record<string, string>>({})
const freshKeys = reactive<Record<string, boolean>>({})
const message = reactive({ ok: true, text: '' })

// ── Create form ──────────────────────────────────────────────────────

const showCreate = ref(false)
const creating = ref(false)
const formMessage = reactive({ ok: true, text: '' })
const form = reactive({ project_id: '', agent_slug: '', model_config_id: '', title: '', description: '' })

const projects = ref<Project[]>([])
// Only projects the current user can publish (manager).
const manageableProjects = computed(() => projects.value.filter(p => p.can_manage))
const scopedAgents = ref<Array<{ slug: string; label: string; project_id: string | null }>>([])
const userModelConfigs = computed(() => agentStore.modelConfigs.filter(c => !c.is_system))

async function load() {
  loading.value = true
  try {
    const [pubs, projs, cfg] = await Promise.all([
      publishApi.list(),
      projectsApi.getProjects(),
      agentStore.fetchModelConfigs(),
    ])
    publishes.value = pubs
    projects.value = projs
    for (const p of pubs) {
      try {
        keysByPublish[p.publish_id] = await publishApi.listKeys(p.publish_id)
      } catch {
        keysByPublish[p.publish_id] = []
      }
    }
    void cfg
  } catch (e) {
    message.ok = false
    message.text = e instanceof Error ? e.message : t('common.loading')
  } finally {
    loading.value = false
  }
}

async function loadAgents(projectId: string) {
  try {
    const agents = await agentsApi.getAgents(projectId)
    scopedAgents.value = agents.map(a => ({ slug: a.slug, label: a.label, project_id: a.project_id ?? null }))
  } catch {
    scopedAgents.value = []
  }
}

function onProjectChange() {
  form.agent_slug = ''
  void loadAgents(form.project_id)
}

async function create() {
  creating.value = true
  formMessage.text = ''
  try {
    const created = await publishApi.create({
      agent_slug: form.agent_slug,
      project_id: form.project_id,
      model_config_id: form.model_config_id,
      title: form.title || null,
      description: form.description || null,
    })
    publishes.value = [created, ...publishes.value]
    keysByPublish[created.publish_id] = []
    showCreate.value = false
    form.project_id = ''
    form.agent_slug = ''
    form.model_config_id = ''
    form.title = ''
    form.description = ''
    message.ok = true
    message.text = t('publishesPage.created')
  } catch (e) {
    formMessage.ok = false
    formMessage.text = e instanceof Error ? e.message : t('common.createFailed')
  } finally {
    creating.value = false
  }
}

// ── Keys ─────────────────────────────────────────────────────────────

async function createKey(p: PublishItem) {
  busy.value = true
  try {
    const res = await publishApi.createKey(p.publish_id, newKeyNames[p.publish_id] || undefined)
    newKeyNames[p.publish_id] = ''
    const item: PublishKeyCreateResult = res
    keysByPublish[p.publish_id] = [item, ...(keysByPublish[p.publish_id] ?? [])]
    pendingKeys[p.publish_id] = item.key
    freshKeys[p.publish_id] = true
  } catch (e) {
    message.ok = false
    message.text = e instanceof Error ? e.message : t('publishesPage.keyCreateFailed')
  } finally {
    busy.value = false
  }
}

async function revokeKey(p: PublishItem, k: PublishKeyItem) {
  if (!window.confirm(t('publishesPage.revokeConfirm'))) return
  busy.value = true
  try {
    await publishApi.revokeKey(p.publish_id, k.key_id)
    k.is_active = false
    k.revoked_at = new Date().toISOString()
  } catch (e) {
    message.ok = false
    message.text = e instanceof Error ? e.message : t('publishesPage.keyRevokeFailed')
  } finally {
    busy.value = false
  }
}

async function copyKey(publishId: string) {
  const key = pendingKeys[publishId]
  if (!key) return
  try {
    await navigator.clipboard.writeText(key)
    delete pendingKeys[publishId]
    freshKeys[publishId] = false
    message.ok = true
    message.text = t('publishesPage.keyCopied')
  } catch {
    message.ok = false
    message.text = t('publishesPage.copyFailed')
  }
}

// ── Page link ────────────────────────────────────────────────────────

function pageUrl(p: PublishItem): string {
  return withBase(`/p/${p.publish_id}`)
}

function openPage(p: PublishItem) {
  void router.push(`/p/${p.publish_id}`)
}

async function copyPageLink(p: PublishItem) {
  try {
    await navigator.clipboard.writeText(pageUrl(p))
    message.ok = true
    message.text = t('publishesPage.linkCopied')
  } catch {
    message.ok = false
    message.text = t('publishesPage.copyFailed')
  }
}

// ── Toggles / delete ─────────────────────────────────────────────────

async function togglePage(p: PublishItem, e: Event) {
  const on = (e.target as HTMLInputElement).checked
  busy.value = true
  try {
    await publishApi.update(p.publish_id, { page_enabled: on })
    p.page_enabled = on
  } catch (err) {
    message.ok = false
    message.text = err instanceof Error ? err.message : t('publishesPage.updateFailed')
    ;(e.target as HTMLInputElement).checked = !on
  } finally {
    busy.value = false
  }
}

async function toggleApi(p: PublishItem, e: Event) {
  const on = (e.target as HTMLInputElement).checked
  busy.value = true
  try {
    await publishApi.update(p.publish_id, { api_enabled: on })
    p.api_enabled = on
  } catch (err) {
    message.ok = false
    message.text = err instanceof Error ? err.message : t('publishesPage.updateFailed')
    ;(e.target as HTMLInputElement).checked = !on
  } finally {
    busy.value = false
  }
}

async function remove(p: PublishItem) {
  if (!window.confirm(t('publishesPage.deleteConfirm'))) return
  busy.value = true
  try {
    await publishApi.remove(p.publish_id)
    publishes.value = publishes.value.filter(x => x.publish_id !== p.publish_id)
    delete keysByPublish[p.publish_id]
  } catch (e) {
    message.ok = false
    message.text = e instanceof Error ? e.message : t('common.deleteFailed')
  } finally {
    busy.value = false
  }
}

// ── Labels ───────────────────────────────────────────────────────────

function agentLabel(slug: string): string {
  const a = scopedAgents.value.find(x => x.slug === slug)
  return a ? a.label || slug : slug
}

function projectLabel(id: string): string {
  return projects.value.find(p => p.project_id === id)?.display_name ?? id
}

function modelLabel(id: string): string {
  return agentStore.modelConfigs.find(c => c.config_id === id)?.model_id ?? id
}

function providerLabel(provider: string): string {
  switch (provider) {
    case 'anthropic': return 'Anthropic'
    case 'openai': return 'OpenAI'
    case 'azure_openai': return 'Azure OpenAI'
    case 'google_gemini': return 'Gemini'
    default: return provider
  }
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString()
}

onMounted(() => {
  void load()
  // Watch the create modal: preload agents once a project is picked.
})
</script>

<style scoped>
.publishes-page {
  max-width: 900px;
  margin: 0 auto;
  padding: 32px 16px 48px;
}

/* ── Header ─────────────────────────────────────────────────────────── */
.publishes-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.publishes-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.publishes-header-icon {
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

.publishes-title {
  margin: 0;
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.02em;
}

.publishes-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
}

.publishes-new {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

/* ── Messages ───────────────────────────────────────────────────────── */
.publishes-message {
  padding: 9px 13px;
  border-radius: 8px;
  font-size: 12.5px;
  margin-bottom: 14px;
}
.publishes-message.success { background: rgba(74, 222, 128, 0.12); color: #4ade80; }
.publishes-message.error { background: rgba(248, 113, 113, 0.12); color: #f87171; }

/* ── States ─────────────────────────────────────────────────────────── */
.publishes-state {
  padding: 48px 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 13px;
}

.publishes-spin { animation: publishes-spin 0.9s linear infinite; }

@keyframes publishes-spin { to { transform: rotate(360deg); } }

.publishes-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 56px 0;
  text-align: center;
  color: var(--text-muted);
}

.publishes-empty-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: var(--accent-dim);
  color: var(--accent);
}

.publishes-empty-title {
  margin: 0;
  font-size: 13.5px;
  max-width: 360px;
  line-height: 1.6;
}

/* ── Cards ──────────────────────────────────────────────────────────── */
.publishes-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.publish-card {
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.32);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.publish-card:hover {
  border-color: var(--border-strong);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4), 0 0 20px rgba(107, 159, 255, 0.04);
}

.publish-card-main {
  padding: 16px 18px;
}

.publish-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.publish-card-heading {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.publish-card-title {
  font-size: 16px;
  font-weight: 650;
  letter-spacing: -0.01em;
  overflow-wrap: anywhere;
}

.publish-card-slug {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono, 'SF Mono', 'Cascadia Code', monospace);
}

.publish-card-status {
  flex: none;
  display: flex;
  gap: 6px;
}

.publish-status-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-weight: 500;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.publish-status-chip.on { background: rgba(74, 222, 128, 0.12); color: #4ade80; }
.publish-status-chip.off { background: rgba(127, 127, 127, 0.12); color: var(--text-muted); }

.publish-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.publish-status-chip.on .publish-status-dot { box-shadow: 0 0 6px currentColor; }

.publish-card-desc {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.publish-card-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.publish-meta-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 9px;
}

/* ── Keys ───────────────────────────────────────────────────────────── */
.publish-keys {
  margin-top: 14px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.publish-keys-empty {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-muted);
}

.publish-key-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.publish-key-icon {
  display: flex;
  align-items: center;
  color: var(--text-muted);
}

.publish-key-hint {
  font-size: 11px;
  color: var(--text-secondary);
  font-family: var(--font-mono, 'SF Mono', 'Cascadia Code', monospace);
  background: rgba(255, 255, 255, 0.04);
  border-radius: 4px;
  padding: 1px 6px;
}

.publish-key-name {
  font-size: 11px;
  color: var(--text-muted);
}

.publish-key-status {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 999px;
}
.publish-key-status.on { background: rgba(74, 222, 128, 0.14); color: #4ade80; }
.publish-key-status.off { background: rgba(127, 127, 127, 0.14); color: var(--text-muted); }

.publish-key-add {
  display: flex;
  align-items: center;
  gap: 8px;
}

.publish-key-add .btn-sm {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.publish-key-name-input {
  width: 190px;
  font-size: 12px;
}

.publish-fresh-key {
  font-size: 11px;
  color: #4ade80;
}

.publish-copy-once {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 9px 11px;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.25);
  border-radius: 8px;
  color: #fbbf24;
}

.publish-raw-key {
  flex: 1;
  font-size: 12px;
  font-family: var(--font-mono, 'SF Mono', 'Cascadia Code', monospace);
  color: #fbbf24;
  word-break: break-all;
}

.publish-copy-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
}

/* ── Card actions ───────────────────────────────────────────────────── */
.publish-card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 11px 18px;
  border-top: 1px solid var(--border);
  background: rgba(127, 127, 127, 0.05);
}

.publish-card-actions-left,
.publish-card-actions-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.publish-card-actions .btn-sm {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-right: 0;
}

/* ── Toggle switch ──────────────────────────────────────────────────── */
.publish-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.publish-toggle-label {
  font-size: 11.5px;
  color: var(--text-secondary);
}

.publish-switch {
  position: relative;
  display: inline-flex;
  width: 34px;
  height: 19px;
  border-radius: 999px;
  background: rgba(127, 127, 127, 0.35);
  transition: background 0.2s;
  flex: none;
}
.publish-switch.on { background: var(--accent-gradient); }

.publish-switch input {
  position: absolute;
  inset: 0;
  opacity: 0;
  margin: 0;
  cursor: pointer;
}
.publish-switch input:disabled { cursor: not-allowed; }

.publish-switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.2s;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}
.publish-switch.on .publish-switch-knob { transform: translateX(15px); }

/* ── Create modal ───────────────────────────────────────────────────── */
.publish-create-modal {
  max-width: 480px;
}

.publish-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 8px 4px 4px;
  overflow-y: auto;
}

.publish-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.publish-field-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.publish-form-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

@media (max-width: 640px) {
  .publishes-page { padding: 20px 10px 32px; }
  .publish-card-actions { flex-direction: column; align-items: stretch; }
  .publish-card-actions-left,
  .publish-card-actions-right { justify-content: space-between; }
}
</style>
