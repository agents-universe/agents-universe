import { describe, expect, it } from 'vitest'

import {
  CUSTOMIZATION_EXPERT_SLUG,
  ONBOARDING_KICKOFF,
  OTHER_ONBOARDING_KICKOFF,
  buildOnboardingKickoff,
} from './onboarding'

describe('buildOnboardingKickoff', () => {
  it('returns the expert-customization kickoff for other category', () => {
    const kickoff = buildOnboardingKickoff('other')
    expect(kickoff).toBe(OTHER_ONBOARDING_KICKOFF)
    expect(kickoff).toContain('项目定制专家')
    expect(kickoff).toContain('status: not_applicable')
  })

  it('returns the standard kickoff for software category', () => {
    expect(buildOnboardingKickoff('software')).toBe(ONBOARDING_KICKOFF)
  })

  it('returns the standard kickoff for docs category', () => {
    expect(buildOnboardingKickoff('docs')).toBe(ONBOARDING_KICKOFF)
  })

  it('returns the standard kickoff for missing category', () => {
    expect(buildOnboardingKickoff(undefined)).toBe(ONBOARDING_KICKOFF)
    expect(buildOnboardingKickoff(null)).toBe(ONBOARDING_KICKOFF)
  })
})

describe('CUSTOMIZATION_EXPERT_SLUG', () => {
  it('points at the project customization expert agent', () => {
    expect(CUSTOMIZATION_EXPERT_SLUG).toBe('project-customization-expert')
  })
})
