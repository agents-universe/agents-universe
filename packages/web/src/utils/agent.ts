import type { AgentInfo } from '@/types'

/** Static (non-MCP) tool names from the agent's frontmatter tools list. */
export function agentStaticTools(agent: AgentInfo | null): string[] {
  return (agent?.tools ?? []).filter(t => !t.startsWith('mcp'))
}

/** MCP server slugs parsed from mcp / mcp:<slug> markers ('mcp' alone → '(all)'). */
export function agentMcpServers(agent: AgentInfo | null): string[] {
  const tools = agent?.tools ?? []
  const servers: string[] = []
  for (const t of tools) {
    if (t === 'mcp') {
      servers.push('(all)')
    } else if (t.startsWith('mcp:')) {
      servers.push(t.slice(4))
    }
  }
  return servers
}

export function initials(label: string): string {
  const words = label.trim().split(/\s+/)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return label.slice(0, 2).toUpperCase()
}

const COLORS = ['#5b7cf6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']
export function avatarColor(slug: string): string {
  let h = 0
  for (let i = 0; i < slug.length; i++) h = (h * 31 + slug.charCodeAt(i)) & 0xffffffff
  return COLORS[Math.abs(h) % COLORS.length]
}
