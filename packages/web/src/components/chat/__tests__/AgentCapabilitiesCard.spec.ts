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

  it('renders one chip per starter prompt with the i18n heading', () => {
    const wrapper = mount(AgentCapabilitiesCard, {
      props: {
        agent: {
          ...agent,
          starter_prompts: ['Review this PR for correctness, risk', 'Write a test plan'],
        },
      },
    })
    expect(wrapper.find('.agent-capabilities-prompts-label').text()).toBe(
      '你可以这样用我：',
    )
    const chips = wrapper.findAll('.agent-capabilities-chip')
    expect(chips).toHaveLength(2)
    expect(chips[0].text()).toBe('Review this PR for correctness, risk')
  })

  it('hides the prompts block when starter_prompts is missing or empty', () => {
    for (const starter_prompts of [undefined, []] as Array<string[] | undefined>) {
      const wrapper = mount(AgentCapabilitiesCard, {
        props: { agent: { ...agent, starter_prompts } },
      })
      expect(wrapper.find('.agent-capabilities-prompts').exists()).toBe(false)
      // No crash regardless — other fixtures omit the field entirely.
      expect(wrapper.find('.agent-capabilities-card').exists()).toBe(true)
    }
  })

  it('emits apply-prompt with the exact string on chip click', async () => {
    const wrapper = mount(AgentCapabilitiesCard, {
      props: { agent: { ...agent, starter_prompts: ['Do the thing'] } },
    })
    await wrapper.find('.agent-capabilities-chip').trigger('click')
    expect(wrapper.emitted('apply-prompt')).toEqual([['Do the thing']])
  })

  it('renders message-format special characters literally', () => {
    // Agent strings never pass through t() — @ { | must stay inert.
    const wrapper = mount(AgentCapabilitiesCard, {
      props: { agent: { ...agent, starter_prompts: ['a@b {c} |d|'] } },
    })
    expect(wrapper.find('.agent-capabilities-chip').text()).toBe('a@b {c} |d|')
  })
})
