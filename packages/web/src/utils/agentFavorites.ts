/**
 * Category → default agents for the sidebar.
 *
 * Keys are PROJECT categories (knowledge/categories.yaml). Values are global
 * AGENT slugs (agents/*.agent.md) — not to be confused with `AgentInfo.category`
 * (agile-development / platform-assistant / security / customer-service /
 * data-analysis / office-docs), which is a different taxonomy that happens to
 * share two slugs.
 *
 * A project's favorites resolve to `defaults(category) − removed + added`, and
 * only the deltas are persisted, so editing this table also reaches existing
 * projects — except for the agents a user added or removed themselves.
 */
export const DEFAULT_AGENT_SLUGS_BY_PROJECT_CATEGORY: Record<string, string[]> = {
  'software': ['project-owner', 'tech-lead', 'quality-assurance'],
  'data-analysis': ['data-analyst', 'office-assistant'],
  'customer-service': ['customer-service', 'office-assistant'],
  'docs': ['office-assistant', 'project-customization-expert'],
  'other': ['project-customization-expert'],
}

/** Unknown or absent category → no defaults. Returns a copy, so a caller can
 *  never mutate the table above. */
export function defaultFavoriteAgentSlugs(category: string | null | undefined): string[] {
  return category ? [...(DEFAULT_AGENT_SLUGS_BY_PROJECT_CATEGORY[category] ?? [])] : []
}
