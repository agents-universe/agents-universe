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
  expandEdge: vi.fn(),
  build: vi.fn(),
}))
vi.mock('@/api/projectGraph', () => ({ projectGraphApi }))

const GraphCanvasStub = vi.hoisted(() => ({
  name: 'GraphCanvasStub',
  props: [
    'nodes',
    'edges',
    'visibleTypes',
    'visibleEdgeTypes',
    'visibleRepos',
    'search',
    'focusNode',
    'selectedId',
  ],
  emits: ['select', 'edgeSelect'],
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

  it('renders relation chips with swatches and toggles edge visibility', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()

    const relationGroup = wrapper
      .findAll('.graph-chip-group')
      .find((g) => g.find('.graph-chip-label').text() === '关系')
    expect(relationGroup, 'relation chip group missing').toBeTruthy()
    const chips = relationGroup!.findAll('.graph-chip')
    expect(chips.some((c) => c.text().includes('导入'))).toBe(true)
    expect(relationGroup!.find('.graph-swatch').exists()).toBe(true)

    const importsChip = chips.find((c) => c.text().includes('导入'))!
    await importsChip.trigger('click')
    const canvas = wrapper.findComponent(GraphCanvasStub)
    const selected = canvas.props('visibleEdgeTypes') as Set<string>
    expect(selected.has('imports')).toBe(true)

    // "All" clears the set again.
    await chips[0].trigger('click')
    expect((canvas.props('visibleEdgeTypes') as Set<string>).size).toBe(0)
  })

  it('expands a tapped edge into its raw symbol edges', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    projectGraphApi.expandEdge.mockResolvedValue({
      src: 'api:f:src/x.py',
      dst: 'api:f:src/y.py',
      count: 1,
      edges: [
        { src: 'api:s:src/x.py:foo', dst: 'api:s:src/y.py:bar', type: 'imports' },
      ],
      nodes: [
        { id: 'api:s:src/x.py:foo', repo: 'api', type: 'function', name: 'foo' },
        { id: 'api:s:src/y.py:bar', repo: 'api', type: 'function', name: 'bar' },
      ],
    })
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(GraphCanvasStub).vm.$emit('edgeSelect', {
      src: 'api:f:src/x.py',
      dst: 'api:f:src/y.py',
      type: 'imports',
    })
    await flushPromises()

    expect(projectGraphApi.expandEdge).toHaveBeenCalledWith(
      'p-1',
      'api:f:src/x.py',
      'api:f:src/y.py',
      'imports',
    )
    // Detail panel opens on the edge's source.
    const panel = wrapper.findComponent(GraphDetailPanelStub)
    expect((panel.props('node') as { id: string }).id).toBe('api:f:src/x.py')
    // Raw symbol edges/nodes merged into the payload the canvas reads.
    const canvas = wrapper.findComponent(GraphCanvasStub)
    const nodes = canvas.props('nodes') as Array<{ id: string }>
    const edges = canvas.props('edges') as Array<{ src: string }>
    expect(nodes.some((n) => n.id === 'api:s:src/x.py:foo')).toBe(true)
    expect(edges.some((e) => e.src === 'api:s:src/x.py:foo')).toBe(true)
  })

  it('skips the edge fetch for doc edges (their edges ship with the payload)', async () => {
    projectGraphApi.getGraph.mockResolvedValue(
      graphPayload({
        nodes: [{ id: 'd:domain/context', repo: null, type: 'module', name: 'ctx' }],
        edges: [{ src: 'd:domain/context', dst: 'd:domain/context', type: 'references' }],
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(GraphCanvasStub).vm.$emit('edgeSelect', {
      src: 'd:domain/context',
      dst: 'd:domain/context',
      type: 'references',
    })
    await flushPromises()

    expect(projectGraphApi.expandEdge).not.toHaveBeenCalled()
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

  it('shows the loading state while the first graph request is pending', async () => {
    let resolveGraph!: (value: unknown) => void
    projectGraphApi.getGraph.mockReturnValue(
      new Promise((resolve) => {
        resolveGraph = resolve
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.graph-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载图谱…')
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(false)

    resolveGraph(graphPayload())
    await flushPromises()
    expect(wrapper.find('.graph-state').exists()).toBe(false)
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(true)
  })

  it('shows a reload overlay and spins the refresh button when re-fetching', async () => {
    projectGraphApi.getGraph.mockResolvedValue(graphPayload())
    const wrapper = mountPage()
    await flushPromises()

    let resolveReload!: (value: unknown) => void
    projectGraphApi.getGraph.mockReturnValue(
      new Promise((resolve) => {
        resolveReload = resolve
      }),
    )
    await wrapper.find('button.btn-sm.secondary').trigger('click')
    await flushPromises()

    expect(wrapper.find('.graph-reload-overlay').exists()).toBe(true)
    expect(wrapper.find('.graph-header-actions .graph-spin').exists()).toBe(true)
    // Old graph stays mounted under the overlay.
    expect(wrapper.find('.graph-canvas-stub').exists()).toBe(true)

    resolveReload(graphPayload())
    await flushPromises()
    expect(wrapper.find('.graph-reload-overlay').exists()).toBe(false)
    expect(wrapper.find('.graph-header-actions .graph-spin').exists()).toBe(false)
  })
})
