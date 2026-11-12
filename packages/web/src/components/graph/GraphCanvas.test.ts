import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import GraphCanvas from './GraphCanvas.vue'

// Minimal cytoscape stand-in: enough surface for rebuild/layout/selection
// assertions without a real layout engine (jsdom has no layout either).
const harness = vi.hoisted(() => {
  type Pos = { x: number; y: number }

  type StubEle = {
    _id: string
    _kind: 'node' | 'edge'
    _pos: Pos
    _data: Record<string, unknown>
    _classes: Set<string>
    _src?: string
    _dst?: string
    id: () => string
    data: (key?: string) => unknown
    position: (p?: Pos) => Pos
    nonempty: () => boolean
    empty: () => boolean
    addClass: (c: string) => void
    removeClass: (c: string) => void
    style: (...args: unknown[]) => unknown
    source: () => StubEle
    target: () => StubEle
    neighborhood: () => StubColl
    closedNeighborhood: () => StubColl
    remove: () => void
  }

  type StubColl = {
    length: number
    forEach: (fn: (e: StubEle) => void) => void
    map: <T>(fn: (e: StubEle) => T) => T[]
    filter: (fn: (e: StubEle) => boolean) => StubColl
    nonempty: () => boolean
    empty: () => boolean
    addClass: (c: string) => void
    removeClass: (c: string) => void
    not: (other: StubColl) => StubColl
    remove: () => void
    forEachEdge?: undefined
  }

  const state = {
    eles: [] as StubEle[],
    layoutCalls: [] as Array<Record<string, unknown>>,
    addCalls: [] as Array<{ nodes: unknown[]; edges: unknown[] }>,
    removeCalls: 0,
    fitCalls: 0,
  }

  const byId = (id: string) => state.eles.find((e) => e._id === id)

  const makeNode = (id: string, data: Record<string, unknown>, pos: Pos): StubEle => {
    const classes = new Set<string>()
    const ele: StubEle = {
      _id: id,
      _kind: 'node',
      _pos: { ...pos },
      _data: data,
      _classes: classes,
      id: () => id,
      data: (key?: string) => (key === undefined ? { ...ele._data } : ele._data[key]),
      position: (p?: Pos) => {
        if (p) ele._pos = { ...p }
        return { ...ele._pos }
      },
      nonempty: () => true,
      empty: () => false,
      addClass: (c) => c.split(/\s+/).forEach((x) => classes.add(x)),
      removeClass: (c) => c.split(/\s+/).forEach((x) => classes.delete(x)),
      style: vi.fn(),
      source: () => ele,
      target: () => ele,
      neighborhood: () => coll([]),
      closedNeighborhood: () => coll([ele]),
      remove: () => {
        state.removeCalls++
        state.eles = state.eles.filter((e) => e !== ele)
      },
    }
    return ele
  }

  const makeEdge = (
    id: string,
    data: Record<string, unknown>,
    src: string,
    dst: string,
  ): StubEle => {
    const classes = new Set<string>()
    const ele: StubEle = {
      _id: id,
      _kind: 'edge',
      _pos: { x: 0, y: 0 },
      _data: data,
      _classes: classes,
      _src: src,
      _dst: dst,
      id: () => id,
      data: (key?: string) => (key === undefined ? { ...ele._data } : ele._data[key]),
      position: () => ({ ...ele._pos }),
      nonempty: () => true,
      empty: () => false,
      addClass: (c) => c.split(/\s+/).forEach((x) => classes.add(x)),
      removeClass: (c) => c.split(/\s+/).forEach((x) => classes.delete(x)),
      style: vi.fn(),
      source: () => byId(src) ?? ele,
      target: () => byId(dst) ?? ele,
      neighborhood: () => coll([]),
      closedNeighborhood: () => coll([ele]),
      remove: () => {
        state.removeCalls++
        state.eles = state.eles.filter((e) => e !== ele)
      },
    }
    return ele
  }

  const coll = (items: StubEle[]): StubColl => ({
    length: items.length,
    forEach: (fn) => items.forEach(fn),
    map: (fn) => items.map(fn),
    filter: (fn) => coll(items.filter(fn)),
    nonempty: () => items.length > 0,
    empty: () => items.length === 0,
    addClass: (c) => items.forEach((i) => i.addClass(c)),
    removeClass: (c) => items.forEach((i) => i.removeClass(c)),
    not: (other) => {
      const exclude = new Set<string>()
      other.forEach((e) => exclude.add(e.id()))
      return coll(items.filter((i) => !exclude.has(i.id())))
    },
    remove: () => items.forEach((i) => i.remove()),
  })

  const nodes = () => coll(state.eles.filter((e) => e._kind === 'node'))
  const edges = () => coll(state.eles.filter((e) => e._kind === 'edge'))

  const cy = {
    on: vi.fn(),
    elements: () => coll([...state.eles]),
    add: vi.fn((def: { nodes?: unknown[]; edges?: unknown[] }) => {
      state.addCalls.push({ nodes: def.nodes ?? [], edges: def.edges ?? [] })
      for (const n of def.nodes ?? []) {
        const d = (n as { data: Record<string, unknown>; position?: Pos }).data
        const pos = (n as { position?: Pos }).position ?? { x: 0, y: 0 }
        state.eles.push(makeNode(String(d.id), d, pos))
      }
      for (const e of def.edges ?? []) {
        const d = (e as { data: Record<string, unknown> }).data
        state.eles.push(
          makeEdge(String(d.id), d, String(d.source), String(d.target)),
        )
      }
    }),
    batch: vi.fn((fn: () => void) => fn()),
    nodes,
    edges,
    layout: vi.fn((opts: Record<string, unknown>) => {
      state.layoutCalls.push(opts)
      return { run: vi.fn() }
    }),
    fit: vi.fn(() => {
      state.fitCalls++
    }),
    destroy: vi.fn(),
    getElementById: (id: string) => {
      const found = byId(id)
      if (!found) {
        return {
          nonempty: () => false,
          empty: () => true,
          position: () => ({ x: 0, y: 0 }),
          addClass: vi.fn(),
          removeClass: vi.fn(),
          data: vi.fn(),
        }
      }
      return found
    },
    animate: vi.fn(),
    zoom: () => 1,
  }

  return { cy, state, reset }
  function reset() {
    state.eles = []
    state.layoutCalls = []
    state.addCalls = []
    state.removeCalls = 0
    state.fitCalls = 0
  }
})

