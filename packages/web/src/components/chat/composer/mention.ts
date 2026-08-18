/** @-mention agent routing helpers for the composer. */

export interface MentionedAgent {
  slug: string
  label: string
}

export interface MentionResolution {
  /** Slug of the single mentioned agent, when exactly one survives. */
  agentSlug?: string
  /** Set when the draft mentions more than one distinct agent. */
  error?: 'multiple'
}

/**
 * Resolve which agent a draft's @-mentions route the turn to.
 *
 * Routing relies on the selection-time map from the popup, not text parsing:
 * a hand-typed @name never went through the popup, so it stays plain text
 * (no agent switch). A mapping whose text the user deleted afterwards is
 * dropped the same way. More than one distinct surviving agent is rejected.
 */
export function resolveMentionAgent(
  content: string,
  mentioned: MentionedAgent[],
): MentionResolution {
  const valid = mentioned.filter((a) => content.includes(`@${a.label}`))
  const slugs = [...new Set(valid.map((a) => a.slug))]
  if (slugs.length > 1) return { error: 'multiple' }
  return slugs[0] ? { agentSlug: slugs[0] } : { agentSlug: undefined }
}
