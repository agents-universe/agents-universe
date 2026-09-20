import { afterAll, beforeAll, describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ToolCallCard from './ToolCallCard.vue'
import { setLocale } from '@/i18n'
import type { ToolCallRecord } from '@/types'

function makeCall(tool: string, status: ToolCallRecord['status'] = 'done'): ToolCallRecord {
  return {
    callId: 'c1',
    tool,
    input: {},
    output: { content: 'ok' },
    status,
  }
}

function makeDelegation(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    callId: 'c2',
    tool: 'delegate_agent',
    input: {
      agent: 'demo--tech-lead',
      brief: 'Review PR #12 and report the blocking issues.',
      reason: 'I have no repository access.',
    },
    status: 'running',
    ...overrides,
  }
}

describe('ToolCallCard', () => {
  it('renders MCP badge and parsed tool name for mcp__ prefixed tools', () => {
    const wrapper = mount(ToolCallCard, {
      props: { call: makeCall('mcp__github_copilot__search_issues') },
    })
    expect(wrapper.find('.mcp-badge').text()).toBe('MCP')
    expect(wrapper.find('.tool-call-name').text()).toBe('search_issues')
    expect(wrapper.find('.mcp-server-tag').text()).toBe('github_copilot')
  })

  it('renders tool name without badge for normal tools', () => {
    const wrapper = mount(ToolCallCard, {
      props: { call: makeCall('shell') },
    })
    expect(wrapper.find('.mcp-badge').exists()).toBe(false)
    expect(wrapper.find('.tool-call-name').text()).toBe('shell')
    expect(wrapper.find('.mcp-server-tag').exists()).toBe(false)
  })

  it('handles multi-segment MCP tool names', () => {
    const wrapper = mount(ToolCallCard, {
      props: { call: makeCall('mcp__server__nested__tool_name') },
    })
    expect(wrapper.find('.mcp-badge').text()).toBe('MCP')
    expect(wrapper.find('.mcp-server-tag').text()).toBe('server')
    expect(wrapper.find('.tool-call-name').text()).toBe('nested__tool_name')
  })
})

describe('ToolCallCard — delegation', () => {
  // The app defaults to zh-CN; the labels below are asserted in English, and
  // one test switches back to prove the keys exist in both locales.
  beforeAll(() => setLocale('en-US'))
  afterAll(() => setLocale('zh-CN'))

  it('shows who was asked and what was asked of them, without expanding', () => {
    const wrapper = mount(ToolCallCard, { props: { call: makeDelegation() } })

    // The raw tool name would tell the user nothing; the target slug is the
    // conversation's agent prefix stripped down to the agent's own name.
    expect(wrapper.find('.tool-call-name').text()).toBe('→ Delegated to demo--tech-lead')
    const text = wrapper.text()
    expect(text).toContain('I have no repository access.')
    expect(text).toContain('Review PR #12 and report the blocking issues.')
    // The JSON body stays collapsed until the user asks for it.
    expect(wrapper.find('.tool-call-json').exists()).toBe(false)
  })

  it('reports the outcome once the agent came back', () => {
    const wrapper = mount(ToolCallCard, {
      props: {
        call: makeDelegation({
          status: 'done',
          output: {
            status: 'ok',
            agent: 'demo--tech-lead',
            agent_name: 'Tech Lead',
            summary: 'Found two blocking issues in the migration.',
            tokens_used: 1200,
            duration_ms: 42000,
          },
        }),
      },
    })

    // The display name replaces the slug as soon as the result carries one.
    expect(wrapper.find('.tool-call-name').text()).toBe('→ Delegated to Tech Lead')
    expect(wrapper.find('.tool-call-badge').text()).toBe('done')
    expect(wrapper.text()).toContain('Found two blocking issues in the migration.')
    expect(wrapper.find('.tool-call-meta').text()).toBe('1200 tokens · 42s')
  })

  it('marks a failed delegation as an error, not as a finished task', () => {
    const wrapper = mount(ToolCallCard, {
      props: {
        call: makeDelegation({
          status: 'error',
          output: { status: 'refused', agent: 'demo--tech-lead', error: 'unknown agent' },
        }),
      },
    })

    expect(wrapper.find('.tool-call-badge').text()).toBe('refused')
    expect(wrapper.find('.delegation-error').text()).toBe('unknown agent')
  })

  it('says nothing alarming when a stopped agent simply reported no text', () => {
    const wrapper = mount(ToolCallCard, {
      props: {
        call: makeDelegation({
          status: 'interrupted',
          output: { status: 'aborted', agent: 'demo--tech-lead' },
        }),
      },
    })

    expect(wrapper.find('.tool-call-badge').text()).toBe('stopped')
    expect(wrapper.text()).toContain('The agent stopped before reporting back.')
  })

  it('keeps the raw tool name when the target is unknown', () => {
    // A card must never be blank: if the input carries no agent there is
    // nothing to attribute, so it falls back to the ordinary rendering.
    const wrapper = mount(ToolCallCard, {
      props: { call: { ...makeDelegation(), input: {} } },
    })

    expect(wrapper.find('.tool-call-name').text()).toBe('delegate_agent')
  })

  it('renders an unknown status verbatim instead of a message path', () => {
    const wrapper = mount(ToolCallCard, {
      props: {
        call: makeDelegation({
          status: 'done',
          output: { status: 'exploded', agent: 'demo--tech-lead' },
        }),
      },
    })

    expect(wrapper.find('.tool-call-badge').text()).toBe('exploded')
  })

  it('renders the card in the active locale', () => {
    setLocale('zh-CN')
    try {
      const wrapper = mount(ToolCallCard, {
        props: {
          call: makeDelegation({
            status: 'done',
            output: { status: 'refused', agent: 'demo--tech-lead' },
          }),
        },
      })

      expect(wrapper.find('.tool-call-name').text()).toBe('→ 委派给 demo--tech-lead')
      expect(wrapper.find('.tool-call-badge').text()).toBe('已拒绝')
    } finally {
      setLocale('en-US')
    }
  })
})
