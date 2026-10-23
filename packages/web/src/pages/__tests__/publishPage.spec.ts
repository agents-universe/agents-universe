import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PublishPage from '@/pages/PublishPage.vue'
import { ApiError } from '@/api/client'
import { i18n } from '@/i18n'

const route = vi.hoisted(() => ({ params: { publishId: 'pub-1' } }))
const router = vi.hoisted(() => ({
  back: vi.fn(),
  push: vi.fn(),
  options: { history: { state: { back: null as string | null } } },
}))
vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => router }))

const publishApi = vi.hoisted(() => ({
  getPage: vi.fn(),
  getSessionMessages: vi.fn(),
  runSession: vi.fn(),
  abortSession: vi.fn(),
}))
vi.mock('@/api/publish', () => ({ publishApi }))

const SESSION = {
  publish_id: 'pub-1',
  conversation_id: 'conv-1',
  agent: { slug: 'helper', display_name: 'Helper', description: null, project_id: 'proj-1' },
  project_id: 'proj-1',
  title: null,
  description: null,
  has_conversation: true,
  token: 'viewer-token',
}

describe('PublishPage composer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    publishApi.getPage.mockResolvedValue(SESSION)
    publishApi.getSessionMessages.mockResolvedValue([])
    publishApi.runSession.mockResolvedValue('reply')
  })

  async function mountLoaded() {
    const wrapper = mount(PublishPage)
    await flushPromises()
    return wrapper
  }

  it('the publisher-model badge tooltip comes from the locale', async () => {
    // The public embed page serves anonymous visitors of any locale; the
    // tooltip must not stay hard-coded Chinese next to the localized badge.
    const wrapper = await mountLoaded()

    expect(wrapper.find('.publish-badge').attributes('title'))
      .toBe(i18n.global.t('publishPage.publisherModelBadgeHint'))

    wrapper.unmount()
  })

  it('ignores the Enter that commits an IME candidate', async () => {
    const wrapper = await mountLoaded()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('你好世界')

    // Enter while composing belongs to the IME candidate list — submitting
    // on it sent the half-composed text and cleared the still-active draft.
    const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    Object.defineProperty(ev, 'isComposing', { value: true })
    textarea.element.dispatchEvent(ev)
    await flushPromises()

    expect(publishApi.runSession).not.toHaveBeenCalled()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('你好世界')

    // A real Enter still sends.
    await textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(publishApi.runSession).toHaveBeenCalledTimes(1)
    expect(publishApi.runSession.mock.calls[0][2]).toBe('你好世界')

    wrapper.unmount()
  })

  it('rolls back the optimistic user message when the turn is rejected', async () => {
    // 409 = another run owns the conversation. The server never persisted
    // the message; keeping the bubble made a discarded instruction look sent.
    publishApi.runSession.mockRejectedValue(new ApiError(409, '该会话已有一轮运行'))
    const wrapper = await mountLoaded()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('请总结')
    await textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(wrapper.find('.publish-bubble.user').exists()).toBe(false)
    expect(wrapper.find('.publish-bubble.error').exists()).toBe(true)
    // The text returns to the composer so the user can retry.
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('请总结')

    wrapper.unmount()
  })

  it('keeps the user message when the run fails mid-stream', async () => {
    const err = new ApiError(500, '模型调用失败')
    err.partialText = '前半段'
    publishApi.runSession.mockRejectedValue(err)
    const wrapper = await mountLoaded()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('请总结')
    await textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    // The turn started and the message reached the server: the bubble stays,
    // beside the partial answer and the error.
    expect(wrapper.find('.publish-bubble.user').exists()).toBe(true)
    expect(wrapper.text()).toContain('前半段')
    expect(wrapper.find('.publish-bubble.error').exists()).toBe(true)
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')

    wrapper.unmount()
  })
})
