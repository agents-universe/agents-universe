import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { i18n } from '@/i18n'
import GraphPage from './GraphPage.vue'

const routeState = vi.hoisted(() => ({ params: { projectId: 'p-1' } }))
vi.mock('vue-router', () => ({ useRoute: () => routeState }))

// Cytoscape cannot mount in jsdom (no layout engine): stub the canvas out.
// The page's contract with it is props + one event, asserted via the stub.
const projectGraphApi = vi.hoisted(() => ({
  getGraph: vi.fn(),
  getNeighbors: vi.fn(),
  build: vi.fn(),
}))
vi.mock('@/api/projectGraph', () => ({ projectGraphApi }))

const GraphCanvasStub = vi.hoisted(() => ({
  name: 'GraphCanvasStub',
  props: ['nodes', 'edges', 'visibleTypes', 'visibleRepos', 'search', 'focusNode'],
  emits: ['select'],
  template: '<div class="graph-canvas-stub" />',
}))
vi.mock('@/components/graph/GraphCanvas.vue', () => ({ default: GraphCanvasStub }))

const GraphDetailPanelStub = vi.hoisted(() => ({
  name: 'GraphDetailPanelStub',
  props: ['node', 'neighbors'],
  emits: ['close', 'focus'],
  template: '<div class="graph-detail-stub" />',
}))
vi.mock('@/components/graph/GraphDetailPanel.vue', () => ({ default: GraphDetailPanelStub }))

function graphPayload(overrides: Record<string, unknown> = {}) {
  return {
    needs_build: false,
    generated_at: 1,
    level: 'overview',
    truncated: false,
    no_repos: false,
    repos: [{ name: 'api', has_graph: true }],
    counts: { nodes: 2, edges: 1, docs: 0, repos: 1 },
    nodes: [
      { id: 'api:f:src/x.py', repo: 'api', type: 'file', name: 'src/x.py' },
      { id: 'api:f:src/y.py', repo: 'api', type: 'file', name: 'src/y.py' },
    ],
    edges: [{ src: 'api:f:src/x.py', dst: 'api:f:src/y.py', type: 'imports' }],
    ...overrides,
  }
}

function mountPage() {
  return mount(GraphPage, {
    global: { plugins: [i18n], stubs: { teleport: true } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  routeState.params.projectId = 'p-1'
})

describe('GraphPage', () => {
  it('shows the build CTA when the graph needs building', async () => {
    projectGraphApi.getGraph.mockResolvedValue(
      graphPayload({ needs_build: true, nodes: [], edges: [], repos: [{ name: 'api', has_graph: false }] }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('尚未构建图谱')
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(false)

    projectGraphApi.build.mockResolvedValue({ repos: [{ name: 'api', status: 'built' }] })
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    await wrapper.find('.graph-empty button.btn-primary').trigger('click')
    await flushPromises()
    expect(projectGraphApi.build).toHaveBeenCalledWith('p-1')
    // Build re-fetches so the fresh graphs render.
    expect(projectGraphApi.getGraph).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(true)
  })

  it('shows the clone hint when the project has no repos', async () => {
    projectGraphApi.getGraph.mockResolvedValue(
      graphPayload({ no_repos: true, nodes: [], edges: [], repos: [] }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('暂无代码仓库')
  })

  it('renders repo chips and the canvas for a built graph', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()
    const chips = wrapper.findAll('.graph-chip')
    expect(chips.some((chip) => chip.text().includes('api'))).toBe(true)
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(true)
  })

  it('selects a node and shows its detail via the panel', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    projectGraphApi.getNeighbors.mockResolvedValue({
      node: { id: 'api:f:src/x.py', repo: 'api', type: 'file', name: 'src/x.py' },
      neighbors: [{ id: 'api:s:src/y.py:bar', repo: 'api', type: 'function', name: 'bar' }],
      count: 1,
      edges: [{ src: 'api:f:src/x.py', dst: 'api:s:src/y.py:bar', type: 'calls' }],
    })
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(GraphCanvasStub).vm.$emit('select', 'api:f:src/x.py')
    await flushPromises()
    const panel = wrapper.findComponent(GraphDetailPanelStub)
    expect(panel.exists()).toBe(true)
    expect((panel.props('node') as { name: string }).name).toBe('src/x.py')
    // Symbol-level neighbors merged into the payload after expansion.
    expect(projectGraphApi.getNeighbors).toHaveBeenCalledWith('p-1', 'api:f:src/x.py')
    const nodes = panel.props('neighbors') as Array<{ id: string }>
    expect(nodes.some((entry) => entry.id === 'api:s:src/y.py:bar')).toBe(true)
  })

  it('reports load failures instead of a blank canvas', async () => {
    projectGraphApi.getGraph.mockRejectedValue(new Error('boom'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('boom')
  })

  it('requests no level params by default (overview)', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    mountPage()
    await flushPromises()
    expect(projectGraphApi.getGraph).toHaveBeenCalledWith('p-1', undefined)
  })

  it('re-fetches with level=full and gains symbol type chips on toggle', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.graph-chip').some((c) => c.text() === '函数')).toBe(false)

    const fullPayload = graphPayload({
      level: 'full',
      nodes: [
        { id: 'api:f:src/x.py', repo: 'api', type: 'file', name: 'src/x.py' },
        { id: 'api:s:src/x.py:bar', repo: 'api', type: 'function', name: 'bar' },
      ],
      edges: [{ src: 'api:f:src/x.py', dst: 'api:s:src/x.py:bar', type: 'calls' }],
      counts: { nodes: 2, edges: 1, docs: 0, repos: 1 },
    })
    projectGraphApi.getGraph.mockResolvedValue(fullPayload)
    await wrapper
      .findAll('.graph-chip')
      .find((c) => c.text() === '完整')!
      .trigger('click')
    await flushPromises()
    expect(projectGraphApi.getGraph).toHaveBeenLastCalledWith('p-1', {
      level: 'full',
      maxNodes: 1500,
    })
    expect(wrapper.findAll('.graph-chip').some((c) => c.text() === '函数')).toBe(true)
  })

  it('re-fetches with the new cap when maxNodes changes in full mode', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()
    await wrapper
      .findAll('.graph-chip')
      .find((c) => c.text() === '完整')!
      .trigger('click')
    await flushPromises()

    await wrapper.find('select.graph-select').setValue('5000')
    await flushPromises()
    expect(projectGraphApi.getGraph).toHaveBeenLastCalledWith('p-1', {
      level: 'full',
      maxNodes: 5000,
    })
  })

  it('drops level params again when switching back to overview', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()
    const chips = () => wrapper.findAll('.graph-chip')
    await chips().find((c) => c.text() === '完整')!.trigger('click')
    await flushPromises()
    await chips().find((c) => c.text() === '概览')!.trigger('click')
    await flushPromises()
    expect(projectGraphApi.getGraph).toHaveBeenLastCalledWith('p-1', undefined)
  })
})