vi.mock('cytoscape', () => ({
  default: Object.assign(vi.fn(() => harness.cy), { use: vi.fn() }),
}))
vi.mock('cytoscape-fcose', () => ({ default: {} }))

const twoNodes = {
  nodes: [
    { id: 'a', repo: 'r', type: 'file', name: 'a.py' },
    { id: 'b', repo: 'r', type: 'file', name: 'b.py' },
  ],
  edges: [{ src: 'a', dst: 'b', type: 'imports' }] as Array<{
    src: string
    dst: string
    type: string
  }>,
  visibleTypes: new Set<string>(),
  visibleEdgeTypes: new Set<string>(),
  visibleRepos: new Set<string>(),
  search: '',
  focusNode: null,
  selectedId: null as string | null,
}

describe('GraphCanvas', () => {
  it('shows the init overlay until cytoscape loads and the first layout runs', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    expect(wrapper.find('.graph-canvas-loading').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载图谱…')

    await flushPromises()
    expect(wrapper.find('.graph-canvas-loading').exists()).toBe(false)
    expect(harness.state.layoutCalls.length).toBeGreaterThan(0)

    wrapper.unmount()
    expect(harness.cy.destroy).toHaveBeenCalled()
  })

  it('runs fcose with randomize:false for a reproducible layout', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()

    const first = harness.state.layoutCalls[0]
    expect(first).toMatchObject({
      name: 'fcose',
      randomize: false,
      quality: 'default',
      fit: true,
    })
    expect(first.fixedNodeConstraint).toBeUndefined()

    wrapper.unmount()
  })

  it('seeds the same grid positions on every first paint', async () => {
    harness.reset()
    const w1 = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()
    const pos1 = harness.state.eles
      .filter((e) => e._kind === 'node')
      .map((e) => ({ id: e.id(), ...e.position() }))
    w1.unmount()

    harness.reset()
    const w2 = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()
    const pos2 = harness.state.eles
      .filter((e) => e._kind === 'node')
      .map((e) => ({ id: e.id(), ...e.position() }))
    w2.unmount()

    expect(pos1).toEqual(pos2)
    expect(pos1.length).toBe(2)
  })

  it('adds only new elements on neighbor merge and pins survivors', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()
    expect(harness.state.removeCalls).toBe(0)
    expect(harness.state.addCalls.length).toBe(1)

    await wrapper.setProps({
      ...twoNodes,
      nodes: [
        ...twoNodes.nodes,
        { id: 'c', repo: 'r', type: 'function', name: 'c' },
      ],
      edges: [...twoNodes.edges, { src: 'b', dst: 'c', type: 'calls' }],
    })
    await flushPromises()

    // Diff path: never wipes the canvas.
    expect(harness.state.removeCalls).toBe(0)
    expect(harness.state.addCalls.length).toBe(2)
    const mergeAdd = harness.state.addCalls[1]
    expect((mergeAdd.nodes as Array<{ data: { id: string } }>).map((n) => n.data.id)).toEqual([
      'c',
    ])
    expect(mergeAdd.edges).toHaveLength(1)

    const layout = harness.state.layoutCalls[harness.state.layoutCalls.length - 1]
    expect(layout).toMatchObject({ randomize: false, fit: false })
    const fixed = layout.fixedNodeConstraint as Array<{ nodeId: string }>
    expect(fixed.map((f) => f.nodeId).sort()).toEqual(['a', 'b'])
    // Viewport must not re-fit on expansion.
    expect(harness.state.fitCalls).toBe(1)

    wrapper.unmount()
  })

  it('skips layout when props only refresh the same element set', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()
    const layoutsAfterMount = harness.state.layoutCalls.length

    // New array references, identical ids/edges (refresh with same graph.json).
    await wrapper.setProps({
      ...twoNodes,
      nodes: twoNodes.nodes.map((n) => ({ ...n })),
      edges: twoNodes.edges.map((e) => ({ ...e })),
    })
    await flushPromises()

    expect(harness.state.layoutCalls.length).toBe(layoutsAfterMount)
    expect(harness.state.removeCalls).toBe(0)

    wrapper.unmount()
  })

  it('hides edges whose type is filtered out without relayout', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()
    const layoutsAfterMount = harness.state.layoutCalls.length

    const imports = harness.state.eles.find((e) => e._kind === 'edge')
    expect(imports?.style).toHaveBeenCalledWith('display', 'element')

    // Empty = all on; picking only `calls` hides the imports edge.
    await wrapper.setProps({ ...twoNodes, visibleEdgeTypes: new Set(['calls']) })
    expect(imports?.style).toHaveBeenLastCalledWith('display', 'none')
    expect(harness.state.layoutCalls.length).toBe(layoutsAfterMount)

    // Back to all-visible: element again.
    await wrapper.setProps({ ...twoNodes, visibleEdgeTypes: new Set<string>() })
    expect(imports?.style).toHaveBeenLastCalledWith('display', 'element')

    wrapper.unmount()
  })

  it('emits edgeSelect with endpoints and type when an edge is tapped', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()

    // Handlers are registered through cy.on(selector, handler); the harness
    // accumulates calls across mounts, so take the latest registration.
    const edgeTap = harness.cy.on.mock.calls.filter(
      (call) => call[0] === 'tap' && call[1] === 'edge',
    ).at(-1)
    expect(edgeTap, 'tap handler for edges not registered').toBeTruthy()

    const edge = harness.state.eles.find((e) => e._kind === 'edge')!
    edgeTap![2]({ target: edge })

    expect(wrapper.emitted('edgeSelect')?.[0]).toEqual([
      { src: 'a', dst: 'b', type: 'imports' },
    ])
    // Selected edge is highlighted and labeled with the translated relation.
    expect(edge._classes.has('selected-edge')).toBe(true)
    expect(edge.style).toHaveBeenCalledWith('label', '导入')

    wrapper.unmount()
  })

  it('labels an edge on hover and clears it on mouseout', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: twoNodes })
    await flushPromises()

    const over = harness.cy.on.mock.calls.filter(
      (call) => call[0] === 'mouseover' && call[1] === 'edge',
    ).at(-1)
    const out = harness.cy.on.mock.calls.filter(
      (call) => call[0] === 'mouseout' && call[1] === 'edge',
    ).at(-1)
    expect(over, 'mouseover handler not registered').toBeTruthy()
    expect(out, 'mouseout handler not registered').toBeTruthy()

    const edge = harness.state.eles.find((e) => e._kind === 'edge')!
    over![2]({ target: edge })
    expect(edge.style).toHaveBeenCalledWith('label', '导入')
    out![2]({ target: edge })
    expect(edge.style).toHaveBeenLastCalledWith('label', '')

    wrapper.unmount()
  })

  it('re-applies selection classes after an additive rebuild', async () => {
    harness.reset()
    const wrapper = mount(GraphCanvas, { props: { ...twoNodes, selectedId: 'a' } })
    await flushPromises()

    const nodeA = () => harness.state.eles.find((e) => e.id() === 'a')
    expect(nodeA()?._classes.has('selected')).toBe(true)

    await wrapper.setProps({
      ...twoNodes,
      nodes: [...twoNodes.nodes, { id: 'c', repo: 'r', type: 'function', name: 'c' }],
      edges: [...twoNodes.edges, { src: 'b', dst: 'c', type: 'calls' }],
      selectedId: 'a',
    })
    await flushPromises()

    expect(nodeA()?._classes.has('selected')).toBe(true)
    expect(harness.state.layoutCalls.at(-1)).toMatchObject({ randomize: false })

    wrapper.unmount()
  })
})
