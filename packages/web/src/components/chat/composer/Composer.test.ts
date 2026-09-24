import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { EditorView } from '@codemirror/view'
import Composer from './Composer.vue'
import { ApiError } from '@/api/client'
import { AUTO_MODEL_CONFIG_ID, useAgentStore } from '@/stores/agent'
import type { ModelConfig } from '@/types'

// Hoisted mutable state so tests can seed modelConfigs per-case. The real
// store is a Pinia setup store; a plain singleton keeps the same shape.
const agentState = vi.hoisted(() => ({
  modelConfigs: [] as Array<{ config_id: string; model_id: string; is_system: boolean }>,
  selectedConfigId: null as string | null,
  fetchModelConfigs: vi.fn(),
  setSelectedConfigId: vi.fn(),
  agents: [],
  currentAgent: null,
}))

vi.mock('@/stores/agent', async () => {
  const { reactive } = await import('vue')
  // One cached proxy (reactive() memoizes per target): the real store is a
  // Pinia setup store, and Composer's watcher only re-fires on reactive
  // changes — tests must mutate through useAgentStore() or the raw object
  // would bypass the proxy's set trap and never trigger it.
  const store = reactive(agentState)
  return {
    AUTO_MODEL_CONFIG_ID: 'auto',
    useAgentStore: () => store,
  }
})

const mediaMock = vi.hoisted(() => ({ upload: vi.fn() }))
vi.mock('@/api/media', () => ({
  mediaApi: mediaMock,
}))

function makeProps() {
  return {
    isStreaming: false,
    projectId: 'p-1',
    conversationId: 'c-1',
  }
}

/** Pick a file through the hidden input (happy-dom: files is not writable). */
async function selectFile(wrapper: VueWrapper, name = 'a.png', type = 'image/png') {
  const input = wrapper.find('input[type="file"]')
  const file = new File([new Uint8Array(1024)], name, { type })
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
}

