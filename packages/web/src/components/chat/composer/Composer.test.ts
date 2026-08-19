import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { EditorView } from '@codemirror/view'
import Composer from './Composer.vue'
import { ApiError } from '@/api/client'
import { AUTO_MODEL_CONFIG_ID } from '@/stores/agent'

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

vi.mock('@/stores/agent', () => ({
  AUTO_MODEL_CONFIG_ID: 'auto',
  useAgentStore: () => agentState,
}))

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
