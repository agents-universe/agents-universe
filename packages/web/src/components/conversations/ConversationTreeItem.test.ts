import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ConversationTreeItem from './ConversationTreeItem.vue'
import type { ConversationItem, AgentTask } from '@/types'

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

describe('ConversationTreeItem', () => {
  it('shows pulse animation when streaming', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: true,
        tasks: [],
      },
    })
    expect(wrapper.find('.conv-tree-pulse').exists()).toBe(true)
    expect(wrapper.find('.conv-tree-live').text()).toBe('运行中')
    expect(wrapper.find('.conv-tree-item').classes()).toContain('streaming')
  })

  it('hides pulse animation when not streaming', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: false,
        tasks: [],
      },
    })
    expect(wrapper.find('.conv-tree-pulse').exists()).toBe(false)
    expect(wrapper.find('.conv-tree-live').exists()).toBe(false)
    expect(wrapper.find('.conv-tree-item').classes()).not.toContain('streaming')
  })

  it('applies active class when isActive', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: true,
        isExpanded: false,
        isStreaming: false,
        tasks: [],
      },
    })
    expect(wrapper.find('.conv-tree-item').classes()).toContain('active')
  })

  it('emits select on click', async () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: false,
        tasks: [],
      },
    })
    await wrapper.find('.conv-tree-item').trigger('click')
    expect(wrapper.emitted('select')).toHaveLength(1)
  })

  it('emits delete on delete button click', async () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: false,
        tasks: [],
      },
    })
    await wrapper.find('.conv-tree-delete-btn').trigger('click')
    expect(wrapper.emitted('delete')).toHaveLength(1)
  })

  it('shows both pulse and active class when streaming and active', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: true,
        isExpanded: false,
        isStreaming: true,
        tasks: [],
      },
    })
    const item = wrapper.find('.conv-tree-item')
    expect(item.classes()).toContain('active')
    expect(item.classes()).toContain('streaming')
    expect(wrapper.find('.conv-tree-pulse').exists()).toBe(true)
  })

  it('emits toggle-expand on chevron click without selecting', async () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: false,
        tasks: [],
      },
    })
    await wrapper.find('.conv-tree-chevron').trigger('click')
    expect(wrapper.emitted('toggle-expand')).toHaveLength(1)
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('renders task path when expanded with tasks', () => {
    const tasks: AgentTask[] = [
      { id: 't1', title: '调研需求', status: 'completed' },
      { id: 't2', title: '编写代码', status: 'running', currentStep: '写测试', nextStep: '跑测试' },
    ]
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: true,
        isStreaming: false,
        tasks,
      },
    })
    const items = wrapper.findAll('.task-tree-item')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('调研需求')
    expect(items[1].text()).toContain('写测试')
    // 截断的标题/步骤带 title 悬停提示，可显示完整内容
    expect(items[0].find('.task-title').attributes('title')).toBe('调研需求')
    expect(items[1].find('.current-step').attributes('title')).toBe('写测试')
  })

  it('renders status icon classes per task state', () => {
    const tasks: AgentTask[] = [
      { id: 't1', title: '调研需求', status: 'pending' },
      { id: 't2', title: '编写代码', status: 'completed' },
      { id: 't3', title: '写测试', status: 'running' },
      { id: 't4', title: '发布', status: 'failed' },
      { id: 't5', title: '跳过', status: 'skipped' },
    ]
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: true,
        isStreaming: false,
        tasks,
      },
    })
    expect(wrapper.findAll('.status-pending')).toHaveLength(1)
    expect(wrapper.findAll('.status-completed')).toHaveLength(1)
    expect(wrapper.findAll('.status-running')).toHaveLength(1)
    expect(wrapper.findAll('.status-failed')).toHaveLength(1)
    expect(wrapper.findAll('.status-skipped')).toHaveLength(1)
  })

  it('shows empty placeholder when expanded without tasks', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: true,
        isStreaming: false,
        tasks: [],
      },
    })
    const empty = wrapper.find('.conv-tree-tasks-empty')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toBe('暂无任务规划')
  })

  it('hides task path when collapsed even with tasks', () => {
    const wrapper = mount(ConversationTreeItem, {
      props: {
        conversation: makeConv(),
        isActive: false,
        isExpanded: false,
        isStreaming: false,
        tasks: [{ id: 't1', title: '调研需求', status: 'pending' }],
      },
    })
    expect(wrapper.find('.conv-tree-tasks').exists()).toBe(false)
    expect(wrapper.find('.task-tree-item').exists()).toBe(false)
  })
})
