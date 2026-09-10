import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { closeAllConnections, closeConnection, useWebSocket, _failedConversations, MAX_RETRIES } from './useWebSocket'

const conversationsApi = vi.hoisted(() => ({
  getMessages: vi.fn(),
  getTasks: vi.fn(),
  getLatestRun: vi.fn(),
}))
vi.mock('@/api/conversations', () => ({ conversationsApi }))

/**
 * Drive the real onmessage → _dispatch path with a stubbed WebSocket.
 * The watcher is immediate, so mounting with a conversation id synchronously
 * creates the connection and registers the fake instance.
 */
class FakeWebSocket {
  static OPEN = 1
  readyState: number = 0
  onopen: ((e: Event) => void) | null = null
  onclose: ((e: Event) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  send(_data: string) {}
  close() { this.readyState = 3 }
  constructor() { instances.push(this) }
}

let instances: FakeWebSocket[] = []

function fire(ws: FakeWebSocket, payload: unknown) {
  ws.onmessage!({ data: JSON.stringify(payload) } as MessageEvent)
}

function mount(convId: string) {
  const s = ref(convId)
  const api = useWebSocket(s)
  return { ws: instances[instances.length - 1], ...api }
}

describe('useWebSocket image/file output payload guards', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    localStorage.clear()
    instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    conversationsApi.getMessages.mockResolvedValue([])
    conversationsApi.getTasks.mockResolvedValue([])
    conversationsApi.getLatestRun.mockResolvedValue(null)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('malformed image/file payloads do not crash or pollute the store', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    // Missing keys, explicit null, and non-array values must all be skipped
    // — the old code passed undefined straight into push(...images).
    fire(ws, { type: 'image_output' })
    fire(ws, { type: 'image_output', images: null })
    fire(ws, { type: 'image_output', images: 'not-an-array' })
    fire(ws, { type: 'file_output' })
    fire(ws, { type: 'file_output', files: null })

    expect(store.streamingImages).toHaveLength(0)
    // pushStreamingMessage skips empty snapshots entirely — append a draft so
    // the snapshot proceeds and we can assert nothing leaked into it.
    store.appendDelta('draft', undefined, 'conv-a')
    store.pushStreamingMessage('assistant-malformed', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].images).toBeUndefined()
    expect(store.messages[0].attachments).toBeUndefined()
  })

  it('closeConnection purges a conversation\'s failed tombstone', () => {
    const store = useConversationStore()
    store.startConversation('conv-fail-a')
    const { ws } = mount('conv-fail-a')

    // Drive the failure path to exhaustion: the fake socket never opens
    // (readyState stays 0), so each retry closes with onclose firing;
    // _scheduleRetry re-arms _open and the give-up branch marks the id
    // failed after MAX_RETRIES. Advance timers to run the retry ladder.
    for (let i = 0; i < 5; i++) {
      ws.onclose!({} as CloseEvent)
      vi.advanceTimersByTime(10_000)
    }

    expect(_failedConversations.has('conv-fail-a')).toBe(true)

    // Deleting the conversation calls closeConnection — without the fix the
    // tombstone survives forever (the set grows one entry per deleted
    // conversation).
    closeConnection('conv-fail-a')
    expect(_failedConversations.has('conv-fail-a')).toBe(false)
  })

  it('keeps retrying after the fast ladder is exhausted', () => {
    // Regression: the ladder used to stop at MAX_RETRIES and delete the
    // entry. connect() is only reachable from the conversationId watch, so a
    // lingering outage (reverse-proxy restart, sleep, captive portal) left
    // the conversation permanently dead and every send failed with
    // "WebSocket 未连接" until the user switched away and back.
    useConversationStore().startConversation('conv-slow')
    mount('conv-slow')

    // Persistent outage: every attempt fails outright, for 8 rounds — well
    // past the 1s/2s/4s fast phase, deep into the 8s/15s/30s slow one.
    const OUTAGE_ROUNDS = 8
    for (let i = 0; i < OUTAGE_ROUNDS; i++) {
      instances[instances.length - 1].onclose!({} as CloseEvent)
      vi.advanceTimersByTime(60_000)
    }

    // One initial attempt plus one per round. The old code stopped at
    // MAX_RETRIES, so anything beyond that proves the ladder no longer
    // terminates.
    expect(instances.length).toBe(1 + OUTAGE_ROUNDS)
    expect(instances.length).toBeGreaterThan(1 + MAX_RETRIES)
  })

  it('reconnect() forces a fresh attempt while degraded', () => {
    useConversationStore().startConversation('conv-manual')
    const { ws, reconnect, status } = mount('conv-manual')
    for (let i = 0; i < 4; i++) {
      ws.onclose!({} as CloseEvent)
      vi.advanceTimersByTime(10_000)
    }
    expect(status.value).toBe('failed')

    const openedBefore = instances.length
    expect(reconnect()).toBe(true)
    expect(instances.length).toBe(openedBefore + 1)
    // Back to a fresh ladder, so the banner stops claiming a lost connection.
    expect(status.value).toBe('connecting')
  })

  it('closeAllConnections clears every failed tombstone', () => {
    const store = useConversationStore()
    store.startConversation('conv-fail-b')
    const { ws } = mount('conv-fail-b')
    for (let i = 0; i < 5; i++) {
      ws.onclose!({} as CloseEvent)
      vi.advanceTimersByTime(10_000)
    }

    expect(_failedConversations.has('conv-fail-b')).toBe(true)

    // A project/agent switch closes all connections — tombstones must not
    // survive across contexts.
    closeAllConnections()
    expect(_failedConversations.size).toBe(0)
  })

  it('reloads persisted history on conversation_updated', async () => {
    // Scheduled-task delivery writes a message server-side with no stream
    // events; the socket only gets this nudge, so history must be re-fetched.
    const store = useConversationStore()
    store.startConversation('conv-sched')
    const { ws } = mount('conv-sched')

    fire(ws, { type: 'conversation_updated' })
    await flushPromises()

    expect(conversationsApi.getMessages).toHaveBeenCalledWith('conv-sched')
  })

  it('well-formed payloads still dispatch into the runtime', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    fire(ws, {
      type: 'image_output',
      images: [{ id: 'i1', url: '/api/media/p/c/shot.png', alt: 'shot' }],
    })
    fire(ws, {
      type: 'file_output',
      files: [{
        id: 'f1',
        url: '/api/media/p/c/data.json',
        name: 'data.json',
        media_type: 'application/json',
        size: 2,
      }],
    })

    expect(store.streamingImages).toHaveLength(1)
    store.pushStreamingMessage('assistant-ok', undefined, false, 'conv-a')
    expect(store.messages[0].images).toHaveLength(1)
    expect(store.messages[0].attachments).toHaveLength(1)
  })
})
