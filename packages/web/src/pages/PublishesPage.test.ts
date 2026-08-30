import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import PublishesPage from './PublishesPage.vue'
import type { PublishItem } from '@/api/publish'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({
  useRouter: () => router,
  useRoute: () => ({ params: { projectId: 'p-1' } }),
}))

const publishApi = vi.hoisted(() => ({
  list: vi.fn(),
  listKeys: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  createKey: vi.fn(),
  revokeKey: vi.fn(),
}))
vi.mock('@/api/publish', () => ({ publishApi }))

const agentsApi = vi.hoisted(() => ({ getAgents: vi.fn() }))
vi.mock('@/api/agents', () => ({ agentsApi }))

const agentStore = vi.hoisted(() => ({
  modelConfigs: [] as Array<{ config_id: string; model_id: string; provider: string; is_system: boolean }>,
  fetchModelConfigs: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

const projectStore = vi.hoisted(() => ({
  currentProject: { project_id: 'p-1', can_manage: true },
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

function makePublish(over: Partial<PublishItem> = {}): PublishItem {
  return {
    publish_id: 'pub-1',
    agent_slug: 'qa-bot',
    project_id: 'p-1',
    model_config_id: 'm-1',
    title: 'QA 助手',
    description: null,
    page_enabled: true,
    api_enabled: true,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: null,
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  publishApi.list.mockResolvedValue([])
  publishApi.listKeys.mockResolvedValue([])
  agentsApi.getAgents.mockResolvedValue([])
  agentStore.modelConfigs = []
  agentStore.fetchModelConfigs.mockResolvedValue([])
  projectStore.currentProject = { project_id: 'p-1', can_manage: true }
})

describe('PublishesPage', () => {
  it('renders only the current project publications', async () => {
    publishApi.list.mockResolvedValue([
      makePublish({ publish_id: 'pub-1', project_id: 'p-1', title: '本项目发布' }),
      makePublish({ publish_id: 'pub-2', project_id: 'p-2', title: '其他项目发布' }),
    ])
    const wrapper = mount(PublishesPage)
    await flushPromises()
    expect(wrapper.text()).toContain('本项目发布')
    expect(wrapper.text()).not.toContain('其他项目发布')
  })

  it('loads API keys only for the current project publications', async () => {
    publishApi.list.mockResolvedValue([
      makePublish({ publish_id: 'pub-1', project_id: 'p-1' }),
      makePublish({ publish_id: 'pub-2', project_id: 'p-2' }),
    ])
    mount(PublishesPage)
    await flushPromises()
    expect(publishApi.listKeys).toHaveBeenCalledTimes(1)
    expect(publishApi.listKeys).toHaveBeenCalledWith('pub-1')
  })

  it('shows the empty state when the current project has no publications', async () => {
    publishApi.list.mockResolvedValue([
      makePublish({ publish_id: 'pub-2', project_id: 'p-2' }),
    ])
    const wrapper = mount(PublishesPage)
    await flushPromises()
    expect(wrapper.find('.publishes-empty').exists()).toBe(true)
    expect(wrapper.text()).toContain('还没有发布')
  })

  it('creates into the current project without a project selector', async () => {
    publishApi.list.mockResolvedValue([])
    agentsApi.getAgents.mockResolvedValue([
      { slug: 'qa-bot', label: 'QA', project_id: 'p-1' },
    ])
    agentStore.modelConfigs = [
      { config_id: 'm-1', model_id: 'claude', provider: 'anthropic', is_system: false },
    ]
    publishApi.create.mockResolvedValue(makePublish())
    const wrapper = mount(PublishesPage, { attachTo: document.body })
    await flushPromises()

    await wrapper.find('.publishes-new').trigger('click')

    // The create modal is Teleported to <body>, outside the component tree.
    const selects = document.querySelectorAll('.publish-form select')
    // Only agent and model config selects remain — no project select.
    expect(selects.length).toBe(2)

    await new DOMWrapper(selects[0] as HTMLSelectElement).setValue('qa-bot')
    await new DOMWrapper(selects[1] as HTMLSelectElement).setValue('m-1')
    const submit = document.querySelector('.publish-form-actions .btn-primary') as HTMLButtonElement
    await new DOMWrapper(submit).trigger('click')
    await flushPromises()

    expect(publishApi.create).toHaveBeenCalledWith({
      agent_slug: 'qa-bot',
      project_id: 'p-1',
      model_config_id: 'm-1',
      title: null,
      description: null,
    })
    wrapper.unmount()
  })

  it('hides the new-publish button for non-managers', async () => {
    projectStore.currentProject = { project_id: 'p-1', can_manage: false }
    publishApi.list.mockResolvedValue([])
    const wrapper = mount(PublishesPage)
    await flushPromises()
    expect(wrapper.find('.publishes-new').exists()).toBe(false)
    expect(wrapper.find('.publishes-empty .btn-primary').exists()).toBe(false)
  })
})
