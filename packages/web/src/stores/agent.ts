import { defineStore } from 'pinia'
import { ref } from 'vue'
import { agentsApi } from '@/api/agents'
import { modelConfigsApi } from '@/api/modelConfigs'
import { closeAllConnections } from '@/composables/useWebSocket'
import { useConversationStore } from '@/stores/conversation'
import type { AgentInfo, ModelConfig } from '@/types'

const STORAGE_KEY = 'agents-universe:currentAgentSlug'
const STORAGE_KEY_CONFIG_ID = 'agents-universe:selectedConfigId'

/** Per-project agent selection key.
 *
 * A single global key made the restored agent last-project-wins: switching
 * projects changed the selection for EVERY project, and since getLatest /
 * list_conversations filter conversations by agent, reopening a project with
 * the wrong agent restored showed an empty conversation tree — "all previous
 * records lost". The legacy global key stays as the fallback for the
 * no-project scope and as a one-time migration read. */
function agentStorageKey(scope: string | null | undefined): string {
  return scope ? `agents-universe:agent:${scope}` : STORAGE_KEY
}

/** Reserved config_id for the composer's "auto" model option. */
export const AUTO_MODEL_CONFIG_ID = 'auto'

export const useAgentStore = defineStore('agent', () => {
  const agents = ref<AgentInfo[]>([])
  const currentAgent = ref<AgentInfo | null>(null)
  const selectedConfigId = ref<string | null>(null)
  const modelConfigs = ref<ModelConfig[]>([])
  const modelConfigsLoading = ref(false)
  const modelConfigsError = ref<string | null>(null)
  const loaded = ref(false)
  // Agents list is cached per project scope (null = global only); project
  // agents must be re-fetched when the current project changes.
  const loadedForProject = ref<string | null | undefined>(undefined)

  function setCurrentAgent(agent: AgentInfo) {
    const changed = currentAgent.value?.slug !== agent.slug
    currentAgent.value = agent
    // Persist under the LIVE current-project scope, not loadedForProject:
    // reloadAgents() sets loadedForProject=undefined before its fetch lands,
    // and a project-switch fetch leaves it holding the PREVIOUS project — a
    // click in either window would write the slug under the wrong scope (the
    // legacy global key), and _reconcileCurrentAgent's global-key fallback
    // would then seed every project that never saved its own key (the exact
    // "last-project-wins" leak the per-project key design exists to prevent).
    // The project store persists its selection to localStorage synchronously
    // and reads the same key via getSavedProjectId(), so reading the key here
    // gives the live scope without importing the store (avoids the agent →
    // project cycle) and without an async dynamic import (the persist must
    // land before setCurrentAgent returns).
    let scope: string | null | undefined = loadedForProject.value
    try {
      scope = localStorage.getItem('agents-universe:currentProjectId') ?? loadedForProject.value
    } catch { /* storage unavailable — fall back to the loaded scope */ }
    try {
      localStorage.setItem(agentStorageKey(scope), agent.slug)
    } catch { /* storage unavailable — selection survives in memory only */ }
    if (changed) {
      // Conversations are bound to (project, agent). Switching agents while
      // away from the chat page (or mid-stream) must not leave the previous
      // agent's conversation active — otherwise returning to the chat page
      // shows the old agent's messages with no trigger to reload. Close the
      // live WS connections AND clear the runtimes here (every route, not
      // just ChatPage's mounted watcher): an open socket would otherwise keep
      // rebuilding the reset runtime from streamed events — messages pile up
      // in a conversation nobody is viewing while the agent runs unseen.
      closeAllConnections()
      useConversationStore().reset()
    }
  }

  function setAgents(list: AgentInfo[]) {
    agents.value = list
  }

  function setSelectedConfigId(configId: string | null) {
    selectedConfigId.value = configId
    try {
      if (configId) {
        localStorage.setItem(STORAGE_KEY_CONFIG_ID, configId)
      } else {
        localStorage.removeItem(STORAGE_KEY_CONFIG_ID)
      }
    } catch { /* storage unavailable — selection survives in memory only */ }
  }

  // Monotonic guard: a slow response for an older scope must not overwrite the
  // agents of a newer scope (e.g. rapid project A → B switching).
  let fetchSeq = 0

  /** Scope hygiene for the current agent, run on EVERY fetchAgents call —
   * including the cache-hit path, which previously skipped it and let a
   * project-scoped agent from another project leak into this scope (and
   * never restored the right selection when returning to a cached scope). */
  function _reconcileCurrentAgent(scope: string | null) {
    // A project agent from another project must never leak into this scope:
    // reset the current agent if it is scoped elsewhere.
    if (currentAgent.value?.project_id && currentAgent.value.project_id !== scope) {
      currentAgent.value = null
    }
    if (agents.value.length > 0 && !currentAgent.value) {
      // Storage can throw (Safari private mode, disabled storage) — the saved
      // selection is a convenience, never something worth breaking the load.
      let savedSlug: string | null = null
      try {
        savedSlug = localStorage.getItem(agentStorageKey(scope))
        // One-time migration: selections saved under the legacy global key
        // (pre-per-project) still restore for scopes without their own key.
        if (!savedSlug) savedSlug = localStorage.getItem(STORAGE_KEY)
      } catch { /* storage unavailable — fall back to the first agent */ }
      const saved = savedSlug
        ? agents.value.find(a => a.slug === savedSlug && !a.project_id) ?? agents.value.find(a => a.slug === savedSlug)
        : null
      currentAgent.value = saved ?? agents.value[0]
    }
  }

  async function fetchAgents(projectId?: string | null) {
    const scope: string | null = projectId ?? null
    // Bump the monotonic guard even on the cached early-return path: a
    // fetchAgents(A) while B's response is still in flight must invalidate
    // it — otherwise B's late response still matches (its seq is the last
    // bump) and writes B's agents into the UI while project A is active
    // .
    const seq = ++fetchSeq
    if (loadedForProject.value === scope) {
      _reconcileCurrentAgent(scope)
      return
    }
    try {
      const data = await agentsApi.getAgents(scope ?? undefined)
      if (seq !== fetchSeq) return
      agents.value = data
      _reconcileCurrentAgent(scope)
      loadedForProject.value = scope
      loaded.value = true
    } catch (e) {
      console.error('Failed to fetch agents', e)
    }
  }

  async function reloadAgents(projectId?: string | null) {
    loadedForProject.value = undefined
    await fetchAgents(projectId)
  }

  async function syncAgents(projectId: string) {
    await agentsApi.syncAgents(projectId)
    await reloadAgents(projectId)
  }

  async function fetchModelConfigs() {
    modelConfigsLoading.value = true
    modelConfigsError.value = null
    try {
      modelConfigs.value = await agentsApi.getModelConfigs()
      let savedConfigId: string | null = null
      try { savedConfigId = localStorage.getItem(STORAGE_KEY_CONFIG_ID) } catch { /* storage unavailable — no saved selection */ }
      if (savedConfigId === AUTO_MODEL_CONFIG_ID) {
        // The "auto" sentinel is not a real config — keep it as the selection
        // instead of treating it as a stale entry.
        selectedConfigId.value = savedConfigId
      } else if (savedConfigId && modelConfigs.value.some(c => c.config_id === savedConfigId)) {
        selectedConfigId.value = savedConfigId
      } else if (savedConfigId) {
        // The saved model config no longer exists; clear the stale cache entry.
        try { localStorage.removeItem(STORAGE_KEY_CONFIG_ID) } catch { /* storage unavailable — stale entry stays */ }
      }
    } catch (e) {
      modelConfigsError.value = e instanceof Error ? e.message : String(e)
    } finally {
      modelConfigsLoading.value = false
    }
  }

  async function reloadModelConfigs() {
    await fetchModelConfigs()
  }

  async function addModelConfig(body: { provider: string; model_id: string; api_key?: string; base_url?: string; url_mode?: string; complexity_tier?: 'low' | 'mid' | 'high' | null; context_window?: number | null }) {
    const created = await modelConfigsApi.create(body)
    modelConfigs.value = [...modelConfigs.value.filter(c => !c.is_system), created, ...modelConfigs.value.filter(c => c.is_system)]
    return created
  }

  async function updateModelConfig(configId: string, body: { model_id?: string; api_key?: string; base_url?: string; url_mode?: string; complexity_tier?: 'low' | 'mid' | 'high' | null; context_window?: number | null }) {
    const updated = await modelConfigsApi.update(configId, body)
    modelConfigs.value = modelConfigs.value.map(c => c.config_id === configId ? updated : c)
    return updated
  }

  async function removeModelConfig(configId: string) {
    await modelConfigsApi.remove(configId)
    modelConfigs.value = modelConfigs.value.filter(c => c.config_id !== configId)
    if (selectedConfigId.value === configId) {
      setSelectedConfigId(modelConfigs.value[0]?.config_id ?? null)
    }
  }

  return {
    agents,
    currentAgent,
    selectedConfigId,
    modelConfigs,
    modelConfigsLoading,
    modelConfigsError,
    loaded,
    setCurrentAgent,
    setAgents,
    setSelectedConfigId,
    fetchAgents,
    reloadAgents,
    syncAgents,
    fetchModelConfigs,
    reloadModelConfigs,
    addModelConfig,
    updateModelConfig,
    removeModelConfig,
  }
})
