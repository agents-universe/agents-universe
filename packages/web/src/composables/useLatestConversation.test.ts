import { describe, expect, it } from 'vitest'
import {
  invalidateLatestConversation,
  isCurrentLatestSeq,
  latestLoading,
  nextLatestSeq,
  releaseLatestSeq,
} from './useLatestConversation'

// The loading flag lives with the seq guard so invalidators (AppLayout's
// new-chat, the conversation tree's cross-agent pick) can release it —
// a page-local flag stayed true forever when its run was discarded without
// a successor (stuck spinner, startChat permanently blocked).
describe('useLatestConversation loading ownership', () => {
  it('invalidate releases the flag; the discarded claim cannot resurrect it', () => {
    const seq = nextLatestSeq()
    expect(latestLoading.value).toBe(true)

    invalidateLatestConversation()
    expect(latestLoading.value).toBe(false)

    // The abandoned claim finishing later must not flip the flag back on.
    releaseLatestSeq(seq)
    expect(latestLoading.value).toBe(false)
    expect(isCurrentLatestSeq(seq)).toBe(false)
  })

  it('only the newest claim releases the flag', () => {
    const first = nextLatestSeq()
    const second = nextLatestSeq()
    expect(latestLoading.value).toBe(true)

    // Superseded: the second claim owns the flag until it finishes.
    releaseLatestSeq(first)
    expect(latestLoading.value).toBe(true)

    releaseLatestSeq(second)
    expect(latestLoading.value).toBe(false)
  })
})
