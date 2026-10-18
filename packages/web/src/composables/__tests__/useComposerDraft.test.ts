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

  it('leaves a newer draft untouched when the expected value differs', () => {
    // A superseded create clears only the chip text it staged; the user may
    // have staged a prompt for the new context by then.
    applyComposerDraft('newer prompt')
    expect(takeComposerDraft('older prompt')).toBeNull()
    expect(pendingDraft.value).toBe('newer prompt')
    expect(takeComposerDraft('newer prompt')).toBe('newer prompt')
    expect(pendingDraft.value).toBeNull()
  })
})
