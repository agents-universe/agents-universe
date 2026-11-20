import { describe, it, expect, vi, afterEach } from 'vitest'
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

const runCounts = vi.hoisted(() => {
  const counts: Record<string, number> = {}
  return {
    counts,
    agentCount: (slug: string) => counts[slug] ?? 0,
    reset: () => { for (const k of Object.keys(counts)) delete counts[k] },
  }
})
vi.mock('@/stores/runningCounts', () => ({ useRunningCountsStore: () => runCounts }))

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
    workflows: [
      { slug: 'knowledge-ingestion', description: 'Ingest URL into knowledge', public: true },
      { slug: 'ad-hoc-analysis', description: 'Ad-hoc analysis' },
    ],
    tools: [],
  }
}

describe('AgentSwitcher', () => {
  afterEach(() => runCounts.reset())

  it('shows the running-count badge between the label and the active pip', () => {
    runCounts.counts['tech-lead'] = 3
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    const row = wrapper.find('.agent-item')
    const badge = row.find('.agent-run-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('3')

    const children = [...row.element.children]
    const labelIdx = children.findIndex((el) => el.classList.contains('agent-label'))
    const badgeIdx = children.findIndex((el) => el.classList.contains('agent-run-badge'))
    expect(labelIdx).toBeGreaterThanOrEqual(0)
    expect(badgeIdx).toBe(labelIdx + 1)

    wrapper.unmount()
  })

  it('hides the badge when the count is zero or belongs to another agent', () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const zero = mount(AgentSwitcher)
    expect(zero.find('.agent-run-badge').exists()).toBe(false)
    zero.unmount()

    runCounts.counts['other-agent'] = 4
    const foreign = mount(AgentSwitcher)
    expect(foreign.find('.agent-run-badge').exists()).toBe(false)
    foreign.unmount()
  })

  it('keeps the active pip independent of the badge', () => {
    runCounts.counts['tech-lead'] = 1
    favorites.resolvedFavoriteAgents = [makeAgent()]
    agentStore.currentAgent = makeAgent()
    const wrapper = mount(AgentSwitcher)
    const row = wrapper.find('.agent-item')
    expect(row.find('.agent-run-badge').exists()).toBe(true)
    expect(row.find('.agent-active-pip').exists()).toBe(true)
    wrapper.unmount()
  })

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

  it('shows the 公共 badge on public workflows in the hover tooltip', async () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    await wrapper.find('.agent-item').trigger('mouseenter')

    const badges = [...document.body.querySelectorAll('.public-badge')]
    const wfBadge = badges.find((b) =>
      b.closest('.agent-tooltip__item')!.querySelector('.agent-tooltip__slug')!.textContent ===
      'knowledge-ingestion',
    )
    expect(wfBadge).toBeDefined()
    expect(wfBadge!.textContent).toBe('公共')

    wrapper.unmount()
  })

  it('omits the badge for non-public workflows', async () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    await wrapper.find('.agent-item').trigger('mouseenter')

    const items = [...document.body.querySelectorAll('.agent-tooltip__item')]
    const plain = items.find((el) => el.textContent?.includes('ad-hoc-analysis'))
    expect(plain).toBeDefined()
    expect(plain!.querySelector('.public-badge')).toBeNull()

    wrapper.unmount()
  })

  it('exposes the full skill/workflow description via title on the clamped desc', async () => {
    favorites.resolvedFavoriteAgents = [makeAgent()]
    const wrapper = mount(AgentSwitcher)

    await wrapper.find('.agent-item').trigger('mouseenter')

    const descs = [...document.body.querySelectorAll('.agent-tooltip__item-desc')]
    expect(descs.map((el) => el.getAttribute('title'))).toEqual([
      'Manage project knowledge',
      'Ask the user',
      'Ingest URL into knowledge',
      'Ad-hoc analysis',
    ])

    wrapper.unmount()
  })
})
