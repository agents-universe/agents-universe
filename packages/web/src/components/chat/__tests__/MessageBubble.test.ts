import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useAgentStore } from '@/stores/agent'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import type { AgentInfo, Message } from '@/types'

const po: AgentInfo = {
  agent_id: 'a1', slug: 'project-owner', label: 'Product Owner',
  description: '', category: '', skills: [], workflows: [],
}
const techLead: AgentInfo = {
  agent_id: 'a2', slug: 'tech-lead', label: 'Tech Lead',
  description: '', category: '', skills: [], workflows: [],
}

function bubble(message: Partial<Message> & Pick<Message, 'role'>) {
  setActivePinia(createPinia())
  const agentStore = useAgentStore()
  agentStore.setAgents([po, techLead])
  agentStore.setCurrentAgent(po)
  return mount(MessageBubble, {
    props: {
      message: {
        id: 'm1', content: 'demo', timestamp: Date.now(), ...message,
      } as Message,
    },
  })
}

describe('MessageBubble — attribution badge', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it('badges an assistant reply from the CURRENT agent (always attributed)', () => {
    const wrapper = bubble({ role: 'assistant', agentSlug: 'project-owner' })
    expect(wrapper.find('.collab-agent-badge').text()).toContain('Product Owner')
  })

  it('badges an assistant reply from a mentioned agent', () => {
    const wrapper = bubble({ role: 'assistant', agentSlug: 'tech-lead' })
    expect(wrapper.find('.collab-agent-badge').text()).toContain('Tech Lead')
  })

  it('falls back to the slug when the agent is unknown to the store', () => {
    const wrapper = bubble({ role: 'assistant', agentSlug: 'mystery-agent' })
    expect(wrapper.find('.collab-agent-badge').text()).toContain('mystery-agent')
  })

  it('shows no badge when agentSlug is missing (legacy rows)', () => {
    const wrapper = bubble({ role: 'assistant' })
    expect(wrapper.find('.collab-agent-badge').exists()).toBe(false)
  })

  it('keeps user turns to the default agent unbadged', () => {
    const wrapper = bubble({ role: 'user', agentSlug: 'project-owner' })
    expect(wrapper.find('.collab-agent-badge').exists()).toBe(false)
  })

  it('badges a user turn @-mentioned to another agent with "sent to"', () => {
    const wrapper = bubble({ role: 'user', agentSlug: 'tech-lead' })
    expect(wrapper.find('.collab-agent-badge').text()).toContain('发送给 Tech Lead')
  })
})
