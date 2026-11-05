import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import GraphCanvas from './GraphCanvas.vue'

// Cytoscape/fcose arrive as dynamic imports; a ~10-method cy stub satisfies
// rebuild()/applyFilters() for a one-node payload without a layout engine.
const cyStub = vi.hoisted(() => ({
  on: vi.fn(),
  elements: vi.fn(() => ({ remove: vi.fn() })),
  add: vi.fn(),
  batch: vi.fn((fn: () => void) => fn()),
  nodes: vi.fn(() => []),
  edges: vi.fn(() => []),
  layout: vi.fn(() => ({ run: vi.fn() })),
  fit: vi.fn(),
  destroy: vi.fn(),
}))

vi.mock('cytoscape', () => ({
  // cytoscape is callable and exposes .use for extension registration.
  default: Object.assign(vi.fn(() => cyStub), { use: vi.fn() }),
}))
vi.mock('cytoscape-fcose', () => ({ default: {} }))

const baseProps = {
  nodes: [{ id: 'a', repo: 'r', type: 'file', name: 'a.py' }],
  edges: [] as never,
  visibleTypes: new Set<string>(),
  visibleRepos: new Set<string>(),
  search: '',
  focusNode: null,
}

describe('GraphCanvas', () => {
  it('shows the init overlay until cytoscape loads and the first layout runs', async () => {
    const wrapper = mount(GraphCanvas, { props: baseProps })
    // Async onMounted has not awaited yet — overlay must be up immediately.
    expect(wrapper.find('.graph-canvas-loading').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载图谱…')

    await flushPromises()
    expect(wrapper.find('.graph-canvas-loading').exists()).toBe(false)
    expect(cyStub.layout).toHaveBeenCalled()

    wrapper.unmount()
    expect(cyStub.destroy).toHaveBeenCalled()
  })
})
