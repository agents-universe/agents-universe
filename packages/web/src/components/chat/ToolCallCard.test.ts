import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ToolCallCard from './ToolCallCard.vue'
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
