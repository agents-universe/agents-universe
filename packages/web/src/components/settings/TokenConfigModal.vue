<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-dialog token-config-modal">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon">
              <KeyRound :size="18" />
            </span>
            <h2 class="modal-title">密钥与集成配置</h2>
          </div>
          <button class="modal-close" @click="emit('close')">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">配置 LLM 和外部服务的 API 密钥，数据加密存储</p>

        <div class="token-tabs">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            class="token-tab"
            :class="{ active: activeTab === tab.id }"
            @click="activeTab = tab.id"
          >{{ tab.label }}</button>
        </div>

        <!-- LLM Models -->
        <div v-if="activeTab === 'llm'" class="token-section">
          <p class="token-section-hint">你配置的模型仅自己可见，其他用户无法查看或使用</p>
          <!-- System default (read-only) -->
          <div v-if="systemDefault" class="token-service-row system-default-row">
            <div class="token-service-header">
              <Zap :size="16" class="token-service-icon" />
              <div class="token-service-info">
                <span class="token-service-label">{{ systemDefault.model_id }} Default</span>
                <span class="token-service-sub">系统默认模型（OpenAI 兼容）</span>
              </div>
              <span class="token-hint-badge system-badge">系统</span>
            </div>
          </div>

          <!-- User model configs -->
          <div
            v-for="cfg in userConfigs"
            :key="cfg.config_id"
            class="token-service-row"
          >
            <div class="token-service-header">
              <component :is="providerIcon(cfg.provider)" :size="16" class="token-service-icon" />
              <div class="token-service-info">
                <span class="token-service-label">{{ cfg.model_id }}</span>
                <span class="token-service-sub">{{ providerLabel(cfg.provider) }}</span>
              </div>
              <span v-if="cfg.key_hint" class="token-hint-badge">{{ cfg.key_hint }}</span>
              <span v-if="cfg.complexity_tier" class="token-hint-badge tier-badge" :class="'tier-' + cfg.complexity_tier">{{ tierLabel(cfg.complexity_tier) }}</span>
              <span v-if="cfg.context_window" class="token-hint-badge">{{ fmtWindow(cfg.context_window) }}</span>
            </div>

            <div v-if="editForms[cfg.config_id]" class="token-edit-block">
              <input
                v-model="editForms[cfg.config_id].model_id"
                class="input"
                placeholder="模型 ID"
              />
              <div class="token-url-row">
                <input
                  v-model="editForms[cfg.config_id].api_key"
                  class="input token-url-input"
                  type="password"
                  :placeholder="cfg.key_hint ? '已配置，留空保持不变' : 'API Key'"
                />
              </div>
              <div class="token-url-row">
                <input
                  v-model="editForms[cfg.config_id].base_url"
                  class="input token-url-input"
                  :placeholder="editForms[cfg.config_id].url_mode === 'full_url' ? '完整 API 地址' : 'Base URL（可选）'"
                />
                <select v-model="editForms[cfg.config_id].url_mode" class="input token-url-mode-select">
                  <option value="base_url">Base URL</option>
                  <option value="full_url">完整地址</option>
                </select>
              </div>
              <div class="token-url-row">
                <select v-model="editForms[cfg.config_id].complexity_tier" class="input token-tier-select">
                  <option :value="null">未指定</option>
                  <option value="low">轻量 (low)</option>
                  <option value="mid">标准 (mid)</option>
                  <option value="high">旗舰 (high)</option>
                </select>
                <span class="token-url-hint">自动路由档位</span>
              </div>
              <div class="token-url-row">
                <input
                  v-model="editForms[cfg.config_id].context_window"
                  class="input token-url-input"
                  type="number"
                  min="1"
                  step="1000"
                  placeholder="留空自动匹配"
                />
                <span class="token-url-hint">上下文窗口（默认 {{ fmtWindow(cfg.default_context_window) }}）</span>
              </div>
              <div class="token-row-actions">
                <button class="btn-sm" @click="saveConfig(cfg.config_id)" :disabled="saving === cfg.config_id">
                  {{ saving === cfg.config_id ? '保存中…' : '保存' }}
                </button>
                <button class="btn-sm secondary" @click="testConfig(cfg.config_id)" :disabled="testing === cfg.config_id">
                  {{ testing === cfg.config_id ? '测试中…' : '测试' }}
                </button>
                <button class="btn-sm danger" @click="deleteConfig(cfg.config_id)" :disabled="saving === cfg.config_id">
                  删除
                </button>
                <span v-if="messages[cfg.config_id]" class="token-inline-msg" :class="messages[cfg.config_id].ok ? 'success' : 'error'">
                  {{ messages[cfg.config_id].ok ? '✓' : '✗' }} {{ messages[cfg.config_id].text }}
                </span>
              </div>
            </div>
          </div>

          <!-- Empty state -->
          <div v-if="!userConfigs.length && !systemDefault" class="token-empty-state">
            未配置任何模型，点击下方按钮添加
          </div>

          <!-- Add model button -->
          <div class="token-add-section">
            <div v-if="showAddForm" class="token-add-form">
              <select v-model="newProvider" class="input">
                <option value="" disabled>选择厂商类型</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="azure_openai">Azure OpenAI</option>
                <option value="google_gemini">Google Gemini</option>
              </select>
              <input v-model="newModelId" class="input" placeholder="模型 ID（如 gpt-4o）" />
              <input v-model="newApiKey" class="input" type="password" placeholder="API Key" />
              <div class="token-url-row">
                <input v-model="newBaseUrl" class="input token-url-input" :placeholder="newUrlMode === 'full_url' ? '完整 API 地址' : 'Base URL（可选）'" />
                <select v-model="newUrlMode" class="input token-url-mode-select">
                  <option value="base_url">Base URL</option>
                  <option value="full_url">完整地址</option>
                </select>
              </div>
              <div class="token-url-row">
                <select v-model="newTier" class="input token-tier-select" @change="newTierDirty = true">
                  <option :value="null">未指定</option>
                  <option value="low">轻量 (low)</option>
                  <option value="mid">标准 (mid)</option>
                  <option value="high">旗舰 (high)</option>
                </select>
                <span class="token-url-hint">自动路由档位（随模型 ID 自动推断）</span>
              </div>
              <div class="token-url-row">
                <input
                  v-model="newContextWindow"
                  class="input token-url-input"
                  type="number"
                  min="1"
                  step="1000"
                  placeholder="留空自动匹配"
                  @change="newContextWindowDirty = true"
                />
                <span class="token-url-hint">上下文窗口（随模型 ID 自动匹配）</span>
              </div>
              <div class="token-row-actions">
                <button class="btn-sm" @click="addConfig" :disabled="!newProvider || !newModelId || saving === '__new__'">
                  {{ saving === '__new__' ? '添加中…' : '添加' }}
                </button>
                <button class="btn-sm secondary" @click="testNewConfig" :disabled="!newProvider || !newModelId || !newApiKey || testing === '__new__'">
                  {{ testing === '__new__' ? '测试中…' : '测试' }}
                </button>
                <button class="btn-sm secondary" @click="showAddForm = false">取消</button>
                <span v-if="messages['__new__']" class="token-inline-msg" :class="messages['__new__'].ok ? 'success' : 'error'">
                  {{ messages['__new__'].ok ? '✓' : '✗' }} {{ messages['__new__'].text }}
                </span>
              </div>
            </div>
            <button v-else class="btn-sm add-model-btn" @click="showAddForm = true">
              + 添加模型
            </button>
          </div>
        </div>

        <!-- Integrations -->
        <div v-else-if="activeTab === 'integrations'" class="token-section">
          <p class="token-section-hint">「用户密钥」跨项目；「项目密钥」由智能体保存，仅当前项目生效</p>
          <!-- User-configured integration rows -->
          <div
            v-for="svc in configuredIntegrations"
            :key="svc.key"
            class="token-service-row"
          >
            <div class="token-service-header">
              <component :is="svc.icon" :size="16" class="token-service-icon" />
              <div class="token-service-info">
                <span class="token-service-label">{{ svc.label }}</span>
                <span class="token-service-sub">{{ svc.description }}</span>
              </div>
              <span v-if="savedKeys[svc.key] || projectSecretByKey[svc.key]?.key_hint" class="token-hint-badge">{{ savedKeys[svc.key] || projectSecretByKey[svc.key]?.key_hint }}</span>
              <span v-if="integrationScopes[svc.key] === 'user' || integrationScopes[svc.key] === 'both'" class="scope-badge user-scope">用户密钥</span>
              <span v-if="integrationScopes[svc.key] === 'project' || integrationScopes[svc.key] === 'both'" class="scope-badge project-scope">项目密钥</span>
            </div>

            <div class="token-edit-block">
              <input
                v-if="integrationScopes[svc.key] !== 'project'"
                v-model="formValues[svc.key + ':base_url']"
                class="input"
                type="text"
                :placeholder="integrationDefaults[svc.key] ? `系统默认: ${integrationDefaults[svc.key]}` : 'Base URL'"
              />
              <input
                v-model="formValues[svc.key]"
                class="input"
                :type="svc.sensitive === false ? 'text' : 'password'"
                :placeholder="savedKeys[svc.key] || projectSecretByKey[svc.key] ? '已配置，留空保持不变' : svc.placeholder"
              />
              <div v-if="svc.extraFields" v-for="field in svc.extraFields" :key="field.key" class="token-url-row">
                <input
                  v-model="formValues[field.key]"
                  class="input token-url-input"
                  :type="field.sensitive === false ? 'text' : 'password'"
                  :placeholder="savedKeys[field.key] || projectSecretByKey[field.key] ? '已配置' : field.placeholder"
                />
                <span class="token-url-hint">{{ field.label }}</span>
              </div>
              <div class="token-row-actions">
                <button class="btn-sm" @click="saveIntegration(svc)" :disabled="saving === svc.key">
                  {{ saving === svc.key ? '保存中…' : '保存' }}
                </button>
                <button class="btn-sm secondary" @click="testService(svc.key)" :disabled="testing === svc.key">
                  {{ testing === svc.key ? '测试中…' : '测试' }}
                </button>
                <button class="btn-sm danger" @click="deleteIntegration(svc.key)" :disabled="saving === svc.key">
                  删除
                </button>
                <span v-if="messages[svc.key]" class="token-inline-msg" :class="messages[svc.key].ok ? 'success' : 'error'">
                  {{ messages[svc.key].ok ? '✓' : '✗' }} {{ messages[svc.key].text }}
                </span>
              </div>
            </div>
          </div>

          <!-- Empty state -->
          <div v-if="!configuredIntegrations.length" class="token-empty-state">
            未配置任何外部集成，点击下方按钮添加
          </div>

          <!-- Add integration section -->
          <div class="token-add-section">
            <div v-if="showAddIntegrationForm" class="token-add-form">
              <select v-model="newIntegrationKey" class="input">
                <option value="" disabled>选择集成类型</option>
                <option
                  v-for="svc in availableToAdd"
                  :key="svc.key"
                  :value="svc.key"
                >{{ svc.label }}</option>
              </select>
              <input
                v-if="newIntegrationKey"
                v-model="formValues[newIntegrationKey + ':base_url']"
                class="input"
                type="text"
                :placeholder="integrationDefaults[newIntegrationKey] ? `系统默认: ${integrationDefaults[newIntegrationKey]}` : 'Base URL'"
              />
              <input
                v-if="newIntegrationKey"
                v-model="formValues[newIntegrationKey]"
                class="input"
                type="password"
                :placeholder="newIntegrationService?.placeholder || 'Token'"
              />
              <template v-if="newIntegrationKey && newIntegrationService?.extraFields">
                <div v-for="field in newIntegrationService.extraFields" :key="field.key" class="token-url-row">
                  <input
                    v-model="formValues[field.key]"
                    class="input token-url-input"
                    :type="field.sensitive === false ? 'text' : 'password'"
                    :placeholder="field.placeholder"
                  />
                  <span class="token-url-hint">{{ field.label }}</span>
                </div>
              </template>
              <div class="token-row-actions">
                <button class="btn-sm" @click="addIntegration" :disabled="!newIntegrationKey || !formValues[newIntegrationKey] || saving === '__new_integration__'">
                  {{ saving === '__new_integration__' ? '添加中…' : '添加' }}
                </button>
                <button class="btn-sm secondary" @click="testNewIntegration" :disabled="!newIntegrationKey || !formValues[newIntegrationKey] || testing === '__new_integration__'">
                  {{ testing === '__new_integration__' ? '测试中…' : '测试' }}
                </button>
                <button class="btn-sm secondary" @click="cancelAddIntegration">取消</button>
                <span v-if="messages['__new_integration__']" class="token-inline-msg" :class="messages['__new_integration__'].ok ? 'success' : 'error'">
                  {{ messages['__new_integration__'].ok ? '✓' : '✗' }} {{ messages['__new_integration__'].text }}
                </span>
              </div>
            </div>
            <button v-else-if="availableToAdd.length" class="btn-sm add-model-btn" @click="showAddIntegrationForm = true">
              + 添加集成
            </button>
          </div>

          <!-- MCP servers -->
          <div class="token-section mcp-section">
            <div class="token-section-title">
              <Zap :size="14" class="token-section-icon" />
              <span>MCP 服务器</span>
            </div>
            <p class="token-section-hint">
              全局服务器跨项目生效（本页维护）；项目服务器由项目知识库
              <code>integrations/mcp-servers.md</code> 定义、随对话自动同步；同 slug 项目条目覆盖全局。智能体声明
              <code>mcp</code> 或 <code>mcp:&lt;slug&gt;</code> 后自动接入
            </p>

            <!-- Global server add/edit form -->
            <div v-if="showMcpForm" class="token-edit-block mcp-edit-block">
              <div class="mcp-form-grid">
                <input v-model="mcpForm.name" class="input" type="text" placeholder="名称（如 GitHub Copilot）" />
                <input v-model="mcpForm.slug" class="input" type="text" placeholder="slug（唯一标识，如 github-copilot）" />
                <input v-model="mcpForm.url" class="input" type="text" placeholder="URL（https://.../mcp）" />
                <select v-model="mcpForm.transport" class="input">
                  <option value="auto">auto（streamable_http → SSE 回退）</option>
                  <option value="streamable_http">streamable_http</option>
                  <option value="sse">sse</option>
                </select>
                <select v-model="mcpForm.auth_type" class="input">
                  <option value="none">无认证</option>
                  <option value="bearer">Bearer Token</option>
                  <option value="header">自定义 Header</option>
                </select>
                <input
                  v-if="mcpForm.auth_type !== 'none'"
                  v-model="mcpForm.secret_ref"
                  class="input"
                  type="text"
                  placeholder="密钥键（如 mcp:github-copilot，存于上方用户密钥）"
                />
                <input
                  v-if="mcpForm.auth_type === 'header'"
                  v-model="mcpForm.auth_header_name"
                  class="input"
                  type="text"
                  placeholder="Header 名（如 X-API-Key）"
                />
                <label class="mcp-toggle">
                  <input v-model="mcpForm.enabled" type="checkbox" />
                  启用
                </label>
              </div>
              <div class="token-row-actions">
                <button class="btn-sm" @click="saveMcpServer" :disabled="mcpSaving || !mcpForm.slug.trim()">
                  {{ mcpSaving ? '保存中…' : '保存' }}
                </button>
                <button class="btn-sm secondary" @click="cancelMcpForm">取消</button>
              </div>
            </div>
            <button v-else class="btn-sm add-model-btn" @click="openMcpForm()">
              + 添加全局服务器
            </button>

            <div v-if="mcpLoading" class="token-empty-state">加载中...</div>
            <div v-else-if="!mcpServers.length" class="token-empty-state">未配置 MCP 服务器</div>
            <ul v-else class="mcp-list">
              <li v-for="s in mcpServers" :key="s.server_id || s.slug" class="mcp-item">
                <div class="mcp-item-main">
                  <span class="mcp-name">{{ s.name }}</span>
                  <span class="mcp-slug">{{ s.slug }}</span>
                  <span class="scope-badge" :class="s.scope === 'global' ? 'user-scope' : 'project-scope'">
                    {{ s.scope === 'global' ? '全局' : '项目' }}
                  </span>
                </div>
                <div class="mcp-item-meta">
                  <span v-if="s.url" class="mcp-url" :title="s.url">{{ s.url }}</span>
                  <span class="mcp-status" :class="s.enabled ? 'on' : 'off'">{{ s.enabled ? '已启用' : '已停用' }}</span>
                  <span
                    v-if="s.auth_type && s.auth_type !== 'none'"
                    class="mcp-auth"
                    :class="s.has_secret ? 'ok' : 'warn'"
                    :title="s.secret_ref ? `密钥键: ${s.secret_ref}` : ''"
                  >
                    {{ s.has_secret ? '密钥已配置' : '密钥未配置' }}
                  </span>
                  <template v-if="s.scope === 'global'">
                    <button class="btn-sm" @click="openMcpForm(s)">编辑</button>
                    <button class="btn-sm secondary" @click="testMcpServer(s)" :disabled="mcpTesting === s.server_id">
                      {{ mcpTesting === s.server_id ? '测试中…' : '测试' }}
                    </button>
                    <button class="btn-sm secondary" @click="deleteMcpServer(s)">删除</button>
                  </template>
                </div>
              </li>
            </ul>
            <span v-if="mcpMessage.text" class="token-inline-msg mcp-inline-msg" :class="mcpMessage.ok ? 'success' : 'error'">
              {{ mcpMessage.ok ? '✓' : '✗' }} {{ mcpMessage.text }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { KeyRound, X, Cpu, Zap, Cloud, GitBranch, BookOpen, Globe, Sparkles } from 'lucide-vue-next'
import { apiFetch } from '@/api/client'
import { modelConfigsApi } from '@/api/modelConfigs'
import { createProjectSecret, deleteProjectSecret, listProjectSecrets, updateProjectSecret } from '@/api/projectSecrets'
import { useAgentStore } from '@/stores/agent'
import { useProjectStore } from '@/stores/project'
import { inferTier } from '@/utils/modelTier'
import { inferContextWindow } from '@/utils/contextWindow'
import type { ProjectSecret } from '@/types'

const emit = defineEmits<{ close: [] }>()
const agentStore = useAgentStore()
const projectStore = useProjectStore()

const activeTab = ref<'llm' | 'integrations'>('llm')
const tabs = [
  { id: 'llm' as const, label: 'LLM 模型' },
  { id: 'integrations' as const, label: '外部集成' },
]

// ── LLM Model Configs ──────────────────────────────────────────────

const systemDefault = computed(() => agentStore.modelConfigs.find(c => c.is_system) ?? null)
const userConfigs = computed(() => agentStore.modelConfigs.filter(c => !c.is_system))

const editForms = reactive<Record<string, { model_id: string; api_key: string; base_url: string; url_mode: string; complexity_tier: 'low' | 'mid' | 'high' | null; context_window: string }>>({})

function initEditForms() {
  for (const cfg of userConfigs.value) {
    if (!editForms[cfg.config_id]) {
      editForms[cfg.config_id] = {
        model_id: cfg.model_id,
        api_key: '',
        base_url: cfg.base_url ?? '',
        url_mode: cfg.url_mode ?? 'base_url',
        complexity_tier: cfg.complexity_tier ?? null,
        context_window: cfg.context_window ? String(cfg.context_window) : '',
      }
    }
  }
}

watch(userConfigs, initEditForms, { immediate: true })

function providerIcon(provider: string) {
  switch (provider) {
    case 'anthropic': return Cpu
    case 'openai': return Zap
    case 'azure_openai': return Cloud
    case 'google_gemini': return Sparkles
    default: return Zap
  }
}

function providerLabel(provider: string) {
  switch (provider) {
    case 'anthropic': return 'Anthropic (Claude)'
    case 'openai': return 'OpenAI'
    case 'azure_openai': return 'Azure OpenAI'
    case 'google_gemini': return 'Google Gemini'
    default: return provider
  }
}

function tierLabel(tier: 'low' | 'mid' | 'high'): string {
  switch (tier) {
    case 'low': return '轻量'
    case 'mid': return '标准'
    case 'high': return '旗舰'
  }
}

/** Compact window display: 128000 → "128k", 1048576 → "1049k". */
function fmtWindow(n: number | null | undefined): string {
  return n ? `${Math.round(n / 1000)}k` : ''
}

const saving = ref<string | null>(null)
const testing = ref<string | null>(null)
const messages = reactive<Record<string, { ok: boolean; text: string }>>({})

async function saveConfig(configId: string) {
  saving.value = configId
  delete messages[configId]
  try {
    const form = editForms[configId]
    const body: {
      model_id?: string; api_key?: string; base_url?: string; url_mode?: string
      complexity_tier?: 'low' | 'mid' | 'high' | null
      context_window?: number | null
    } = {}
    if (form.model_id) body.model_id = form.model_id
    if (form.api_key) body.api_key = form.api_key
    if (form.base_url !== undefined) body.base_url = form.base_url
    if (form.url_mode) body.url_mode = form.url_mode
    body.complexity_tier = form.complexity_tier
    const windowNum = Number(form.context_window)
    body.context_window = form.context_window && Number.isFinite(windowNum) ? windowNum : null
    await agentStore.updateModelConfig(configId, body)
    form.api_key = ''
    messages[configId] = { ok: true, text: '已保存' }
  } catch (e) {
    messages[configId] = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  } finally {
    saving.value = null
  }
}

async function testConfig(configId: string) {
  testing.value = configId
  delete messages[configId]
  try {
    const res = await modelConfigsApi.test(configId)
    messages[configId] = res.ok
      ? { ok: true, text: '连接成功' }
      : { ok: false, text: res.error || '连接失败' }
  } catch (e) {
    messages[configId] = { ok: false, text: e instanceof Error ? e.message : '测试失败' }
  } finally {
    testing.value = null
  }
}

async function deleteConfig(configId: string) {
  saving.value = configId
  delete messages[configId]
  try {
    await agentStore.removeModelConfig(configId)
    delete editForms[configId]
  } catch (e) {
    messages[configId] = { ok: false, text: e instanceof Error ? e.message : '删除失败' }
  } finally {
    saving.value = null
  }
}

// ── Add New Model ──────────────────────────────────────────────────

const showAddForm = ref(false)
const newProvider = ref('')
const newModelId = ref('')
const newApiKey = ref('')
const newBaseUrl = ref('')
const newUrlMode = ref('base_url')
const newTier = ref<'low' | 'mid' | 'high' | null>(null)
// User-picked tier wins over inference while the model id keeps changing.
const newTierDirty = ref(false)

const newContextWindow = ref('')
// User-typed window wins over inference while the model id keeps changing.
const newContextWindowDirty = ref(false)

watch([newProvider, newModelId], ([provider, modelId]) => {
  if (newTierDirty.value) return
  newTier.value = inferTier(provider, modelId)
})

watch([newProvider, newModelId], ([provider, modelId]) => {
  if (newContextWindowDirty.value || !provider || !modelId) return
  newContextWindow.value = String(inferContextWindow(provider, modelId))
})

async function addConfig() {
  saving.value = '__new__'
  delete messages['__new__']
  try {
    const created = await agentStore.addModelConfig({
      provider: newProvider.value,
      model_id: newModelId.value,
      api_key: newApiKey.value || undefined,
      base_url: newBaseUrl.value || undefined,
      url_mode: newUrlMode.value,
      complexity_tier: newTier.value,
      context_window: newContextWindow.value ? Number(newContextWindow.value) : null,
    })
    editForms[created.config_id] = {
      model_id: created.model_id,
      api_key: '',
      base_url: created.base_url ?? '',
      url_mode: created.url_mode ?? 'base_url',
      complexity_tier: created.complexity_tier ?? null,
      context_window: created.context_window ? String(created.context_window) : '',
    }
    newProvider.value = ''
    newModelId.value = ''
    newApiKey.value = ''
    newBaseUrl.value = ''
    newUrlMode.value = 'base_url'
    newTier.value = null
    newTierDirty.value = false
    newContextWindow.value = ''
    newContextWindowDirty.value = false
    showAddForm.value = false
    messages['__new__'] = { ok: true, text: '已添加' }
  } catch (e) {
    messages['__new__'] = { ok: false, text: e instanceof Error ? e.message : '添加失败' }
  } finally {
    saving.value = null
  }
}

async function testNewConfig() {
  testing.value = '__new__'
  delete messages['__new__']
  try {
    const res = await modelConfigsApi.testConnection({
      provider: newProvider.value,
      model_id: newModelId.value,
      api_key: newApiKey.value,
      base_url: newBaseUrl.value || undefined,
      url_mode: newUrlMode.value,
    })
    messages['__new__'] = res.ok
      ? { ok: true, text: '连接成功' }
      : { ok: false, text: res.error || '连接失败' }
  } catch (e) {
    messages['__new__'] = { ok: false, text: e instanceof Error ? e.message : '测试失败' }
  } finally {
    testing.value = null
  }
}

// ── Integrations ───────────────────────────────────────────────────

interface ExtraField { key: string; label: string; placeholder: string; sensitive?: boolean }
interface IntegrationService { key: string; label: string; description: string; icon: any; placeholder: string; sensitive?: boolean; extraFields?: ExtraField[] }

const integrationServices: IntegrationService[] = [
  { key: 'git', label: 'GitHub / Git', description: 'Personal Access Token', icon: GitBranch, placeholder: 'ghp_...' },
  { key: 'jira', label: 'Jira', description: 'Atlassian Token', icon: BookOpen, placeholder: 'API Token', extraFields: [{ key: 'jira:email', label: 'Email', placeholder: 'you@example.com', sensitive: false }] },
  { key: 'confluence', label: 'Confluence', description: '与 Jira 共用 Atlassian Token', icon: Globe, placeholder: 'API Token' },
]

/** System-configured default base URLs from GET /api/integrations/defaults */
const integrationDefaults = reactive<Record<string, string>>({})

/** Saved token hints + base_urls keyed by service_key */
const savedKeys = reactive<Record<string, string>>({})
const savedBaseUrls = reactive<Record<string, string | null>>({})

/**
 * Project-scoped secrets (agent-configured) by exact service_key, e.g.
 * { secret_id, key_hint } for "git". The integrations tab shows these too.
 */
const projectSecretByKey = reactive<Record<string, { secret_id: string; key_hint: string | null }>>({})

/** Where each integration currently lives: 'user' | 'project' | 'both'. */
const integrationScopes = reactive<Record<string, 'user' | 'project' | 'both'>>({})

interface McpServerInfo {
  server_id: string
  slug: string
  name: string
  url: string | null
  transport: string
  enabled: boolean
  auth_type: string
  secret_ref: string | null
  has_secret: boolean
  tool_allowlist: string[]
  tool_denylist: string[]
  scope: 'global' | 'project'
}
const mcpServers = ref<McpServerInfo[]>([])
const mcpLoading = ref(false)

// Global MCP server add/edit form (project servers are file-managed).
const showMcpForm = ref(false)
const mcpSaving = ref(false)
const mcpTesting = ref<string | null>(null)
const mcpEditId = ref<string | null>(null)
const mcpMessage = reactive({ ok: true, text: '' })
const mcpForm = reactive({
  name: '',
  slug: '',
  url: '',
  transport: 'auto',
  auth_type: 'none',
  secret_ref: '',
  auth_header_name: '',
  enabled: true,
})

function openMcpForm(server?: McpServerInfo) {
  mcpEditId.value = server?.server_id ?? null
  mcpForm.name = server?.name ?? ''
  mcpForm.slug = server?.slug ?? ''
  mcpForm.url = server?.url ?? ''
  mcpForm.transport = server?.transport ?? 'auto'
  mcpForm.auth_type = server?.auth_type ?? 'none'
  mcpForm.secret_ref = server?.secret_ref ?? ''
  mcpForm.auth_header_name = ''
  mcpForm.enabled = server?.enabled ?? true
  mcpMessage.text = ''
  showMcpForm.value = true
}

function cancelMcpForm() {
  showMcpForm.value = false
}

async function saveMcpServer() {
  mcpSaving.value = true
  mcpMessage.ok = true
  mcpMessage.text = ''
  try {
    const body = {
      slug: mcpForm.slug.trim(),
      name: mcpForm.name.trim() || null,
      url: mcpForm.url.trim() || null,
      transport: mcpForm.transport,
      auth_type: mcpForm.auth_type,
      secret_ref: mcpForm.secret_ref.trim() || null,
      auth_header_name: mcpForm.auth_header_name.trim() || null,
      enabled: mcpForm.enabled,
    }
    if (mcpEditId.value) {
      await apiFetch(`/api/mcp/servers/${mcpEditId.value}`, { method: 'PUT', body: JSON.stringify(body) })
    } else {
      await apiFetch('/api/mcp/servers', { method: 'POST', body: JSON.stringify(body) })
    }
    mcpMessage.ok = true
    mcpMessage.text = '已保存'
    showMcpForm.value = false
    await loadMcpServers()
  } catch (e) {
    mcpMessage.ok = false
    mcpMessage.text = e instanceof Error ? e.message : '保存失败'
  } finally {
    mcpSaving.value = false
  }
}

async function testMcpServer(server: McpServerInfo) {
  mcpTesting.value = server.server_id
  mcpMessage.ok = true
  mcpMessage.text = ''
  try {
    const res = await apiFetch<{ ok: boolean; error?: string; tools?: number }>(
      `/api/mcp/servers/${server.server_id}/test`,
      { method: 'POST' }
    )
    mcpMessage.ok = res.ok
    mcpMessage.text = res.ok ? `连接成功，发现 ${res.tools ?? 0} 个工具` : (res.error || '连接失败')
  } catch (e) {
    mcpMessage.ok = false
    mcpMessage.text = e instanceof Error ? e.message : '测试失败'
  } finally {
    mcpTesting.value = null
  }
}

async function deleteMcpServer(server: McpServerInfo) {
  if (!window.confirm(`删除 MCP 服务器「${server.name}」？`)) return
  mcpMessage.ok = true
  mcpMessage.text = ''
  try {
    await apiFetch(`/api/mcp/servers/${server.server_id}`, { method: 'DELETE' })
    mcpMessage.ok = true
    mcpMessage.text = '已删除'
    await loadMcpServers()
  } catch (e) {
    mcpMessage.ok = false
    mcpMessage.text = e instanceof Error ? e.message : '删除失败'
  }
}

/** Services the user has configured (user_tokens and/or project_secrets) */
const configuredIntegrationKeys = ref<Set<string>>(new Set())

const configuredIntegrations = computed(() =>
  integrationServices.filter(s => configuredIntegrationKeys.value.has(s.key))
)

const availableToAdd = computed(() =>
  integrationServices.filter(s => !configuredIntegrationKeys.value.has(s.key))
)

const formValues = reactive<Record<string, string>>({})

async function loadIntegrationDefaults() {
  try {
    const data = await apiFetch<Record<string, string>>('/api/integrations/defaults')
    Object.assign(integrationDefaults, data)
  } catch { /* ignore */ }
}

async function loadIntegrationTokens() {
  try {
    const data = await apiFetch<Array<{ service_key: string; key_hint: string; base_url: string | null }>>('/api/tokens')
    // Project-scoped secrets (agent-configured integrations) — shown alongside user tokens.
    let projectSecrets: ProjectSecret[] = []
    const projectId = projectStore.currentProject?.project_id
    if (projectId) {
      try { projectSecrets = await listProjectSecrets(projectId) } catch { /* ignore */ }
    }
    const projectByKey = new Map<string, ProjectSecret>()
    for (const s of projectSecrets) {
      if (s.environment) continue  // env-scoped keys (kong:dev) are not integrations
      projectByKey.set(s.service_key, s)
    }

    const llmProviders = ['anthropic', 'openai', 'azure_openai', 'google_gemini']
    const configured = new Set<string>()
    for (const k of Object.keys(projectSecretByKey)) delete projectSecretByKey[k]
    for (const k of Object.keys(integrationScopes)) delete integrationScopes[k]
    // Drop stale hints from earlier sessions (e.g. token deleted elsewhere)
    // so scope detection reflects the current DB state.
    const freshKeys = new Set(data.map(t => t.service_key))
    for (const k of Object.keys(savedKeys)) {
      if (!freshKeys.has(k)) delete savedKeys[k]
    }
    for (const k of Object.keys(savedBaseUrls)) {
      if (!freshKeys.has(k)) delete savedBaseUrls[k]
    }
    for (const t of data) {
      if (llmProviders.includes(t.service_key.split(':')[0])) continue
      savedKeys[t.service_key] = t.key_hint
      savedBaseUrls[t.service_key] = t.base_url
      // Map sub-keys (e.g. "jira:email") back to their parent service
      const parentKey = t.service_key.split(':')[0]
      if (integrationServices.some(s => s.key === parentKey)) {
        configured.add(parentKey)
      }
    }
    // Project-scoped integrations: main key + extra fields map back to the service.
    for (const svc of integrationServices) {
      const main = projectByKey.get(svc.key)
      if (main) {
        configured.add(svc.key)
        projectSecretByKey[svc.key] = { secret_id: main.secret_id, key_hint: main.key_hint }
      }
      for (const field of svc.extraFields ?? []) {
        const pf = projectByKey.get(field.key)
        if (pf) projectSecretByKey[field.key] = { secret_id: pf.secret_id, key_hint: pf.key_hint }
      }
    }
    for (const key of configured) {
      const hasUser = savedKeys[key] !== undefined
      const hasProject = !!projectSecretByKey[key]
      integrationScopes[key] = hasUser && hasProject ? 'both' : hasUser ? 'user' : 'project'
    }
    configuredIntegrationKeys.value = configured
    // Pre-fill base_url form values from saved data
    for (const svc of integrationServices) {
      if (savedBaseUrls[svc.key]) {
        formValues[svc.key + ':base_url'] = savedBaseUrls[svc.key]!
      }
    }
  } catch { /* ignore */ }
}

async function loadMcpServers() {
  const projectId = projectStore.currentProject?.project_id
  if (!projectId) {
    mcpServers.value = []
    return
  }
  mcpLoading.value = true
  try {
    mcpServers.value = await apiFetch<McpServerInfo[]>(`/api/projects/${projectId}/mcp-servers`)
  } catch {
    mcpServers.value = []
  } finally {
    mcpLoading.value = false
  }
}

async function saveIntegration(svc: IntegrationService) {
  saving.value = svc.key
  delete messages[svc.key]
  try {
    const projectId = projectStore.currentProject?.project_id
    const scope = integrationScopes[svc.key]
    // user scope (user | both) writes user_tokens; project scope (project | both) writes project_secrets
    const toUser = scope !== 'project'
    const toProject = scope !== 'user' && !!projectId && !!projectSecretByKey[svc.key]
    const baseUrlVal = formValues[svc.key + ':base_url'] ?? ''
    // Snapshot the value first: the user branch clears the input below, and
    // the project branch must still see what was typed .
    const tokenVal = formValues[svc.key] ?? ''
    if (toUser) {
      // Save token value (or keep existing if empty)
      if (tokenVal) {
        const res = await apiFetch<{ key_hint: string }>(`/api/tokens/${svc.key}`, {
          method: 'PUT',
          body: JSON.stringify({ value: tokenVal, base_url: baseUrlVal || null }),
        })
        savedKeys[svc.key] = res.key_hint
        formValues[svc.key] = ''
      } else {
        // Update base_url only, keep existing token
        await apiFetch(`/api/tokens/${svc.key}`, {
          method: 'PUT',
          body: JSON.stringify({ base_url: baseUrlVal || null }),
        })
      }
      savedBaseUrls[svc.key] = baseUrlVal || null
    }
    // Project-scoped copy of the value, when the integration also lives there.
    // this used formValues[svc.key], which the user branch above
    // just cleared — on 'both' scope the project secret was overwritten with
    // an empty string. Use the snapshot taken before the user branch ran.
    if (toProject && tokenVal) {
      const res = await updateProjectSecret(projectId!, projectSecretByKey[svc.key].secret_id, {
        value: tokenVal,
      })
      projectSecretByKey[svc.key].key_hint = res.key_hint
      formValues[svc.key] = ''
    }
    // Save extra fields (e.g. jira:email) into the same scopes as the parent
    if (svc.extraFields) {
      for (const field of svc.extraFields) {
        if (!formValues[field.key]) continue
        if (toUser) {
          const res = await apiFetch<{ key_hint: string }>(`/api/tokens/${field.key}`, {
            method: 'PUT',
            body: JSON.stringify({ value: formValues[field.key] }),
          })
          savedKeys[field.key] = res.key_hint
        }
        if (toProject) {
          const existing = projectSecretByKey[field.key]
          if (existing) {
            const res = await updateProjectSecret(projectId!, existing.secret_id, { value: formValues[field.key] })
            existing.key_hint = res.key_hint
          } else {
            const res = await createProjectSecret(projectId!, { service_key: field.key, value: formValues[field.key] })
            projectSecretByKey[field.key] = { secret_id: res.secret_id, key_hint: res.key_hint }
          }
        }
        formValues[field.key] = ''
      }
    }
    messages[svc.key] = { ok: true, text: '已保存' }
  } catch (e) {
    messages[svc.key] = { ok: false, text: e instanceof Error ? e.message : '保存失败' }
  } finally {
    saving.value = null
  }
}

async function testService(key: string) {
  testing.value = key
  delete messages[key]
  try {
    const svc = integrationServices.find(s => s.key === key)
    const scope = integrationScopes[key]
    if (svc && formValues[svc.key]) {
      await saveIntegration(svc)
      if (messages[svc.key] && !messages[svc.key].ok) return
    } else if (scope !== 'project' && !savedKeys[key]) {
      messages[key] = { ok: false, text: '请先填写并保存 Token' }
      return
    }
    const projectId = projectStore.currentProject?.project_id
    const projectSecret = projectSecretByKey[key]
    let res: { ok: boolean; error?: string }
    if (scope === 'project' && projectSecret && projectId) {
      res = await apiFetch<{ ok: boolean; error?: string }>(
        `/api/projects/${projectId}/secrets/${projectSecret.secret_id}/test`,
        { method: 'POST' }
      )
    } else {
      res = await apiFetch<{ ok: boolean; error?: string }>(`/api/tokens/${key}/test`, { method: 'POST' })
    }
    messages[key] = res.ok
      ? { ok: true, text: '连接成功' }
      : { ok: false, text: res.error || '连接失败' }
  } catch (e) {
    messages[key] = { ok: false, text: e instanceof Error ? e.message : '测试失败' }
  } finally {
    testing.value = null
  }
}

async function deleteIntegration(key: string) {
  saving.value = key
  delete messages[key]
  try {
    const projectId = projectStore.currentProject?.project_id
    const scope = integrationScopes[key]
    const svc = integrationServices.find(s => s.key === key)
    // Delete the user token(s) when the integration lives there
    if (scope !== 'project') {
      await apiFetch(`/api/tokens/${key}`, { method: 'DELETE' })
      if (svc?.extraFields) {
        for (const field of svc.extraFields) {
          if (savedKeys[field.key]) {
            await apiFetch(`/api/tokens/${field.key}`, { method: 'DELETE' }).catch(() => {})
          }
        }
      }
    }
    // Delete the project secret(s) when the integration lives there
    if (scope !== 'user' && projectId) {
      const main = projectSecretByKey[key]
      if (main) {
        await deleteProjectSecret(projectId, main.secret_id)
      }
      for (const field of svc?.extraFields ?? []) {
        const pf = projectSecretByKey[field.key]
        if (pf) {
          await deleteProjectSecret(projectId, pf.secret_id).catch(() => {})
        }
      }
    }
    // Clean up local state
    delete savedKeys[key]
    delete savedBaseUrls[key]
    delete formValues[key]
    delete formValues[key + ':base_url']
    delete projectSecretByKey[key]
    delete integrationScopes[key]
    if (svc?.extraFields) {
      for (const field of svc.extraFields) {
        delete savedKeys[field.key]
        delete formValues[field.key]
        delete projectSecretByKey[field.key]
      }
    }
    configuredIntegrationKeys.value = new Set(
      [...configuredIntegrationKeys.value].filter(k => k !== key)
    )
    messages[key] = { ok: true, text: '已删除' }
  } catch (e) {
    messages[key] = { ok: false, text: e instanceof Error ? e.message : '删除失败' }
  } finally {
    saving.value = null
  }
}

// ── Add New Integration ────────────────────────────────────────────

const showAddIntegrationForm = ref(false)
const newIntegrationKey = ref('')

const newIntegrationService = computed(() =>
  integrationServices.find(s => s.key === newIntegrationKey.value)
)

function cancelAddIntegration() {
  showAddIntegrationForm.value = false
  if (newIntegrationKey.value) {
    // The form binds token and base_url to formValues[key] / [key + ':base_url']
    // — the old literal '__new_integration_base_url__' key never existed, so
    // the value stayed behind in memory and would pre-fill the next attempt.
    delete formValues[newIntegrationKey.value]
    delete formValues[newIntegrationKey.value + ':base_url']
  }
  newIntegrationKey.value = ''
  delete messages['__new_integration__']
}

async function addIntegration() {
  const key = newIntegrationKey.value
  if (!key || !formValues[key]) return
  saving.value = '__new_integration__'
  delete messages['__new_integration__']
  try {
    const baseUrlVal = formValues[key + ':base_url'] ?? ''
    const res = await apiFetch<{ key_hint: string }>(`/api/tokens/${key}`, {
      method: 'PUT',
      body: JSON.stringify({ value: formValues[key], base_url: baseUrlVal || null }),
    })
    savedKeys[key] = res.key_hint
    savedBaseUrls[key] = baseUrlVal || null
    // Save extra fields
    const svc = newIntegrationService.value
    if (svc?.extraFields) {
      for (const field of svc.extraFields) {
        if (formValues[field.key]) {
          const fieldRes = await apiFetch<{ key_hint: string }>(`/api/tokens/${field.key}`, {
            method: 'PUT',
            body: JSON.stringify({ value: formValues[field.key] }),
          })
          savedKeys[field.key] = fieldRes.key_hint
        }
      }
    }
    configuredIntegrationKeys.value = new Set([...configuredIntegrationKeys.value, key])
    integrationScopes[key] = 'user'  // new integrations always go to user_tokens
    // Reset add form
    formValues[key] = ''
    delete formValues[key + ':base_url']
    if (svc?.extraFields) {
      for (const field of svc.extraFields) {
        delete formValues[field.key]
      }
    }
    newIntegrationKey.value = ''
    showAddIntegrationForm.value = false
    messages['__new_integration__'] = { ok: true, text: '已添加' }
  } catch (e) {
    messages['__new_integration__'] = { ok: false, text: e instanceof Error ? e.message : '添加失败' }
  } finally {
    saving.value = null
  }
}

async function testNewIntegration() {
  const key = newIntegrationKey.value
  if (!key || !formValues[key]) return
  testing.value = '__new_integration__'
  delete messages['__new_integration__']
  try {
    // Save first, then test
    await addIntegration()
    if (messages['__new_integration__']?.ok) {
      const res = await apiFetch<{ ok: boolean; error?: string }>(`/api/tokens/${key}/test`, { method: 'POST' })
      messages['__new_integration__'] = res.ok
        ? { ok: true, text: '连接成功' }
        : { ok: false, text: res.error || '连接失败' }
    }
  } catch (e) {
    messages['__new_integration__'] = { ok: false, text: e instanceof Error ? e.message : '测试失败' }
  } finally {
    testing.value = null
  }
}

// ── Lifecycle ──────────────────────────────────────────────────────

function onKeydown(e: KeyboardEvent) { if (e.key === 'Escape') emit('close') }

onMounted(async () => {
  await agentStore.fetchModelConfigs()
  initEditForms()
  loadIntegrationDefaults()
  loadIntegrationTokens()
  loadMcpServers()
  document.addEventListener('keydown', onKeydown)
})
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.system-default-row {
  opacity: 0.8;
  border-left: 3px solid var(--accent);
}
.system-badge {
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
}
.token-empty-state {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
}
.token-add-section {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}
.token-add-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.add-model-btn {
  width: 100%;
}
.btn-sm.danger {
  color: var(--danger);
  border-color: var(--danger);
}
.btn-sm.danger:hover {
  background: var(--danger);
  color: #fff;
}
.token-url-mode-select {
  width: 120px;
  flex-shrink: 0;
  font-size: 12px;
}
.token-tier-select {
  width: 140px;
  flex-shrink: 0;
  font-size: 12px;
}
.tier-badge {
  border: none;
  background: rgba(127, 127, 127, 0.12);
}
.tier-badge.tier-low {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.tier-badge.tier-mid {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}
.tier-badge.tier-high {
  background: rgba(139, 92, 246, 0.15);
  color: #a78bfa;
}
.token-url-row {
  display: flex;
  gap: 8px;
}
.token-url-input {
  flex: 1;
}
.token-section-hint {
  margin: 0 16px 8px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
}
.token-section-hint code {
  font-size: 11px;
  background: var(--bg-secondary, rgba(127, 127, 127, 0.15));
  padding: 1px 4px;
  border-radius: 3px;
}
.scope-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.scope-badge.user-scope {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
}
.scope-badge.project-scope {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.mcp-section {
  margin-top: 4px;
  padding-top: 12px;
}
.mcp-edit-block {
  margin: 4px 16px 8px;
}
.mcp-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 8px;
}
.mcp-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.mcp-inline-msg {
  margin: 6px 16px 0;
}
.token-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}
.mcp-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.mcp-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
}
.mcp-item:last-child {
  border-bottom: none;
}
.mcp-item-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mcp-name {
  font-size: 13px;
  font-weight: 500;
}
.mcp-slug {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
.mcp-item-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.mcp-url {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mcp-status,
.mcp-auth {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.mcp-status.on {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.mcp-status.off {
  background: rgba(127, 127, 127, 0.15);
  color: var(--text-muted);
}
.mcp-auth.ok {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.mcp-auth.warn {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}
</style>
