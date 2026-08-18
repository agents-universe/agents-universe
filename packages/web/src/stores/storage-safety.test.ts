import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from './project'
import { useAgentStore } from './agent'
import { useFavoritesStore } from './favorites'
import { useKnowledgeStore } from './knowledge'
import { useConversationStore } from './conversation'
import type { Project, AgentInfo } from '@/types'

function makeProject(id: string): Project {
  return {
    project_id: id,
    slug: id,
    display_name: id,
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category: 'default',
    created_by: 'u-1',
    visibility: 'public',
    is_owner: true,
    can_manage: true,
  }
}

function makeAgent(slug: string): AgentInfo {
  return {
    agent_id: `a-${slug}`,
    slug,
    label: slug,
    description: '',
    category: 'general',
    skills: [],
    workflows: [],
  }
}

describe('stores survive localStorage failures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('project store: setItem/removeItem throwing does not break selection', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('not allowed')
    })
    const store = useProjectStore()
    expect(() => store.setCurrentProject(makeProject('p1'))).not.toThrow()
    expect(store.currentProject?.project_id).toBe('p1')
    expect(() => store.setCurrentProject(null)).not.toThrow()
    expect(store.currentProject).toBeNull()
  })

  it('agent store: setItem throwing does not break selection or config', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('not allowed')
    })
    const store = useAgentStore()
    expect(() => store.setCurrentAgent(makeAgent('analyst'))).not.toThrow()
    expect(store.currentAgent?.slug).toBe('analyst')
    expect(() => store.setSelectedConfigId('cfg-1')).not.toThrow()
    expect(store.selectedConfigId).toBe('cfg-1')
    expect(() => store.setSelectedConfigId(null)).not.toThrow()
    expect(store.selectedConfigId).toBeNull()
  })

  it('favorites store: setItem throwing does not break toggling', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    const store = useFavoritesStore()
    expect(() => store.toggleProjectFavorite('p1')).not.toThrow()
    expect(store.favoriteProjectIds).toContain('p1')
    expect(() => store.toggleProjectFavorite('p1')).not.toThrow()
    expect(store.favoriteProjectIds).not.toContain('p1')
  })

  it('agent store: getItem throwing does not break reconcile or config restore', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const { agentsApi } = await import('@/api/agents')
    vi.spyOn(agentsApi, 'getAgents').mockResolvedValue([makeAgent('analyst'), makeAgent('researcher')])
    vi.spyOn(agentsApi, 'getModelConfigs').mockResolvedValue([])

    const store = useAgentStore()
    await store.fetchAgents()
    // The saved slug is unreadable — fall back to the first agent instead of
    // leaving currentAgent null (which disables the chat entry point).
    expect(store.agents.length).toBe(2)
    expect(store.currentAgent?.slug).toBe('analyst')

    await store.fetchModelConfigs()
    expect(store.modelConfigsError).toBeNull()
    expect(store.selectedConfigId).toBeNull()
  })

  it('favorites store: parseable non-array storage values are ignored', () => {
    // JSON.parse succeeds but the value is not an array — every .map()
    // consumer (resolvedFavorite*) would crash on a raw object/string.
    localStorage.setItem('agents-universe:favoriteProjectIds', '{}')
    localStorage.setItem('agents-universe:favoriteAgentSlugs', '"abc"')
    const store = useFavoritesStore()
    expect(store.favoriteProjectIds).toEqual([])
    expect(store.favoriteAgentSlugs).toEqual([])
    expect(() => store.resolvedFavoriteProjects).not.toThrow()
    expect(() => store.resolvedFavoriteAgents).not.toThrow()
  })

  it('startConversation clears the knowledge loadedThisTurn list', async () => {
    const knowledge = useKnowledgeStore()
    knowledge.setLoadedThisTurn(['docs/one.md', 'docs/two.md'])
    expect(knowledge.loadedThisTurn.length).toBe(2)

    const conv = useConversationStore()
    conv.startConversation('conv-b')

    // The knowledge store is cleared via dynamic import (async) — wait for it
    await vi.waitFor(() => expect(knowledge.loadedThisTurn).toEqual([]))
    // Items survive — only the turn-scoped list is cleared
    expect(conv.conversationId).toBe('conv-b')
  })
})
