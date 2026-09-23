import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ThinkingBlock from '@/components/chat/ThinkingBlock.vue'

/** v-show toggles inline `display` — jsdom's layout is always zero-sized, so
 *  offsetWidth-based visibility checks would report everything as hidden. */
function collapsed(wrapper: ReturnType<typeof mount>, sel: string): boolean {
  return (wrapper.find(sel).element as HTMLElement).style.display === 'none'
}

describe('ThinkingBlock', () => {
  it('starts expanded while live so the user can watch the trace', () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'weighing options', live: true },
    })
    expect(collapsed(wrapper, '.thinking-body')).toBe(false)
    expect(collapsed(wrapper, '.thinking-clamp')).toBe(true)
    expect(wrapper.find('.thinking-title').text()).toBe('深度思考')
    expect(wrapper.find('.thinking-live-tag').exists()).toBe(true)
  })

  it('starts collapsed for history (settled trace)', () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'old trace', live: false },
    })
    expect(collapsed(wrapper, '.thinking-body')).toBe(true)
    expect(collapsed(wrapper, '.thinking-clamp')).toBe(false)
    expect(wrapper.find('.thinking-live-tag').exists()).toBe(false)
  })

  it('defaults to collapsed when live is omitted', () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'trace' },
    })
    expect(collapsed(wrapper, '.thinking-body')).toBe(true)
  })

  it('auto-collapses the moment live flips false (thinking_end)', async () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'streaming trace', live: true },
    })
    expect(collapsed(wrapper, '.thinking-body')).toBe(false)

    await wrapper.setProps({ live: false })
    expect(collapsed(wrapper, '.thinking-body')).toBe(true)
    expect(collapsed(wrapper, '.thinking-clamp')).toBe(false)
    // The trace itself survives — collapse is presentation only.
    expect(wrapper.find('.thinking-clamp').text()).toBe('streaming trace')
  })

  it('expands again when live flips back to true', async () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'trace', live: false },
    })
    await wrapper.setProps({ live: true })
    expect(collapsed(wrapper, '.thinking-body')).toBe(false)
  })

  it('toggles on header click', async () => {
    const wrapper = mount(ThinkingBlock, {
      props: { text: 'trace', live: true },
    })
    await wrapper.find('.thinking-header').trigger('click')
    expect(collapsed(wrapper, '.thinking-body')).toBe(true)
    await wrapper.find('.thinking-header').trigger('click')
    expect(collapsed(wrapper, '.thinking-body')).toBe(false)
  })

  it('renders CoT characters verbatim as plain text (never markdown)', () => {
    // Raw braces, HTML-ish fragments and fences are common in reasoning
    // traces — markdown parsing would mangle or swallow them.
    const cot = 'use {x} <script>alert(1)</script> ```code``` & more'
    const wrapper = mount(ThinkingBlock, {
      props: { text: cot, live: true },
    })
    expect(wrapper.find('.thinking-text').text()).toBe(cot)
  })
})
