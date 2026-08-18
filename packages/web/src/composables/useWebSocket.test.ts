import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useWebSocket } from './useWebSocket'

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
  useWebSocket(ref(convId))
  return instances[instances.length - 1]
}

describe('useWebSocket image/file output payload guards', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('malformed image/file payloads do not crash or pollute the store', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const ws = mount('conv-a')

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

  it('well-formed payloads still dispatch into the runtime', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    const ws = mount('conv-a')

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
