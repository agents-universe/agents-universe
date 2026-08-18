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
