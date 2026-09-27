/**
 * One-shot draft handoff from a starter-prompt chip to the composer.
 *
 * The fresh-conversation card renders in two places: ChatPage's
 * pre-conversation empty state (where no Composer exists yet — ChatPanel is
 * v-if="conversationId") and ChatPanel's zero-message state (Composer
 * mounted). A ref on the parent cannot span both, so the pending text lives
 * at module scope: `applyComposerDraft` sets it, the Composer consumes it
 * once with `takeComposerDraft` — on mount for the pre-conversation path,
 * via the pendingDraft watch when it is already mounted.
 *
 * Module-level state mirrors useLatestConversation's cross-panel pattern
 * (CLAUDE.md forbids provide/inject for cross-panel state).
 */
import { ref } from 'vue'

/** Pending chip text; null = nothing to apply. */
export const pendingDraft = ref<string | null>(null)

/** Stage a prompt for the composer to pick up. */
export function applyComposerDraft(text: string): void {
  pendingDraft.value = text
}

/** Read-and-clear the pending text (null when nothing is staged). */
export function takeComposerDraft(): string | null {
  const text = pendingDraft.value
  if (text != null) pendingDraft.value = null
  return text
}
