import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useWebSocket } from '@/composables/useWebSocket'
import Composer from '@/components/chat/composer/Composer.vue'
import type { ToolCallRecord } from '@/types'

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

function makeComposerProps(isStreaming: boolean) {
  return { isStreaming, projectId: 'p-1', conversationId: 'c-1' }
}

describe('in-flight injection — Composer', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('keeps the send button enabled and shows stop alongside while streaming', async () => {
    const wrapper = mount(Composer, { props: makeComposerProps(true) })
    // Both buttons coexist: stop (abort) + send (inject).
    expect(wrapper.findAll('.submit-btn').length).toBe(2)
    expect(wrapper.find('.submit-btn.abort').exists()).toBe(true)
    const sendBtn = wrapper.find('.submit-btn:not(.abort)')
    expect(sendBtn.exists()).toBe(true)
    // Tooltip tells the user the message joins the running execution.
    expect(sendBtn.attributes('title')).toContain('将加入当前执行')
  })

  it('renders only the send button when idle', () => {
    const wrapper = mount(Composer, { props: makeComposerProps(false) })
    expect(wrapper.findAll('.submit-btn').length).toBe(1)
    expect(wrapper.find('.submit-btn.abort').exists()).toBe(false)
  })
})

describe('in-flight injection — conversation store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('confirmInjected replaces the optimistic id and moves the message to the end', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    // User sent "stop now" while the agent was streaming; the interrupted
    // snapshot lands after the optimistic message.
    store.registerInjectedMessage('opt-1', 'stop now')
    store.addMessage({ id: 'opt-1', role: 'user', content: 'stop now', timestamp: 1 })
    store.addMessage({ id: 'snap-1', role: 'assistant', content: 'partial...', timestamp: 2, interrupted: true })

    store.confirmInjected('srv-1', 'stop now', 'c1')

    // Replaced id + moved after the snapshot (matching the DB sequence).
    expect(store.messages.map((m) => m.id)).toEqual(['snap-1', 'srv-1'])
    expect(store.messages[1].content).toBe('stop now')
  })

  it('confirmInjected matches by content when the queued ack carried no server id', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    // Claim-window path: input_queued arrives with message_id null.
    store.registerInjectedMessage('opt-2', 'queued blind')
    store.addMessage({ id: 'opt-2', role: 'user', content: 'queued blind', timestamp: 1 })

    store.confirmInjected('srv-2', 'queued blind', 'c1')
    expect(store.messages[0].id).toBe('srv-2')
  })

  it('rejectInjected attaches the server notice to the optimistic message', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.registerInjectedMessage('opt-3', 'too long')
    store.addMessage({ id: 'opt-3', role: 'user', content: 'too long', timestamp: 1 })

    store.rejectInjected('too long', 'Message content exceeds the 200,000 character limit', 'c1')
    expect(store.messages[0].content).toContain('Message content exceeds')
  })

  it('unregisterInjectedMessage drops a failed send', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.registerInjectedMessage('opt-4', 'never left')
    store.unregisterInjectedMessage('opt-4', 'c1')
    // A later confirm for the same content must not resurrect it.
    store.confirmInjected('srv-4', 'never left', 'c1')
    expect(store.messages).toEqual([])
  })

  it('pushStreamingMessage flags interrupted and normalizes tools to interrupted', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.addToolCall({
      callId: 'call-1', tool: 'read_file', input: {}, status: 'running', taskId: undefined,
    } satisfies ToolCallRecord, 'c1')
    store.appendDelta('partial answer', undefined, 'c1')

    store.pushStreamingMessage('snap-1', undefined, false, 'c1', { interrupted: true })

    const msg = store.messages[0]
    expect(msg.interrupted).toBe(true)
    expect(msg.content).toBe('partial answer')
    // running → interrupted (not error): the user's injection cut the step.
    expect(msg.toolCalls?.[0].status).toBe('interrupted')
    expect(msg.toolCalls?.[0].output).toBeUndefined()
  })

  it('pushStreamingMessage without interrupted opts keeps error normalization', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.addToolCall({
      callId: 'call-2', tool: 'read_file', input: {}, status: 'preparing', taskId: undefined,
    } satisfies ToolCallRecord, 'c1')

    store.pushStreamingMessage('snap-2', undefined, false, 'c1')

    expect(store.messages[0].interrupted).toBeUndefined()
    expect(store.messages[0].toolCalls?.[0].status).toBe('error')
  })

  it('pushStreamingMessage interrupted keeps streaming flag alive', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.appendDelta('partial answer', undefined, 'c1')

    // The agent is still running - isStreaming must survive the snapshot.
    expect(store.isStreaming).toBe(true)
    store.pushStreamingMessage('snap-3', undefined, false, 'c1', { interrupted: true })
    expect(store.isStreaming).toBe(true)
    // Streaming content cleared (snapshotted into the message), but the
    // activity flag stays so the UI keeps its indicator and the Composer
    // keeps treating new messages as injections.
    expect(store.streamingContent).toBe('')
  })
})

describe('in-flight injection — websocket dispatch', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('routes user_message_injected to confirmInjected', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.registerInjectedMessage('opt-1', 'stop now')
    store.addMessage({ id: 'opt-1', role: 'user', content: 'stop now', timestamp: 1 })

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'user_message_injected', message_id: 'srv-1', content: 'stop now', sequence_num: 3 })

    expect(store.messages[0].id).toBe('srv-1')
  })

  it('routes input_rejected without clearing streaming state', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.registerInjectedMessage('opt-2', 'bad')
    store.addMessage({ id: 'opt-2', role: 'user', content: 'bad', timestamp: 1 })
    store.appendDelta('agent still talking', undefined, 'c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'input_rejected', message_id: null, content: 'bad', message: 'Message content exceeds the 200,000 character limit' })

    // The turn keeps streaming — only the message got the notice.
    expect(store.messages[0].content).toContain('Message content exceeds')
    expect(store.isStreaming).toBe(true)
  })

  it('stream_end interrupted snapshots partial output as an interrupted message', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.appendDelta('partial answer', undefined, 'c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'stream_end', message_id: 'snap-1', stop_reason: 'interrupted' })

    const msg = store.messages[0]
    expect(msg.interrupted).toBe(true)
    expect(msg.content).toBe('partial answer')
  })

  it('empty interrupted snapshot keeps the run active (agent continues)', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    // Agent thinking, nothing streamed yet when the injection is consumed.
    store.startThinking()

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'stream_end', message_id: 'snap-0', stop_reason: 'interrupted' })

    // Not stopped: no partial content was lost and the agent is still going
    // with the injected message.
    expect(store.isThinking).toBe(true)
    expect(store.messages).toEqual([])
  })
})
