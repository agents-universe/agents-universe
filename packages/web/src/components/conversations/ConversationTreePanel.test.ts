import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import ConversationTreePanel from './ConversationTreePanel.vue'
import ConversationTreeItem from './ConversationTreeItem.vue'
import type { ConversationItem } from '@/types'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  getTasks: vi.fn(),
  getMessages: vi.fn(),
  delete: vi.fn(),
}))
vi.mock('@/api/conversations', () => ({ conversationsApi: api }))
vi.mock('@/composables/useWebSocket', () => ({ closeConnection: vi.fn() }))

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

describe('ConversationTreePanel', () => {
  let store: ReturnType<typeof useConversationStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useConversationStore()
    api.list.mockReset().mockResolvedValue([makeConv()])
    api.getTasks.mockReset().mockResolvedValue([])
    api.getMessages.mockReset().mockResolvedValue([])
    api.delete.mockReset().mockResolvedValue(undefined)
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
})
