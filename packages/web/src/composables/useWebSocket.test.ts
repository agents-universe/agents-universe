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

describe('useWebSocket thinking and turn_status dispatch', () => {
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

  it('routes thinking_delta/thinking_end into the live block', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    fire(ws, { type: 'thinking_delta', delta: 'let me ', message_id: 'm1' })
    fire(ws, { type: 'thinking_delta', delta: 'think', message_id: 'm1' })
    expect(store.streamingThinking).toBe('let me think')
    expect(store.thinkingOpen).toBe(true)

    fire(ws, { type: 'thinking_end', message_id: 'm1' })
    expect(store.thinkingOpen).toBe(false)
    expect(store.streamingThinking).toBe('let me think')
  })

  it('turn_status arms the phase; heartbeats cannot clobber or resurrect it', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    fire(ws, { type: 'turn_status', phase: 'waiting_model' })
    expect(store.turnPhase).toBe('waiting_model')
    expect(store.isStreaming).toBe(true)

    // A heartbeat re-sends a (possibly stale) phase — it must only refresh
    // the freshness clock, never overwrite the fresher live frame.
    fire(ws, { type: 'turn_status', phase: 'thinking', heartbeat: true })
    expect(store.turnPhase).toBe('waiting_model')
    expect(store.turnPhaseAt).toBeGreaterThan(0)

    // After the turn wound down, a straggler heartbeat is a no-op.
    store.stopStreaming('conv-a')
    fire(ws, { type: 'turn_status', phase: 'thinking', heartbeat: true })
    expect(store.turnPhase).toBeNull()
    expect(store.isStreaming).toBe(false)
  })

  it('sync replays the streaming thinking and phase', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    fire(ws, {
      type: 'sync',
      streaming_text: '',
      thinking: 'replayed trace',
      phase: 'running_tool',
      tool_calls: [],
      prompts: [],
    })

    expect(store.streamingThinking).toBe('replayed trace')
    expect(store.turnPhase).toBe('running_tool')
    expect(store.isStreaming).toBe(true)
  })
})

describe('token_update context occupancy', () => {
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

  it('sets occupancy when the payload carries context fields', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')

    fire(ws, {
      type: 'token_update',
      used: 120_000,
      budget: 128_000,
      context_tokens: 18_400,
      context_window: 200_000,
    })

    expect(store.tokensUsed).toBe(120_000)
    expect(store.contextTokens).toBe(18_400)
    expect(store.contextWindow).toBe(200_000)
  })

  it('legacy payload without context fields leaves occupancy untouched', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setContextOccupancy(5_000, 100_000)
    const { ws } = mount('conv-a')

    fire(ws, { type: 'token_update', used: 42_000, budget: 128_000 })

    // Billing still updates; the meter keeps its last known occupancy.
    expect(store.tokensUsed).toBe(42_000)
    expect(store.contextTokens).toBe(5_000)
    expect(store.contextWindow).toBe(100_000)
  })

  it('routes occupancy to a background conversation via convId', () => {
    const store = useConversationStore()
    store.startConversation('conv-b')
    // Background connection: the socket's convId (not the message body)
    // decides which runtime receives the figures.
    const { ws } = mount('conv-a')

    fire(ws, {
      type: 'token_update',
      used: 1_000,
      budget: 128_000,
      context_tokens: 700,
      context_window: 64_000,
    })

    expect(store.contextTokens).toBe(0)
    store.startConversation('conv-a')
    expect(store.contextTokens).toBe(700)
    expect(store.contextWindow).toBe(64_000)
  })
})

describe('useWebSocket error frame settlement', () => {
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

  it('turn-alive errors keep streaming state and only add the bubble', () => {
    // handlers.py answers stale/duplicate requests (no pending prompt, no
    // active session, access recheck) WHILE THE TURN KEEPS RUNNING —
    // clearing streaming state here wiped the live turn's content and
    // stopped the Stop button while the agent was still producing output.
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')
    fire(ws, { type: 'stream_delta', delta: 'partial answer' })

    fire(ws, { type: 'error', message: 'No pending prompt for that prompt_id' })

    expect(store.streamingContent).toBe('partial answer')
    expect(store.isStreaming).toBe(true)
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].isError).toBe(true)
    expect(store.messages[0].content).toContain('No pending prompt')
  })

  it('terminal errors still settle the turn', () => {
    // Early turn-death errors (agent_turn._send_turn_error: validation
    // failures before the session starts) flag `terminal` — nothing will
    // ever stream again, so the runtime must wind down or the Stop button
    // spins forever and pending injections stay stuck.
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')
    fire(ws, { type: 'stream_delta', delta: 'partial' })

    fire(ws, { type: 'error', message: 'No model configured.', terminal: true })

    expect(store.isStreaming).toBe(false)
    expect(store.streamingContent).toBe('')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].isError).toBe(true)
  })

  it('errors carrying stream_message_id settle and dedupe the paired stream_end', () => {
    // Mid-turn failures (agent_turn run_turn crash path) send an error with
    // stream_message_id immediately followed by a stream_end for the same
    // id — the settlement must happen on the error, and the stream_end must
    // not push a second bubble for the same message.
    const store = useConversationStore()
    store.startConversation('conv-a')
    const { ws } = mount('conv-a')
    fire(ws, { type: 'stream_delta', delta: 'partial' })

    fire(ws, { type: 'error', message: 'Provider exploded.', stream_message_id: 'e1' })
    expect(store.isStreaming).toBe(false)
    expect(store.messages).toHaveLength(1)

    fire(ws, { type: 'stream_end', message_id: 'e1' })
    expect(store.messages).toHaveLength(1)
  })
})
