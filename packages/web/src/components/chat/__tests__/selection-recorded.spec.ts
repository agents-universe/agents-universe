import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useWebSocket } from '@/composables/useWebSocket'

// ── Fake WebSocket to drive useWebSocket._dispatch from tests ──────

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 0
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  sent: string[] = []

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

function lastSocket(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1)
  if (!ws) throw new Error('no WebSocket created')
  return ws
}

describe('selection answer recording — conversation store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
  })

  it('recordSelectionAnswer appends the confirmation as a user message', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.addMessage({ id: 'a-1', role: 'assistant', content: 'working...', timestamp: 1 })

    store.recordSelectionAnswer('sel-1', '【确认】Which environment?：dev', 'c1')

    const msg = store.messages.at(-1)!
    expect(msg.id).toBe('sel-1')
    expect(msg.role).toBe('user')
    expect(msg.content).toBe('【确认】Which environment?：dev')
  })

  it('recordSelectionAnswer dedupes a broadcast replay by id', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    store.recordSelectionAnswer('sel-1', '【确认】q：a', 'c1')
    store.recordSelectionAnswer('sel-1', '【确认】q：a', 'c1')

    expect(store.messages).toHaveLength(1)
  })
})

describe('selection answer recording — websocket dispatch', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('routes user_selection_recorded to a new user message', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({
      type: 'user_selection_recorded',
      message_id: 'msg-1',
      prompt_id: 'prompt-1',
      content: '【确认】Which environment?：dev',
      sequence_num: 2,
    })

    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('user')
    expect(store.messages[0].content).toBe('【确认】Which environment?：dev')
  })

  it('a duplicate frame appends only once', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    useWebSocket(ref('c1'))
    const frame = {
      type: 'user_selection_recorded',
      message_id: 'msg-1',
      prompt_id: 'prompt-1',
      content: '【确认】q：a',
      sequence_num: 2,
    }
    lastSocket().emit(frame)
    lastSocket().emit(frame)

    expect(store.messages).toHaveLength(1)
  })
})
