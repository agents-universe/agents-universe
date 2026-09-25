/**
 * Monotonic sequence guarding the "restore the latest conversation" requests
 * (ChatPage's getLatest / startChat) against stale responses.
 *
 * Lives outside ChatPage so non-page components (the conversation panel's
 * cross-agent jump) can invalidate an in-flight restore without importing
 * the page component — importing ChatPage would drag ChatPanel, CodeMirror
 * and markdown-it into those components' test runs.
 *
 * The loading flag lives here for the same reason: a page-local flag was
 * only ever released by claims whose seq stayed current, but invalidation
 * comes from AppLayout's new-chat button and the conversation tree — so a
 * discarded in-flight restore left `loading` stuck true forever (dead-end
 * spinner on the empty state, startChat permanently blocked). Claiming a
 * seq claims the flag; invalidation releases it.
 */
import { ref } from 'vue'

let latestSeq = 0

/** True while the newest seq claim's async work is still pending. */
export const latestLoading = ref(false)

/** Claim the next sequence number; only the newest claim is "current".
 *  A claim owns `latestLoading` until it is released or invalidated. */
export function nextLatestSeq(): number {
  latestLoading.value = true
  return ++latestSeq
}

/** True while `seq` is still the newest claim (its response may be applied). */
export function isCurrentLatestSeq(seq: number): boolean {
  return seq === latestSeq
}

/** Release a finished claim's flag — no-op when a newer claim owns it. */
export function releaseLatestSeq(seq: number): void {
  if (seq === latestSeq) latestLoading.value = false
}

/**
 * Discard every in-flight restore. Used when the user's intent (a fresh chat,
 * or a conversation picked from search results) must win over whatever the
 * async latest-conversation lookup is about to paint.
 */
export function invalidateLatestConversation(): void {
  latestSeq++
  latestLoading.value = false
}
