import { describe, it, expect } from 'vitest'
import { agentStaticTools, agentMcpServers, initials, avatarColor } from './agent'
import type { AgentInfo } from '@/types'

function makeAgent(overrides: Partial<AgentInfo> = {}): AgentInfo {
  return {
    agent_id: 'a1',
    slug: 'test-agent',
    label: 'Test Agent',
    description: 'desc',
    category: 'software',
    skills: [],
    workflows: [],
    ...overrides,
  }
}

describe('agentStaticTools', () => {
  it('filters out mcp markers, keeps static tools', () => {
    const agent = makeAgent({ tools: ['read_file', 'plan_task', 'mcp', 'mcp:github'] })
    expect(agentStaticTools(agent)).toEqual(['read_file', 'plan_task'])
  })

  it('handles null/undefined tools', () => {
    expect(agentStaticTools(makeAgent())).toEqual([])
    expect(agentStaticTools(null)).toEqual([])
  })
})

describe('agentMcpServers', () => {
  it('maps bare mcp to (all)', () => {
    expect(agentMcpServers(makeAgent({ tools: ['mcp'] }))).toEqual(['(all)'])
  })

  it('maps mcp:<slug> to the slug', () => {
    expect(agentMcpServers(makeAgent({ tools: ['mcp:github'] }))).toEqual(['github'])
  })

  it('handles mixed lists in order', () => {
    const agent = makeAgent({ tools: ['mcp', 'read_file', 'mcp:github', 'mcp:slack'] })
    expect(agentMcpServers(agent)).toEqual(['(all)', 'github', 'slack'])
  })

  it('handles no tools', () => {
    expect(agentMcpServers(makeAgent())).toEqual([])
    expect(agentMcpServers(null)).toEqual([])
  })
})

describe('initials', () => {
  it('takes first letters of a multi-word label', () => {
    expect(initials('QA Expert')).toBe('QE')
    expect(initials('Test Agent')).toBe('TA')
  })

  it('falls back to the first two characters of a single word', () => {
    expect(initials('Claude')).toBe('CL')
    expect(initials('定制')).toBe('定制')
  })
})

describe('avatarColor', () => {
  it('is deterministic for the same slug', () => {
    expect(avatarColor('qa-agent')).toBe(avatarColor('qa-agent'))
    expect(avatarColor('project-expert')).toBe(avatarColor('project-expert'))
  })

  it('returns a hex color', () => {
    expect(avatarColor('qa-agent')).toMatch(/^#[0-9a-f]{6}$/i)
  })
})
