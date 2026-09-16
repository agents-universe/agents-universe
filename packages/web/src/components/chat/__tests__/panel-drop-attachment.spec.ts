/**
 * Dragging a file onto the chat panel must attach it.
 *
 * Only the CodeMirror editor used to accept drops: dropping on the message
 * list did nothing at all — and, the drop not being cancelled, the browser
 * navigated away to the file and blew up the session. The whole panel is a
 * drop target now, and the hint must come and go with the drag.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import ChatPanel from '@/components/chat/ChatPanel.vue'
import { useProjectStore } from '@/stores/project'
import type { Project } from '@/types'

// The panel's socket is not under test — and a real one would try to connect.
vi.mock('@/composables/useWebSocket', async () => {
  const { ref } = await import('vue')
  return {
    useWebSocket: () => ({
      send: vi.fn(() => true),
      abort: vi.fn(() => true),
      status: ref('connected'),
      reconnect: vi.fn(),
    }),
    closeAllConnections: vi.fn(),
  }
})

const agentState = vi.hoisted(() => ({
  modelConfigs: [] as Array<{ config_id: string; model_id: string; is_system: boolean }>,
  selectedConfigId: null as string | null,
  fetchModelConfigs: vi.fn(),
  setSelectedConfigId: vi.fn(),
  agents: [],
  currentAgent: { slug: 'agent-1' },
}))

vi.mock('@/stores/agent', () => ({
  AUTO_MODEL_CONFIG_ID: 'auto',
  useAgentStore: () => agentState,
}))

const mediaMock = vi.hoisted(() => ({ upload: vi.fn() }))
vi.mock('@/api/media', () => ({ mediaApi: mediaMock }))

function makeFile(name = 'shot.png', type = 'image/png') {
  return new File([new Uint8Array(1024)], name, { type })
}

/** happy-dom has no DragEvent: VTU falls back to a plain Event and copies the
 *  custom `dataTransfer` onto it, which is all the handlers read. */
function dragEvent(files: File[], types: string[]) {
  return { dataTransfer: { files, types, dropEffect: '' } }
}

async function mountPanel(): Promise<VueWrapper> {
  setActivePinia(createPinia())
  useProjectStore().currentProject = { project_id: 'p-1' } as Project
  const wrapper = mount(ChatPanel, {
    props: { conversationId: 'c-1', agentSlug: 'agent-1' },
  })
  await flushPromises()
  return wrapper
}

describe('ChatPanel file drop', () => {
  beforeEach(() => {
    mediaMock.upload.mockReset()
    mediaMock.upload.mockResolvedValue({
      id: 'm-1',
      url: '/api/media/c-1/shot.png',
      name: 'shot.png',
      media_type: 'image/png',
      size: 1024,
    })
  })

  it('attaches a file dropped on the message list, and cancels the drop', async () => {
    const wrapper = await mountPanel()
    const file = makeFile()

    // Dispatched by hand (not via trigger) so the default-prevented flag — the
    // thing that stops the browser from navigating to the file — is assertable.
    const event = new Event('drop', { bubbles: true, cancelable: true })
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [file], types: ['Files'] },
    })
    wrapper.find('.messages-list').element.dispatchEvent(event)
    await flushPromises()

    expect(event.defaultPrevented).toBe(true)
    expect(mediaMock.upload).toHaveBeenCalledTimes(1)
    expect(mediaMock.upload.mock.calls[0][0]).toBe('p-1')
    expect(mediaMock.upload.mock.calls[0][1]).toBe('c-1')
    expect((mediaMock.upload.mock.calls[0][2] as File).name).toBe('shot.png')
    expect(wrapper.find('.composer-attachment').exists()).toBe(true)
    expect(wrapper.find('.composer-attachment').classes()).toContain('status-ready')
  })

  it('shows the drop hint while a file drag is over the panel and hides it after', async () => {
    const wrapper = await mountPanel()
    const panel = wrapper.find('.chat-panel')

    await panel.trigger('dragenter', dragEvent([makeFile()], ['Files']))
    await panel.trigger('dragover', dragEvent([makeFile()], ['Files']))
    expect(wrapper.find('.drop-overlay').exists()).toBe(true)
    expect(wrapper.find('.drop-overlay').text()).toContain('松开鼠标即可添加附件')

    await panel.trigger('dragleave', dragEvent([], ['Files']))
    expect(wrapper.find('.drop-overlay').exists()).toBe(false)
  })

  it('keeps the hint up while the drag crosses child elements', async () => {
    const wrapper = await mountPanel()
    const panel = wrapper.find('.chat-panel')

    await panel.trigger('dragenter', dragEvent([makeFile()], ['Files']))
    await panel.trigger('dragenter', dragEvent([makeFile()], ['Files']))
    await panel.trigger('dragleave', dragEvent([makeFile()], ['Files']))
    expect(wrapper.find('.drop-overlay').exists()).toBe(true)

    await panel.trigger('dragleave', dragEvent([makeFile()], ['Files']))
    expect(wrapper.find('.drop-overlay').exists()).toBe(false)
  })

  it('ignores text drags', async () => {
    const wrapper = await mountPanel()

    await wrapper.find('.chat-panel').trigger('dragenter', dragEvent([], ['text/plain']))
    expect(wrapper.find('.drop-overlay').exists()).toBe(false)

    await wrapper.find('.chat-panel').trigger('drop', dragEvent([], ['text/plain']))
    expect(mediaMock.upload).not.toHaveBeenCalled()
  })

  it('attaches a file dropped on the editor exactly once', async () => {
    // The editor used to run its own drop handler: with the panel handler now
    // in play, both firing would upload the same file twice.
    const wrapper = await mountPanel()

    await wrapper.find('.cm-content').trigger('drop', dragEvent([makeFile()], ['Files']))
    await flushPromises()

    expect(mediaMock.upload).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('.composer-attachment')).toHaveLength(1)
  })
})
