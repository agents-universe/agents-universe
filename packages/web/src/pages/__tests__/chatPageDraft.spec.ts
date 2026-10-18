import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ChatPage from '@/pages/ChatPage.vue'
import { applyComposerDraft, pendingDraft, takeComposerDraft } from '@/composables/useComposerDraft'
import { invalidateLatestConversation } from '@/composables/useLatestConversation'

const route = vi.hoisted(() => ({ params: { projectId: 'p-1' }, query: {} }))
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}))

const agentStore = vi.hoisted(() => ({
  currentAgent: { slug: 'helper', display_name: 'Helper' },
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

const conversationsApi = vi.hoisted(() => ({
  getLatest: vi.fn(),
  create: vi.fn(),
  getMessages: vi.fn(),
  getTasks: vi.fn(),
  getLatestRun: vi.fn(),
}))
vi.mock('@/api/conversations', () => ({ conversationsApi }))

describe('ChatPage staged chip drafts', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    takeComposerDraft()
    invalidateLatestConversation()
    conversationsApi.getLatest.mockResolvedValue(null)
    conversationsApi.getMessages.mockResolvedValue([])
    conversationsApi.getTasks.mockResolvedValue([])
    conversationsApi.getLatestRun.mockResolvedValue(null)
  })

  it('a superseded create does not discard a draft staged for the new context', async () => {
    let resolveCreate!: (v: unknown) => void
    conversationsApi.create.mockReturnValue(new Promise((r) => { resolveCreate = r }))

    const wrapper = mount(ChatPage, {
      global: { stubs: { ChatPanel: true, AgentCapabilitiesCard: true } },
    })
    await flushPromises()

    // Chip click: stages "A prompt" and starts the create.
    wrapper.findComponent({ name: 'AgentCapabilitiesCard' }).vm.$emit('apply-prompt', 'A prompt')
    await flushPromises()
    expect(conversationsApi.create).toHaveBeenCalledTimes(1)
    expect(pendingDraft.value).toBe('A prompt')

    // The user moves to a new context (superseding the create) and stages a
    // prompt there before the old request lands.
    invalidateLatestConversation()
    applyComposerDraft('B prompt')

    // The stale success must clear only its own value — this used to wipe
    // the new context's draft too.
    resolveCreate({ conversation_id: 'conv-a', token_budget: 1000 })
    await flushPromises()

    expect(pendingDraft.value).toBe('B prompt')
    wrapper.unmount()
  })
})
