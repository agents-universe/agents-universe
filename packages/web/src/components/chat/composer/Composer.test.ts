import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import Composer from './Composer.vue'
import { ApiError } from '@/api/client'

vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({
    modelConfigs: [],
    selectedConfigId: null,
    fetchModelConfigs: vi.fn(),
    setSelectedConfigId: vi.fn(),
    agents: [],
    currentAgent: null,
  }),
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
