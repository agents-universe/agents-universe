import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import ConversationTreePanel from './ConversationTreePanel.vue'
import ConversationTreeItem from './ConversationTreeItem.vue'
import type { AgentInfo, ConversationItem } from '@/types'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  getTasks: vi.fn(),
  getMessages: vi.fn(),
  getLatestRun: vi.fn(),
  delete: vi.fn(),
  rename: vi.fn(),
}))
vi.mock('@/api/conversations', () => ({ conversationsApi: api }))
vi.mock('@/composables/useWebSocket', () => ({
  closeConnection: vi.fn(),
  closeAllConnections: vi.fn(),
}))

function makeConv(over: Partial<ConversationItem> = {}): ConversationItem {
  return {
    conversation_id: 'c1',
    title: 'Test conversation',
    agent_id: null,
    agent_slug: null,
    token_budget: 128000,
    tokens_used: 0,
    message_count: 5,
    active_task_count: 0,
    total_task_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function makeAgent(over: Partial<AgentInfo> = {}): AgentInfo {
  return {
    agent_id: 'a1',
    slug: 'agent',
    label: '甲',
    description: '',
    category: 'test',
    skills: [],
    workflows: [],
    ...over,
  }
}

describe('ConversationTreePanel', () => {
  let store: ReturnType<typeof useConversationStore>
  let agentStore: ReturnType<typeof useAgentStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useConversationStore()
    agentStore = useAgentStore()
    api.list.mockReset().mockResolvedValue([makeConv()])
    api.getTasks.mockReset().mockResolvedValue([])
    api.getMessages.mockReset().mockResolvedValue([])
    api.getLatestRun.mockReset().mockResolvedValue(null)
    api.delete.mockReset().mockResolvedValue(undefined)
    api.rename.mockReset()
  })

  it('expands the active conversation by default and prefetches its tasks', async () => {
    store.startConversation('c1')
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()
    expect(wrapper.findComponent(ConversationTreeItem).props('isExpanded')).toBe(true)
    expect(api.getTasks).toHaveBeenCalledWith('c1')
    wrapper.unmount()
  })

  it('does not auto-expand non-active conversations', async () => {
    store.startConversation('c2')
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()
    expect(wrapper.findComponent(ConversationTreeItem).props('isExpanded')).toBe(false)
    wrapper.unmount()
  })

  it('shows live store tasks for the active conversation while streaming', async () => {
    store.startConversation('c1')
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()
    store.setTasks([{ task_id: 't1', title: '调研需求', status: 'running' }], 'c1')
    await flushPromises()
    const items = wrapper.findAll('.task-tree-item')
    expect(items).toHaveLength(1)
    expect(items[0].text()).toContain('调研需求')
    wrapper.unmount()
  })

  it('refetches tasks when the active conversation stream ends', async () => {
    store.startConversation('c1')
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()
    expect(api.getTasks).toHaveBeenCalledTimes(1)
    store.startThinking('c1')
    await flushPromises()
    store.stopThinking('c1')
    await flushPromises()
    expect(api.getTasks.mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('searches across all agents after the debounce', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)
      api.list.mockClear()

      await wrapper.find('.conv-tree-search').setValue('kafka')
      // Debounce has not elapsed yet — no request.
      expect(api.list).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(300)
      // No agent_slug: the search spans every agent in the project.
      expect(api.list).toHaveBeenCalledWith('p1', undefined, 'kafka')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('restores the agent-scoped list when the search is cleared', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)
      await wrapper.find('.conv-tree-search').setValue('kafka')
      await vi.advanceTimersByTimeAsync(300)
      api.list.mockClear()

      await wrapper.find('.conv-tree-search').setValue('')
      await vi.advanceTimersByTimeAsync(0)
      expect(api.list).toHaveBeenCalledWith('p1', 'agent')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('pauses the 5s poll while a search is active', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)

      await wrapper.find('.conv-tree-search').setValue('kafka')
      await vi.advanceTimersByTimeAsync(300)
      api.list.mockClear()

      await vi.advanceTimersByTimeAsync(5_000)
      expect(api.list).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the no-match message instead of the empty message while searching', async () => {
    vi.useFakeTimers()
    try {
      api.list.mockResolvedValue([])
      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)
      expect(wrapper.find('.conv-tree-empty').text()).toBe('暂无对话')

      await wrapper.find('.conv-tree-search').setValue('nothing matches')
      await vi.advanceTimersByTimeAsync(300)
      expect(wrapper.find('.conv-tree-empty').text()).toBe('没有匹配的对话')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('badges other agents results and switches agent before opening one', async () => {
    vi.useFakeTimers()
    try {
      agentStore.agents = [
        makeAgent(),
        makeAgent({ agent_id: 'a2', slug: 'other', label: '乙' }),
      ]
      agentStore.currentAgent = agentStore.agents[0]
      api.list.mockResolvedValue([makeConv({ conversation_id: 'c9', agent_slug: 'other' })])

      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)
      await wrapper.find('.conv-tree-search').setValue('kafka')
      await vi.advanceTimersByTimeAsync(300)

      expect(wrapper.find('.conv-tree-agent-badge').text()).toBe('乙')

      await wrapper.find('.conv-tree-item').trigger('click')
      await vi.advanceTimersByTimeAsync(0)
      await flushPromises()

      expect(agentStore.currentAgent?.slug).toBe('other')
      expect(store.conversationId).toBe('c9')
      expect(api.getMessages).toHaveBeenCalledWith('c9')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the search when the new-conversation button is clicked', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ConversationTreePanel, {
        props: { projectId: 'p1', agentSlug: 'agent' },
      })
      await vi.advanceTimersByTimeAsync(0)
      await wrapper.find('.conv-tree-search').setValue('kafka')
      await vi.advanceTimersByTimeAsync(300)
      api.list.mockClear()

      await wrapper.find('.conv-tree-new-btn').trigger('click')
      await vi.advanceTimersByTimeAsync(0)

      expect(wrapper.emitted('new-conversation')).toHaveLength(1)
      expect((wrapper.find('.conv-tree-search').element as HTMLInputElement).value).toBe('')
      expect(api.list).toHaveBeenCalledWith('p1', 'agent')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('renames a conversation in place after the API accepts it', async () => {
    api.rename.mockResolvedValue({ conversation_id: 'c1', title: '新名字' })
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()

    await wrapper.find('.conv-tree-rename-btn').trigger('click')
    const input = wrapper.find('.conv-tree-rename-input')
    await input.setValue('新名字')
    await input.trigger('keydown.enter')
    await flushPromises()

    expect(api.rename).toHaveBeenCalledWith('c1', '新名字')
    expect(wrapper.find('.conv-tree-title').text()).toBe('新名字')
    wrapper.unmount()
  })

  it('keeps the old title and surfaces the error when renaming fails', async () => {
    api.rename.mockRejectedValue(new Error('rename boom'))
    const wrapper = mount(ConversationTreePanel, {
      props: { projectId: 'p1', agentSlug: 'agent' },
    })
    await flushPromises()

    await wrapper.find('.conv-tree-rename-btn').trigger('click')
    const input = wrapper.find('.conv-tree-rename-input')
    await input.setValue('新名字')
    await input.trigger('keydown.enter')
    await flushPromises()

    expect(wrapper.find('.conv-tree-error').text()).toBe('rename boom')
    expect(wrapper.find('.conv-tree-title').text()).toBe('Test conversation')
    wrapper.unmount()
  })
})
