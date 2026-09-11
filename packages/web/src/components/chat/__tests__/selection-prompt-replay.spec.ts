import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useWebSocket, closeAllConnections } from '@/composables/useWebSocket'

// Regression: "和智能体对话出现确认框时，切换到别的智能体再切回来，对话框看不见
// 了，但是对话还在等待用户输入"
//
// Root cause: the dialog lives only in the per-conversation runtime and in the
// live server session — a user_confirm writes no DB row. Switching agents runs
// closeAllConnections() + convStore.reset() (runtimes.clear()), and switching
// back rebuilds the conversation from message history, which has no trace of
// the prompt. The server session was still blocked on its Future.
//
// Fix: the WS `sync` event (sent on every connect when a session is live)
// now replays the prompts still awaiting input; applySync restores them.

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 1
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  onerror: ((e: unknown) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send() { /* no-op */ }
  close() { this.readyState = 3 }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

function lastSocket(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1)
  if (!ws) throw new Error('no WebSocket created')
  return ws
}

const PROMPT_EVENT = {
  type: 'user_selection_required',
  prompt_id: 'prompt-1',
  field_key: 'deploy_target',
  question: 'Which environment?',
  options: [{ label: 'dev', value: 'dev' }],
  allow_other: true,
  kind: 'selection',
}

const PROMPT_SYNC = {
  prompt_id: 'prompt-1',
  field_key: 'deploy_target',
  question: 'Which environment?',
  options: [{ label: 'dev', value: 'dev' }],
  allow_other: true,
  kind: 'selection',
}

describe('pending prompt replay on reconnect', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })
  afterEach(() => {
    closeAllConnections()
    vi.unstubAllGlobals()
  })

  it('restores the dialog after an agent switch wiped the runtime', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    useWebSocket(ref('c1'))
    lastSocket().emit(PROMPT_EVENT)
    expect(store.pendingPrompts.map((p) => p.promptId)).toEqual(['prompt-1'])

    // Agent switch: ChatPage closes every socket and clears every runtime.
    closeAllConnections()
    store.reset()
    expect(store.conversationId).toBeNull()
    expect(store.pendingPrompts).toEqual([])

    // Switch back: the conversation (and its history) reload, then the new
    // socket's sync frame replays the prompt the agent is still blocked on.
    store.startConversation('c1')
    store.loadHistory(
      [{ message_id: 'm1', role: 'user', content: 'deploy it', created_at: new Date(1).toISOString() } as never],
      'c1',
    )
    useWebSocket(ref('c1'))
    lastSocket().emit({
      type: 'sync',
      streaming_text: '',
      // The user_confirm call is still "running" in the server snapshot — the
      // tool returns only once the prompt is answered.
      tool_calls: [{ call_id: 'call-1', tool: 'user_confirm', input: {}, status: 'running' }],
      prompts: [PROMPT_SYNC],
    })

    expect(store.pendingPrompts.length).toBe(1)
    expect(store.pendingPrompts[0].promptId).toBe('prompt-1')
    expect(store.pendingPrompts[0].question).toBe('Which environment?')
    expect(store.pendingPrompts[0].options).toEqual([{ label: 'dev', value: 'dev' }])
    // The turn is alive and blocked on the user — the composer must offer the
    // inject path, not start a new turn on a claimed conversation.
    expect(store.isStreaming).toBe(true)
    // Same rendering as the live dialog: the card is settled, not spinning.
    expect(store.activeToolCalls[0].status).toBe('done')
  })

  it('does not stack a second dialog when the reconnect repeats a prompt already on screen', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    useWebSocket(ref('c1'))
    lastSocket().emit(PROMPT_EVENT)

    // Socket dropped mid-prompt and came back: the dialog never left the UI.
    lastSocket().emit({ type: 'sync', streaming_text: '', tool_calls: [], prompts: [PROMPT_SYNC] })

    expect(store.pendingPrompts.length).toBe(1)
  })

  it('an empty sync leaves prompts and the streaming flag untouched', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    useWebSocket(ref('c1'))

    lastSocket().emit({ type: 'sync', streaming_text: '', tool_calls: [], prompts: [] })

    expect(store.pendingPrompts).toEqual([])
    // Empty sync means the server turn is gone — re-arming "running" here
    // would pin the conversation in the tree forever.
    expect(store.isStreaming).toBe(false)
  })

  it('a resolved prompt is dropped by a later sync replay', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    useWebSocket(ref('c1'))
    lastSocket().emit(PROMPT_EVENT)

    store.resolvePrompt('prompt-1', 'c1')
    // The server session's snapshot is authoritative for what is STILL
    // pending: a sync without the prompt must not resurrect the dialog.
    lastSocket().emit({ type: 'sync', streaming_text: 'working…', tool_calls: [], prompts: [] })

    expect(store.pendingPrompts).toEqual([])
  })
})
