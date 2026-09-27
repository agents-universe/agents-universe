import { describe, it, expect, beforeEach } from 'vitest'
import { pendingDraft, applyComposerDraft, takeComposerDraft } from '../useComposerDraft'

describe('useComposerDraft', () => {
  beforeEach(() => {
    takeComposerDraft() // drain any staged value between tests
  })

  it('stages a draft and consumes it exactly once', () => {
    applyComposerDraft('Do the thing')
    expect(pendingDraft.value).toBe('Do the thing')
    expect(takeComposerDraft()).toBe('Do the thing')
    expect(takeComposerDraft()).toBeNull()
    expect(pendingDraft.value).toBeNull()
  })

  it('returns null when nothing is staged', () => {
    expect(takeComposerDraft()).toBeNull()
  })

  it('an empty string is a valid staged value', () => {
    applyComposerDraft('')
    expect(takeComposerDraft()).toBe('')
  })
})
