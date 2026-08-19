import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAgentStore, AUTO_MODEL_CONFIG_ID } from './agent'
import type { ModelConfig } from '@/types'

vi.mock('@/api/agents', () => ({
  agentsApi: {
    getAgents: vi.fn(),
    syncAgents: vi.fn(),
    getModelConfigs: vi.fn(),
  },
}))

vi.mock('@/api/modelConfigs', () => ({
  modelConfigsApi: {
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
  },
}))

vi.mock('@/composables/useWebSocket', () => ({
  closeAllConnections: vi.fn(),
}))

vi.mock('@/stores/conversation', () => ({
  useConversationStore: () => ({ reset: vi.fn() }),
}))

import { agentsApi } from '@/api/agents'
import { modelConfigsApi } from '@/api/modelConfigs'

const mockedGetModelConfigs = agentsApi.getModelConfigs as ReturnType<typeof vi.fn>
const mockedRemove = modelConfigsApi.remove as ReturnType<typeof vi.fn>

function makeConfig(over: Partial<ModelConfig> = {}): ModelConfig {
  return {
    config_id: 'c1',
    provider: 'openai',
    model_id: 'gpt-4o',
    key_hint: null,
    base_url: null,
    url_mode: 'base_url',
    complexity_tier: null,
    context_window: null,
    default_context_window: 128_000,
    is_system: false,
    ...over,
  }
}

describe('agent store — model config selection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('keeps the auto sentinel as the selection after fetch', async () => {
    localStorage.setItem('agents-universe:selectedConfigId', AUTO_MODEL_CONFIG_ID)
    mockedGetModelConfigs.mockResolvedValue([makeConfig()])

    const store = useAgentStore()
    await store.fetchModelConfigs()

    // "auto" is not a real config_id — it must not be treated as a stale
    // entry and dropped on fetch.
    expect(store.selectedConfigId).toBe(AUTO_MODEL_CONFIG_ID)
    expect(localStorage.getItem('agents-universe:selectedConfigId')).toBe(AUTO_MODEL_CONFIG_ID)
  })

  it('restores a real saved selection when present', async () => {
    localStorage.setItem('agents-universe:selectedConfigId', 'c1')
    mockedGetModelConfigs.mockResolvedValue([makeConfig()])

    const store = useAgentStore()
    await store.fetchModelConfigs()

    expect(store.selectedConfigId).toBe('c1')
  })

  it('clears a stale saved selection that no longer exists', async () => {
    localStorage.setItem('agents-universe:selectedConfigId', 'c-gone')
    mockedGetModelConfigs.mockResolvedValue([makeConfig()])

    const store = useAgentStore()
    await store.fetchModelConfigs()

    expect(store.selectedConfigId).toBeNull()
    expect(localStorage.getItem('agents-universe:selectedConfigId')).toBeNull()
  })

  it('keeps auto selected when removing an unrelated config', async () => {
    mockedRemove.mockResolvedValue(undefined)
    const store = useAgentStore()
    store.modelConfigs = [makeConfig(), makeConfig({ config_id: 'c2', model_id: 'claude-sonnet' })]
    store.selectedConfigId = AUTO_MODEL_CONFIG_ID

    await store.removeModelConfig('c2')

    // Auto is not tied to any config — deleting another model must not
    // yank the selection back to a concrete model.
    expect(store.selectedConfigId).toBe(AUTO_MODEL_CONFIG_ID)
  })

  it('falls back to the first remaining real config (never auto) when the selected one is removed', async () => {
    mockedRemove.mockResolvedValue(undefined)
    const store = useAgentStore()
    store.modelConfigs = [makeConfig(), makeConfig({ config_id: 'c2', model_id: 'claude-sonnet' })]
    store.selectedConfigId = 'c1'

    await store.removeModelConfig('c1')

    expect(store.selectedConfigId).toBe('c2')
    expect(localStorage.getItem('agents-universe:selectedConfigId')).toBe('c2')
  })
})
