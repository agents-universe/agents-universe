import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentSwitcher from './AgentSwitcher.vue'
import type { AgentInfo } from '@/types'

const agentStore = vi.hoisted(() => ({
  currentAgent: null as AgentInfo | null,
  fetchAgents: vi.fn(),
  setCurrentAgent: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

const favorites = vi.hoisted(() => ({
  resolvedFavoriteAgents: [] as AgentInfo[],
}))
vi.mock('@/stores/favorites', () => ({ useFavoritesStore: () => favorites }))

const projectStore = vi.hoisted(() => ({
  currentProject: null as { project_id: string } | null,
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

function makeAgent(): AgentInfo {
  return {
    agent_id: 'a-1',
    slug: 'tech-lead',
    label: '技术负责人',
    description: 'Leads technical decisions',
    category: 'agile-development',
    project_id: null,
    skills: [
      {
        slug: 'knowledge/knowledge-manager',
        description: 'Manage project knowledge',
        public: true,
      },
      { slug: 'interaction/user-confirm', description: 'Ask the user' },
    ],
    workflows: [],
    tools: [],
  }
}

describe('AgentSwitcher', () => {
  it('shows the 公共 badge on public skills in the hover tooltip', async () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    await wrapper.find('.agent-item').trigger('mouseenter')

    // The tooltip is teleported to <body>, so query the document.
    const badge = document.body.querySelector('.public-badge')
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toBe('公共')
    expect(badge!.closest('.agent-tooltip__item')!.querySelector('.agent-tooltip__slug')!.textContent)
      .toBe('knowledge/knowledge-manager')

    wrapper.unmount()
  })

  it('omits the badge for non-public skills', async () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    await wrapper.find('.agent-item').trigger('mouseenter')

    const items = [...document.body.querySelectorAll('.agent-tooltip__item')]
    const plain = items.find((el) => el.textContent?.includes('interaction/user-confirm'))
    expect(plain).toBeDefined()
    expect(plain!.querySelector('.public-badge')).toBeNull()

    wrapper.unmount()
  })
})
