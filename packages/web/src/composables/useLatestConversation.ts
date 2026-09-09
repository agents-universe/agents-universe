/**
 * Monotonic sequence guarding the "restore the latest conversation" requests
 * (ChatPage's getLatest / startChat) against stale responses.
 *
 * Lives outside ChatPage so non-page components (the conversation panel's
 * cross-agent jump) can invalidate an in-flight restore without importing the
 * page component — importing ChatPage would drag ChatPanel, CodeMirror and
 * markdown-it into those components' test runs.
 */
let latestSeq = 0

/** Claim the next sequence number; only the newest claim is "current". */
export function nextLatestSeq(): number {
  return ++latestSeq
}

/** True while `seq` is still the newest claim (its response may be applied). */
export function isCurrentLatestSeq(seq: number): boolean {
  return seq === latestSeq
}

/**
 * Discard every in-flight restore. Used when the user's intent (a fresh chat,
 * or a conversation picked from search results) must win over whatever the
 * async latest-conversation lookup is about to paint.
 */
export function invalidateLatestConversation(): void {
  latestSeq++
}
