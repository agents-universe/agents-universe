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
  it('renders label, description, skills and workflows', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent } })
    const text = wrapper.text()
    expect(text).toContain('QA Agent')
    expect(text).toContain('Finds bugs')
    expect(text).toContain('code-review')
    expect(text).toContain('Reviews code')
    expect(text).toContain('test-plan')
    expect(text).toContain('Plans tests')
  })

  it('renders static tools but excludes mcp markers from the tools section', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent } })
    expect(wrapper.text()).toContain('read_file')
    // 'mcp:github' appears only inside the MCP section as the bare slug
    expect(wrapper.text()).not.toContain('mcp:github')
  })

  it('renders the MCP section with a badge', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent } })
    const mcpItem = wrapper.find('.agent-tooltip__item:has(.mcp-badge)')
    expect(mcpItem.exists()).toBe(true)
    expect(mcpItem.text()).toContain('github')
  })

  it('omits empty sections', () => {
    const bare = mount(AgentCapabilitiesCard, {
      props: { agent: { ...agent, skills: [], workflows: [], tools: undefined } },
    })
    // Header only — no section headings at all
    expect(bare.findAll('.agent-tooltip__section')).toHaveLength(0)
    expect(bare.text()).toContain('QA Agent')
  })

  it('renders nothing for a null agent', () => {
    const wrapper = mount(AgentCapabilitiesCard, { props: { agent: null } })
    expect(wrapper.find('.agent-capabilities-card').exists()).toBe(false)
  })
})