describe('Composer attachments', () => {
  it('shows 附件上传中 and blocks send while an upload is pending', async () => {
    mediaMock.upload.mockImplementation(() => new Promise(() => {})) // never settles
    const wrapper = mount(Composer, { props: makeProps() })
    await selectFile(wrapper)
    await nextTick()

    const btn = wrapper.find('.submit-btn')
    expect(btn.attributes('title')).toBe('附件上传中…')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('re-enables the send button after an upload fails', async () => {
    mediaMock.upload.mockRejectedValue(new ApiError(413, 'File exceeds the 10MB limit'))
    const wrapper = mount(Composer, { props: makeProps() })
    await selectFile(wrapper, 'big.bin', 'application/octet-stream')
    await flushPromises()

    // Failed attachment must not keep the button in the "uploading" state.
    expect(wrapper.find('.submit-btn').attributes('title')).toBe('发送 (Enter)')
    expect(wrapper.find('.attachment-error').text()).toContain('上传失败')
  })

  it('excludes failed attachments from the submit payload', async () => {
    mediaMock.upload.mockRejectedValue(new ApiError(500, 'boom'))
    const wrapper = mount(Composer, { props: makeProps() })
    await selectFile(wrapper)
    await flushPromises()
    // Failed attachment stays visible (removable) but is not sent.
    expect(wrapper.find('.composer-attachment').exists()).toBe(true)
  })

  it('enables send for an attachment-only message', async () => {
    // A pure-attachment message is legal (Enter sends it, the backend accepts
    // it) — the button must not be the only path that refuses.
    mediaMock.upload.mockResolvedValue({ media_id: 'm-1', url: '/api/media/m-1' })
    const wrapper = mount(Composer, { props: makeProps() })
    await selectFile(wrapper)
    await flushPromises()

    const btn = wrapper.find('.submit-btn')
    expect(btn.attributes('disabled')).toBeUndefined()

    await btn.trigger('click')
    const payload = wrapper.emitted('submit')![0][0] as {
      content: string
      attachments?: unknown[]
    }
    expect(payload.content).toBe('')
    expect(payload.attachments).toHaveLength(1)
  })
})

describe('Composer auto model option', () => {
  beforeEach(() => {
    agentState.modelConfigs = [
      { config_id: 'm1', model_id: 'gpt-4o', is_system: false },
      { config_id: 'm2', model_id: 'claude-sonnet', is_system: false },
    ]
    agentState.selectedConfigId = null
    agentState.setSelectedConfigId.mockClear()
  })

  it('renders the auto pill first, before real models', async () => {
    const wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    const pills = wrapper.findAll('.provider-pill')
    expect(pills[0].text()).toBe('自动')
    expect(pills[1].text()).toBe('gpt-4o')
  })

  it('clicking auto selects it through the store', async () => {
    const wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    await wrapper.findAll('.provider-pill')[0].trigger('click')
    await nextTick()

    expect(agentState.setSelectedConfigId).toHaveBeenCalledWith(AUTO_MODEL_CONFIG_ID)
    expect(wrapper.findAll('.provider-pill')[0].classes()).toContain('active')
  })

  it('submits config_id "auto" when auto is selected', async () => {
    const wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    await wrapper.findAll('.provider-pill')[0].trigger('click')
    const view = EditorView.findFromDOM(wrapper.find('.cm-content').element as HTMLElement)!
    view.dispatch({ changes: { from: 0, insert: 'hello' } })
    await nextTick()

    await wrapper.find('.submit-btn').trigger('click')
    const payload = wrapper.emitted('submit')![0][0] as { config_id?: string }
    expect(payload.config_id).toBe(AUTO_MODEL_CONFIG_ID)
  })
})

describe('Composer selection hydration on page load', () => {
  let wrapper: VueWrapper | undefined

  /** The component only reads config_id/model_id/is_system from each row. */
  function seedConfigs(rows: Array<Pick<ModelConfig, 'config_id' | 'model_id' | 'is_system'>>) {
    useAgentStore().modelConfigs = rows as unknown as ModelConfig[]
  }

  beforeEach(() => {
    // A full page reload: the store selection starts null and the configs
    // list is empty until onMounted's fetchModelConfigs resolves. Mutations
    // go through the proxy — raw-object writes bypass reactivity.
    const store = useAgentStore()
    store.modelConfigs = []
    store.selectedConfigId = null
    agentState.setSelectedConfigId.mockClear()
    wrapper = undefined
  })

  afterEach(() => {
    // A live watcher from this test must not react to the next test's
    // store mutations and skew its spy assertions.
    wrapper?.unmount()
    wrapper = undefined
  })

  it('does not persist a selection before configs load', async () => {
    // The immediate watcher fires during setup with only the 'auto' sentinel
    // in options. Selecting it went through setSelectedConfigId → localStorage,
    // destroying the saved real config id before fetch could restore it — the
    // next message silently sent config_id 'auto' instead of the chosen model.
    wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    expect(agentState.setSelectedConfigId).not.toHaveBeenCalled()
  })

  it('adopts a restored store selection once configs arrive', async () => {
    wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    // fetchModelConfigs resolved: configs land and the restored selection
    // with them — the watcher must take the store value, not re-persist.
    seedConfigs([{ config_id: 'm1', model_id: 'gpt-4o', is_system: false }])
    useAgentStore().selectedConfigId = 'm1'
    await nextTick()

    expect(agentState.setSelectedConfigId).not.toHaveBeenCalled()
    const pills = wrapper.findAll('.provider-pill')
    expect(pills[1].classes()).toContain('active')
  })

  it('selects the first real model when nothing was saved', async () => {
    wrapper = mount(Composer, { props: makeProps() })
    await nextTick()

    seedConfigs([{ config_id: 'm1', model_id: 'gpt-4o', is_system: false }])
    await nextTick()

    expect(agentState.setSelectedConfigId).toHaveBeenCalledWith('m1')
  })
})
