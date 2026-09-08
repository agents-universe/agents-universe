import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'

// Regression: "pentest-expert 的授权对话框弹出过一次以后，超时了，再对话它弹出不了了"
//
// Root cause: when the server-side prompt Future timed out (300 s) the client
// never received any cancel/resolve event — only abort_ack cleared prompts.
// The zombie dialog stayed in pendingPrompts forever; the next user_confirm
// stacked a second dialog on top (or the user clicked the stale one into a
// dead session), so it looked like the dialog "stopped appearing".
//
// Fix:
//  1. Server emits user_selection_cancelled on timeout/abort (session.py).
//  2. Client dispatches it via removePendingPrompt (useWebSocket.ts).
//  3. Defensive: stream_end/error clear every unanswered prompt.
describe('pending prompt timeout — zombie dialog cleanup', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('user_selection_cancelled removes exactly the timed-out prompt', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    store.addPendingPrompt({
      promptId: 'prompt-1',
      fieldKey: 'pentest_scope',
      question: 'Old question',
      options: [],
      allowOther: true,
      kind: 'selection',
    }, 'c1')
    store.addPendingPrompt({
      promptId: 'prompt-2',
      fieldKey: 'pentest_scope',
      question: 'New question',
      options: [],
      allowOther: true,
      kind: 'selection',
    }, 'c1')

    // Server: prompt-1 timed out → user_selection_cancelled for prompt-1 only.
    store.removePendingPrompt('prompt-1', 'c1')

    expect(store.pendingPrompts.length).toBe(1)
    expect(store.pendingPrompts[0].promptId).toBe('prompt-2')
  })

  it('after a timeout the next user_confirm dialog appears cleanly (no stacking)', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    // First dialog times out; the cancel event removes it.
    store.addPendingPrompt({
      promptId: 'prompt-1',
      fieldKey: 'pentest_scope',
      question: 'Which environment and entry points are authorized?',
      options: [{ label: 'dev', value: 'dev' }],
      allowOther: true,
      kind: 'selection',
    }, 'c1')
    store.removePendingPrompt('prompt-1', 'c1')
    expect(store.pendingPrompts.length).toBe(0)

    // User talks again → the agent calls user_confirm → only ONE dialog.
    store.addPendingPrompt({
      promptId: 'prompt-2',
      fieldKey: 'pentest_scope',
      question: 'Please confirm scope again',
      options: [{ label: 'dev', value: 'dev' }],
      allowOther: true,
      kind: 'selection',
    }, 'c1')
    expect(store.pendingPrompts.length).toBe(1)
    expect(store.pendingPrompts[0].promptId).toBe('prompt-2')
  })

  it('a user-answered prompt still resolves (existing path unchanged)', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.addPendingPrompt({
      promptId: 'prompt-3',
      fieldKey: 'pentest_scope',
      question: 'Approve?',
      options: [],
      allowOther: true,
      kind: 'selection',
    }, 'c1')

    store.resolvePrompt('prompt-3', 'c1')
    expect(store.pendingPrompts.length).toBe(0)
  })
})
