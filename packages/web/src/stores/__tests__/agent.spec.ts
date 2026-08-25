import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAgentStore } from '@/stores/agent'
import { agentsApi } from '@/api/agents'
import type { AgentInfo } from '@/types'

vi.mock('@/api/agents', () => ({
  agentsApi: {
    getAgents: vi.fn(),
    getModelConfigs: vi.fn(),
    syncAgents: vi.fn(),
  },
}))
const getAgentsMock = vi.mocked(agentsApi.getAgents)

const pentest: AgentInfo = {
  agent_id: 'g1',
  slug: 'pentest-expert',
  label: 'Pentest',
  description: '',
  category: 'software',
  skills: [],
  workflows: [],
}
const qaProj2: AgentInfo = {
  agent_id: 'p1',
  slug: 'qa-agent',
  label: 'QA',
  description: '',
  category: 'software',
  skills: [],
  workflows: [],
  project_id: 'proj-2',
}

describe('agent store per-project selection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    getAgentsMock.mockReset()
  })

  it('setCurrentAgent saves under the current project scope, not globally', async () => {
    getAgentsMock.mockResolvedValue([pentest, qaProj2])
    const agentStore = useAgentStore()
    await agentStore.fetchAgents('proj-2')

    agentStore.setCurrentAgent(qaProj2)

    expect(localStorage.getItem('agents-universe:agent:proj-2')).toBe('qa-agent')
    // The legacy global key is untouched — another project's selection
    // must not leak into this one.
    expect(localStorage.getItem('agents-universe:currentAgentSlug')).toBeNull()
  })

  it('restores the scoped selection per project (no cross-project leak)', async () => {
    localStorage.setItem('agents-universe:agent:proj-2', 'qa-agent')
    localStorage.setItem('agents-universe:currentAgentSlug', 'pentest-expert')
    getAgentsMock.mockResolvedValue([pentest, qaProj2])

    const agentStore = useAgentStore()
    await agentStore.fetchAgents('proj-2')
    expect(agentStore.currentAgent?.slug).toBe('qa-agent')
  })

  it('falls back to the legacy global key for projects without a scoped selection', async () => {
    localStorage.setItem('agents-universe:currentAgentSlug', 'pentest-expert')
    getAgentsMock.mockResolvedValue([pentest])

    const agentStore = useAgentStore()
    await agentStore.fetchAgents('proj-3')
    expect(agentStore.currentAgent?.slug).toBe('pentest-expert')
  })
})
