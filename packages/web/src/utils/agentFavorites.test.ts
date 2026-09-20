import { describe, it, expect } from 'vitest'
import { DEFAULT_AGENT_SLUGS_BY_PROJECT_CATEGORY, defaultFavoriteAgentSlugs } from './agentFavorites'

describe('DEFAULT_AGENT_SLUGS_BY_PROJECT_CATEGORY', () => {
  it('covers every project category with the intended agents', () => {
    // A typo here would silently drop one default from the sidebar of every
    // project in that category, so assert the table as a whole.
    expect(DEFAULT_AGENT_SLUGS_BY_PROJECT_CATEGORY).toEqual({
      'software': ['project-owner', 'tech-lead', 'quality-assurance'],
      'data-analysis': ['data-analyst', 'office-assistant'],
      'customer-service': ['customer-service', 'office-assistant'],
      'docs': ['office-assistant', 'project-customization-expert'],
      'other': ['project-customization-expert'],
    })
  })
})

describe('defaultFavoriteAgentSlugs', () => {
  it('returns the category defaults', () => {
    expect(defaultFavoriteAgentSlugs('software')).toEqual([
      'project-owner', 'tech-lead', 'quality-assurance',
    ])
  })

  it('returns nothing for a missing or unknown category', () => {
    // An unknown category must not fall back to another category's agents.
    expect(defaultFavoriteAgentSlugs(null)).toEqual([])
    expect(defaultFavoriteAgentSlugs(undefined)).toEqual([])
    expect(defaultFavoriteAgentSlugs('')).toEqual([])
    expect(defaultFavoriteAgentSlugs('unknown-category')).toEqual([])
  })

  it('returns a copy that cannot mutate the table', () => {
    const slugs = defaultFavoriteAgentSlugs('docs')
    slugs.push('pentest-expert')
    expect(defaultFavoriteAgentSlugs('docs')).toEqual(['office-assistant', 'project-customization-expert'])
  })
})
