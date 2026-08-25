import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentCapabilitiesCard from '../AgentCapabilitiesCard.vue'
import type { AgentInfo } from '@/types'

const agent: AgentInfo = {
  agent_id: 'a1',
  slug: 'qa-agent',
  label: 'QA Agent',
  description: 'Finds bugs',
  category: 'software',
  skills: [{ slug: 'code-review', description: 'Reviews code' }],
  workflows: [{ slug: 'test-plan', description: 'Plans tests' }],
  tools: ['read_file', 'mcp:github'],
}

describe('AgentCapabilitiesCard', () => {
  it('renders label and description as plain text', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent } })
    const text = wrapper.text()
    expect(text).toContain('QA Agent')
    expect(text).toContain('Finds bugs')
  })

  it('omits the description element when the agent has none', () => {
    const wrapper = mount(AgentCapabilitiesCard, {
      props: { agent: { ...agent, description: '' } },
    })
    expect(wrapper.text()).toContain('QA Agent')
    expect(wrapper.find('.agent-capabilities-desc').exists()).toBe(false)
  })

  it('renders no capability list sections (skills/workflows/tools/MCP)', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent } })
    expect(wrapper.findAll('.agent-tooltip__section')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('code-review')
    expect(wrapper.text()).not.toContain('test-plan')
    expect(wrapper.text()).not.toContain('read_file')
    expect(wrapper.text()).not.toContain('github')
  })

  it('renders nothing for a null agent', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent: null } })
    expect(wrapper.find('.agent-capabilities-card').exists()).toBe(false)
  })
})
