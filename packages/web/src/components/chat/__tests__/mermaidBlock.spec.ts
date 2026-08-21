import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MermaidBlock from '@/components/chat/MermaidBlock.vue'

// The component lazy-loads mermaid via `await import('mermaid')`. Mock the
// module so tests can drive both the success path and the production failure
// path (dynamic chunk fetch failure -> "Failed to fetch dynamically imported
// module: .../mermaid.core-*.js").
const mockMermaid = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(),
}))

vi.mock('mermaid', () => ({
  default: {
    initialize: mockMermaid.initialize,
    render: mockMermaid.render,
  },
}))

const SAMPLE_CODE = 'flowchart LR\n  A --> B'

describe('MermaidBlock', () => {
  beforeEach(() => {
    mockMermaid.initialize.mockClear()
    mockMermaid.render.mockReset()
  })

  it('shows raw markdown source plus the error when mermaid fails to render', async () => {
    mockMermaid.render.mockRejectedValue(
      new Error('Failed to fetch dynamically imported module: https://example.com/assets/mermaid.core-Cxx0g_91.js'),
    )
    const wrapper = mount(MermaidBlock, { props: { code: SAMPLE_CODE } })
    await flushPromises()

    const error = wrapper.find('.mermaid-error')
    expect(error.exists()).toBe(true)
    expect(wrapper.find('.mermaid-error-head').text()).toContain('图表渲染失败')
    expect(wrapper.find('.mermaid-error-msg').text()).toContain('Failed to fetch dynamically imported module')
    // The fallback exposes the original markdown so the diagram is still readable.
    expect(wrapper.find('.mermaid-source-code').text()).toContain('flowchart LR')
    expect(wrapper.find('.mermaid-source-code').text()).toContain('A --> B')
    expect(wrapper.find('.mermaid-source-label').text()).toContain('markdown')
  })

  it('renders the svg when mermaid render succeeds', async () => {
    mockMermaid.render.mockResolvedValue({ svg: '<svg id="ok-svg"><g /></svg>' })
    const wrapper = mount(MermaidBlock, { props: { code: SAMPLE_CODE } })
    await flushPromises()

    expect(wrapper.find('.mermaid-error').exists()).toBe(false)
    expect(wrapper.find('.mermaid-source-code').exists()).toBe(false)
    expect(wrapper.find('.mermaid-diagram svg').exists()).toBe(true)
  })

  it('shows empty-code error for a blank code block', async () => {
    const wrapper = mount(MermaidBlock, { props: { code: '   \n  ' } })
    await flushPromises()

    expect(wrapper.find('.mermaid-error').exists()).toBe(true)
    expect(wrapper.find('.mermaid-error-msg').text()).toContain('空的 Mermaid 代码块')
    expect(mockMermaid.render).not.toHaveBeenCalled()
  })
})
