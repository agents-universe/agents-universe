import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { setLocale } from '@/i18n'
import AgentPickerDialog from './AgentPickerDialog.vue'
import type { AgentInfo } from '@/types'

const projectStore = vi.hoisted(() => ({ currentProject: null as { project_id: string } | null }))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

const agentStore = vi.hoisted(() => ({
  agents: [] as AgentInfo[],
  reloadAgents: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

const favorites = vi.hoisted(() => ({
  favorited: new Set<string>(),
  defaults: new Set<string>(),
  hasAgentFavoriteOverrides: false,
  isAgentFavorited: (slug: string) => favorites.favorited.has(slug),
  isDefaultFavorite: (slug: string) => favorites.defaults.has(slug),
  toggleAgentFavorite: vi.fn(),
  resetAgentFavoritesToDefaults: vi.fn(),
}))
vi.mock('@/stores/favorites', () => ({ useFavoritesStore: () => favorites }))

function makeAgent(slug: string, category: string, label = slug): AgentInfo {
  return {
    agent_id: `a-${slug}`,
    slug,
    label,
    description: '',
    category,
    skills: [],
    workflows: [],
    project_id: null,
  }
}

describe('AgentPickerDialog', () => {
  let wrapper: VueWrapper | null = null

  beforeEach(() => {
    agentStore.agents = []
    favorites.favorited.clear()
    favorites.defaults.clear()
    favorites.hasAgentFavoriteOverrides = false
    favorites.toggleAgentFavorite.mockClear()
    favorites.resetAgentFavoritesToDefaults.mockClear()
    projectStore.currentProject = { project_id: 'p-1' }
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
    setLocale('zh-CN')
  })

  function mountDialog() {
    wrapper = mount(AgentPickerDialog, { attachTo: document.body })
  }

  it('groups data-analysis and office-docs agents under their own headings', async () => {
    agentStore.agents = [
      makeAgent('data-analyst', 'data-analysis', '数据分析专家'),
      makeAgent('office-assistant', 'office-docs', '办公助手'),
    ]
    mountDialog()
    await vi.waitFor(() => {
      const headings = [...document.querySelectorAll('.picker-category-heading')].map(h => h.textContent)
      expect(headings).toEqual(['数据分析', '办公文档'])
      // neither falls into the catch-all bucket
      expect(headings).not.toContain('未分类/未知')
    })
  })

  it('marks an agent that is favorited by the project category', async () => {
    agentStore.agents = [makeAgent('project-owner', 'agile-development', 'Product Owner')]
    favorites.favorited.add('project-owner')
    favorites.defaults.add('project-owner')
    mountDialog()
    await vi.waitFor(() => {
      const tag = document.querySelector('.picker-default-tag')
      expect(tag?.textContent).toBe('默认')
    })
  })

  it('does not mark a manually favorited agent', async () => {
    agentStore.agents = [makeAgent('pentest-expert', 'security', '渗透测试专家')]
    favorites.favorited.add('pentest-expert')
    mountDialog()
    await vi.waitFor(() => {
      expect(document.querySelectorAll('.picker-item').length).toBe(1)
      expect(document.querySelector('.picker-default-tag')).toBeNull()
    })
  })

  it('does not mark a default the user unfavorited', async () => {
    agentStore.agents = [makeAgent('quality-assurance', 'agile-development', 'Quality Assurance')]
    favorites.defaults.add('quality-assurance')
    mountDialog()
    await vi.waitFor(() => {
      expect(document.querySelectorAll('.picker-item').length).toBe(1)
      expect(document.querySelector('.picker-default-tag')).toBeNull()
    })
  })

  it('resets to the category defaults only when the favorites deviate', async () => {
    agentStore.agents = [makeAgent('project-owner', 'agile-development')]
    mountDialog()
    const button = () => document.querySelector<HTMLButtonElement>('.picker-reset-btn')!
    await vi.waitFor(() => expect(button()).not.toBeNull())
    expect(button().disabled).toBe(true)
    button().click()
    expect(favorites.resetAgentFavoritesToDefaults).not.toHaveBeenCalled()

    favorites.hasAgentFavoriteOverrides = true
    await wrapper!.vm.$forceUpdate()
    await wrapper!.vm.$nextTick()
    expect(button().disabled).toBe(false)
    button().click()
    expect(favorites.resetAgentFavoritesToDefaults).toHaveBeenCalled()
  })

  it('filters agents across languages via the search box', async () => {
    // Canonical label is zh, en override lives only in i18n.
    agentStore.agents = [
      {
        ...makeAgent('tech-lead', 'agile-development', '技术负责人'),
        i18n: { display_name: { 'en-US': 'Tech Lead' } },
      },
      makeAgent('data-analyst', 'data-analysis', '数据分析专家'),
    ]
    mountDialog()
    await vi.waitFor(() => expect(document.querySelectorAll('.picker-item').length).toBe(2))

    // Search in English against the en override of a zh canonical agent.
    setLocale('en-US')
    const input = document.querySelector<HTMLInputElement>('.picker-search')!
    input.value = 'tech lead'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.waitFor(() => {
      const names = [...document.querySelectorAll('.picker-item-name')].map(n => n.textContent)
      expect(names).toEqual(['Tech Lead'])
    })

    // Search in Chinese against the zh canonical while locale is en-US.
    input.value = '数据分析'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.waitFor(() => {
      const names = [...document.querySelectorAll('.picker-item-name')].map(n => n.textContent)
      expect(names).toEqual(['数据分析专家'])
    })
  })
})
