import { describe, it, expect, beforeEach } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import StreamingStatus from '@/components/chat/StreamingStatus.vue'
import type { AgentInfo, ToolCallRecord } from '@/types'

const po: AgentInfo = {
  agent_id: 'a1', slug: 'project-owner', label: 'Product Owner',
  description: '', category: '', skills: [], workflows: [],
}
const techLead: AgentInfo = {
  agent_id: 'a2', slug: 'tech-lead', label: 'Tech Lead',
  description: '', category: '', skills: [], workflows: [],
}

function setup(turnAgentSlug: string | null) {
  setActivePinia(createPinia())
  const agentStore = useAgentStore()
  agentStore.setAgents([po, techLead])
  agentStore.setCurrentAgent(po)
  const convStore = useConversationStore()
  convStore.setConversationId('c-1')
  convStore.setTurnAgent(turnAgentSlug, 'c-1')
  return { wrapper: mount(StreamingStatus), convStore }
}

describe('StreamingStatus — @-mention turn agent visibility', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it('shows the anonymous "正在思考…" when the default agent answers', () => {
    const { wrapper } = setup('project-owner')
    expect(wrapper.find('.streaming-label').text()).toBe('正在思考…')
  })

  it('shows "正在调用 @Tech Lead…" while the mentioned agent is thinking', () => {
    const { wrapper } = setup('tech-lead')
    expect(wrapper.find('.streaming-label').text()).toBe('正在调用 @Tech Lead…')
  })

  it('prefixes the output state with the mentioned agent', async () => {
    const { wrapper, convStore } = setup('tech-lead')
    convStore.appendDelta('demo content', undefined, 'c-1')
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('@Tech Lead 正在输出…')
  })

  it('prefixes the running-tool state with the mentioned agent', async () => {
    const { wrapper, convStore } = setup('tech-lead')
    const tool: ToolCallRecord = {
      callId: 'c1', tool: 'code_executor', status: 'running', input: {},
    }
    convStore.addToolCall(tool, 'c-1')
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('@Tech Lead 正在调用 code_executor…')
  })

  it('falls back to the slug when the agent label is unknown', () => {
    const { wrapper } = setup('mystery-agent')
    expect(wrapper.find('.streaming-label').text()).toBe('正在调用 @mystery-agent…')
  })
})

describe('StreamingStatus — phase frames and parallel tool labels', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it('counts preparing tools toward the status line', async () => {
    // The card exists before execution starts — without it the line said
    // "thinking" while a tool warmed up.
    const { wrapper, convStore } = setup(null)
    convStore.addToolCall(
      { callId: 'c1', tool: 'playwright', status: 'preparing', input: {} }, 'c-1',
    )
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('正在调用 playwright…')
  })

  it('names the first two tools when several run in parallel', async () => {
    const { wrapper, convStore } = setup(null)
    for (const [i, tool] of ['shell', 'playwright', 'code_executor'].entries()) {
      convStore.addToolCall(
        { callId: `c${i}`, tool, status: 'running', input: {} }, 'c-1',
      )
    }
    await nextTick()
    expect(wrapper.find('.streaming-label').text())
      .toBe('shell, playwright 等 3 个工具并行调用中…')
  })

  it('falls back to the count wording when tool names are empty', async () => {
    const { wrapper, convStore } = setup(null)
    for (const i of [0, 1]) {
      convStore.addToolCall(
        { callId: `c${i}`, tool: '', status: 'running', input: {} }, 'c-1',
      )
    }
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('2 个工具并行调用中…')
  })

  it.each([
    ['waiting_model', '等待模型响应…'],
    ['thinking', '深度思考中…'],
    ['compressing', '正在压缩历史…'],
    ['degrading', '正在降级请求…'],
  ] as const)('shows the %s phase label', async (phase, label) => {
    const { wrapper, convStore } = setup(null)
    convStore.setTurnStatus(phase, undefined, 'c-1')
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe(label)
  })

  it('a running tool outranks the phase label', async () => {
    // Phase switches only when no tool is active — the tool branch states
    // the running action more precisely.
    const { wrapper, convStore } = setup(null)
    convStore.setTurnStatus('thinking', undefined, 'c-1')
    convStore.addToolCall(
      { callId: 'c1', tool: 'shell', status: 'running', input: {} }, 'c-1',
    )
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('正在调用 shell…')
  })

  it('the responding phase falls through to the output wording', async () => {
    const { wrapper, convStore } = setup(null)
    convStore.setTurnStatus('responding', undefined, 'c-1')
    convStore.appendDelta('partial answer', undefined, 'c-1')
    await nextTick()
    expect(wrapper.find('.streaming-label').text()).toBe('正在输出…')
  })
})
