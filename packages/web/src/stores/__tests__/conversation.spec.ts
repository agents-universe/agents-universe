import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import type { DbMessage } from '@/types'

function dbMsg(overrides: Partial<DbMessage> = {}): DbMessage {
  return {
    message_id: 'm1',
    role: 'assistant',
    content: 'reply',
    tool_calls: [],
    agent_slug: null,
    model_name: null,
    images: null,
    attachments: null,
    created_at: '2026-08-25T12:00:00',
    ...overrides,
  }
}

describe('conversation store loadHistory', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('maps a persisted error flag to isError on the message', () => {
    const convStore = useConversationStore()
    convStore.startConversation('c1')
    convStore.loadHistory([dbMsg({ error: true, content: 'LLM API error: 404' })], 'c1')
    expect(convStore.messages[0].isError).toBe(true)
    expect(convStore.messages[0].content).toBe('LLM API error: 404')
  })

  it('leaves normal assistant messages unflagged', () => {
    const convStore = useConversationStore()
    convStore.startConversation('c1')
    convStore.loadHistory([dbMsg({ role: 'assistant', content: 'normal reply' })], 'c1')
    expect(convStore.messages[0].isError).toBeUndefined()
  })
})
