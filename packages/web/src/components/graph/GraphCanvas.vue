<template>
  <div class="graph-canvas">
    <div ref="containerEl" class="graph-canvas-el" />
    <div v-if="!ready" class="graph-canvas-loading">
      <Loader2 :size="18" class="spin" />
      <span>{{ t('graphPage.loading') }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// Cytoscape lifecycle lives entirely here: the page passes plain DTOs and
// reads selection events back. fcose is registered lazily on first mount so
// the chat/workspace bundles never pay for it (the graph route is lazy too,
// but a second-level split keeps even graph-page navigations cheap).
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Loader2 } from 'lucide-vue-next'
import type cytoscape from 'cytoscape'
import type { GraphEdgeDto, GraphNodeDto } from '@/api/projectGraph'
import { EDGE_I18N, EDGE_STYLE } from './edgeStyle'

const { t } = useI18n()

const props = defineProps<{
  nodes: GraphNodeDto[]
  edges: GraphEdgeDto[]
  /** Hide nodes whose type is not in this set (empty = show all). */
  visibleTypes: Set<string>
  /** Hide edges whose type is not in this set (empty = show all). */
  visibleEdgeTypes: Set<string>
  /** Hide nodes outside these repos (empty = all). */
  visibleRepos: Set<string>
  /** Case-insensitive substring match on node name (empty = all). */
  search: string
  /** Re-centers the canvas on this node when it changes (detail-panel click). */
  focusNode: string | null
  /** Page-owned selection; restored after any rebuild so classes survive merges. */
  selectedId: string | null
}>()

const emit = defineEmits<{
  select: [nodeId: string | null]
  /** Edge tapped — the page drills into the symbol-level edges behind it. */
  edgeSelect: [payload: { src: string; dst: string; type: string }]
}>()

const containerEl = ref<HTMLElement | null>(null)
let cy: cytoscape.Core | null = null
let layoutReady = false
let fcoseReady: Promise<void> | null = null
// Covers the blank window while the cytoscape/fcose chunks load; hidden only
// after the first fcose layout has run, since the container stays empty until then.
const ready = ref(false)

function ensureFcose(cytoscapeLib: typeof cytoscape): Promise<void> {
  if (!fcoseReady) {
    fcoseReady = import('cytoscape-fcose').then((mod) => {
      cytoscapeLib.use(mod.default as never)
    })
  }
  return fcoseReady
}

const LANG_BORDER: Record<string, string> = {
  python: '#60a5fa',
  typescript: '#38bdf8',
  javascript: '#facc15',
  vue: '#4ade80',
  java: '#f97316',
  csharp: '#a78bfa',
  sql: '#fbbf24',
}

const TYPE_FILL: Record<string, string> = {
  file: '#1f2937',
  table: '#78350f',
  view: '#713f12',
  procedure: '#713f12',
  trigger: '#713f12',
  module: '#3b2a63',
}

const EDGE_STYLE_FALLBACK = { color: '#4b5563' }

function stylesheet(): cytoscape.StylesheetJsonBlock[] {
  const sheets: cytoscape.StylesheetJsonBlock[] = [
    {
      selector: 'node',
      style: {
        label: 'data(name)',
        'font-size': 9,
        color: '#e5e7eb',
        'text-valign': 'bottom',
        'text-margin-y': 3,
        'text-outline-width': 0,
        width: 18,
        height: 18,
        'background-color': '#1f2937',
        'text-wrap': 'ellipsis',
        'text-max-width': '120px',
      },
    },
    {
      selector: 'node[type = "file"]',
      style: {
        shape: 'round-rectangle',
        width: 26,
        height: 14,
        'background-color': TYPE_FILL.file,
        'border-width': 2,
        'border-color': '#4b5563',
      },
    },
    ...Object.entries(LANG_BORDER).map(([lang, color]) => ({
      selector: `node[type = "file"][lang = "${lang}"]`,
      style: { 'border-color': color },
    })),
    {
      selector: 'node[type = "table"]',
      style: {
        // barrel is the closest built-in to the database-cylinder metaphor
        shape: 'barrel',
        width: 26,
        height: 20,
        'background-color': TYPE_FILL.table,
        'border-width': 1.5,
        'border-color': '#fbbf24',
      },
    },
    ...(['view', 'procedure', 'trigger'] as const).map((type) => ({
      selector: `node[type = "${type}"]`,
      style: {
        shape: 'diamond',
        width: 22,
        height: 22,
        'background-color': TYPE_FILL[type],
        'border-width': 1.5,
        'border-color': '#fcd34d',
      },
    })),
    {
      selector: 'node[type = "module"]',
      style: {
        shape: 'rectangle',
        width: 30,
        height: 22,
        'background-color': TYPE_FILL.module,
        'border-width': 1.5,
        'border-style': 'dashed',
        'border-color': '#c4b5fd',
        color: '#ddd6fe',
      },
    },
    {
      selector: 'node.symbol',
      style: {
        shape: 'ellipse',
        width: 12,
        height: 12,
        'background-color': '#374151',
        'border-width': 1,
        'border-color': '#6b7280',
        'font-size': 8,
      },
    },
    {
      selector: 'edge',
      style: {
        width: 1,
        'curve-style': 'bezier',
        'line-color': '#4b5563',
        'target-arrow-color': '#4b5563',
        'target-arrow-shape': 'triangle',
        opacity: 0.6,
      },
    },
    {
      selector: '.dimmed',
      style: { opacity: 0.12 },
    },
    {
      selector: '.hit',
      style: { 'border-width': 3, 'border-color': '#f87171' },
    },
    {
      selector: '.selected',
      style: {
        'border-width': 3,
        'border-color': '#38bdf8',
        'background-color': '#0ea5e9',
      },
    },
    {
      selector: '.selected-edge',
      style: { width: 2, opacity: 1 },
    },
  ]
  return sheets
}

function isSymbolType(type: string): boolean {
  return type === 'class' || type === 'function' || type === 'symbol' || type === 'column'
}

function applyFilters(): void {
  if (!cy) return
  const q = props.search.trim().toLowerCase()
  cy.batch(() => {
    cy!.nodes().forEach((node) => {
      const data = node.data() as GraphNodeDto
      const typeOk = props.visibleTypes.size === 0 || props.visibleTypes.has(data.type)
      const repoOk =
        props.visibleRepos.size === 0 || (data.repo !== null && props.visibleRepos.has(data.repo))
      const searchOk =
        !q || data.name.toLowerCase().includes(q) || (data.slug ?? '').toLowerCase().includes(q)
      node.style('display', typeOk && repoOk && searchOk ? 'element' : 'none')
    })
    // Edges to hidden endpoints would dangle — hide them too; the edge-type
    // chip set cuts by relation (empty = all types).
    cy!.edges().forEach((edge) => {
      const endpointsVisible =
        edge.source().style('display') !== 'none' && edge.target().style('display') !== 'none'
      const typeOk =
        props.visibleEdgeTypes.size === 0 ||
        props.visibleEdgeTypes.has(edge.data('type') as string)
      edge.style('display', endpointsVisible && typeOk ? 'element' : 'none')
    })
  })
  if (q) {
    const hits = cy.nodes().filter((n) => n.style('display') !== 'none')
    if (hits.length > 0) cy.fit(hits, 60)
  }
}

/** Stable edge identity — cytoscape's data.id must not use array index
 * (indices shift when GraphPage appends neighbor-merge edges). Node ids are
 * `repo:…` / `d:…` and never contain `|`. */
function edgeKey(src: string, dst: string, type: string): string {
  return `${src}|${dst}|${type}`
}

/** Deterministic grid position from payload order (pure function of the list). */
function gridPosition(index: number, count: number): { x: number; y: number } {
  const cols = Math.max(1, Math.ceil(Math.sqrt(count)))
  return { x: (index % cols) * 80, y: Math.floor(index / cols) * 80 }
}

type FixedNode = { nodeId: string; position: { x: number; y: number } }

/** randomize:false skips fcose's Math.random spectral phase; quality default
 * still applies positions (see cytoscape-fcose index.js). Expansion keeps the
 * viewport (`fit: false` + pinned survivors); first paint fits the frame. */
function runLayout(opts?: { fixed?: FixedNode[]; fit?: boolean }): void {
  if (!cy) return
  const fit = opts?.fit ?? true
  const options: Record<string, unknown> = {
    name: 'fcose',
    animate: false,
    fit,
    randomize: false,
    quality: 'default',
  }
  if (opts?.fixed && opts.fixed.length > 0) options.fixedNodeConstraint = opts.fixed
  cy.layout(options as unknown as cytoscape.LayoutOptions).run()
  if (fit) cy.fit(undefined, 40)
}

function styleEdges(): void {
  if (!cy) return
  // Edge styling by type — cytoscape stylesheet selectors cannot match on
  // arbitrary data values portably, so paint in a batch instead.
  cy.batch(() => {
    cy!.edges().forEach((edge) => {
      const style = EDGE_STYLE[edge.data('type') as string] ?? EDGE_STYLE_FALLBACK
      edge.style('line-color', style.color)
      edge.style('target-arrow-color', style.color)
      if (style.lineStyle) edge.style('line-style', style.lineStyle)
    })
  })
}

/** i18n label for an edge's relation type (raw type when unmapped). */
function edgeLabelText(type: string): string {
  const key = EDGE_I18N[type]
  return key ? t(key) : type
}

let selectedEdgeKey: string | null = null

function clearEdgeSelection(): void {
  if (!cy || !selectedEdgeKey) return
  const prev = cy.getElementById(selectedEdgeKey)
  if (!prev.empty()) {
    prev.removeClass('selected-edge')
    prev.style('label', '')
  }
  selectedEdgeKey = null
}

/** Edge tap: highlight + label the edge and let the page expand the raw
 * symbol edges it was rolled up from (overview) — a no-op payload at full
 * level, where the edge is already raw. */
function onEdgeTap(target: cytoscape.EventObject): void {
  if (!cy) return
  const edge = target.target as cytoscape.EdgeSingular
  if (edge.empty?.()) return
  clearEdgeSelection()
  const key = edgeKey(edge.source().id(), edge.target().id(), edge.data('type') as string)
  selectedEdgeKey = key
  edge.addClass('selected-edge')
  edge.style('label', edgeLabelText(edge.data('type') as string))
  emit('edgeSelect', {
    src: edge.source().id(),
    dst: edge.target().id(),
    type: edge.data('type') as string,
  })
}

/** Re-apply page-owned selection after wipe/merge. Optional `id` covers the
 * tap path where props.selectedId has not flushed yet. */
function applySelection(id: string | null = props.selectedId): void {
  if (!cy) return
  cy.elements().removeClass('selected dimmed')
  if (!id) return
  const node = cy.getElementById(id)
  if (node.empty()) return
  node.addClass('selected')
  cy.elements().not(node.closedNeighborhood()).addClass('dimmed')
}

function rebuild(): void {
  // layoutReady guards the window where cy exists but the fcose import has
  // not landed: layout('fcose') would throw. onMounted re-runs rebuild after
  // registration, so nothing is lost.
  if (!cy || !layoutReady) return

  const prevNodeIds = new Set(cy.nodes().map((n) => n.id()))
  const prevEdgeKeys = new Set(
    cy.edges().map((e) => edgeKey(e.source().id(), e.target().id(), e.data('type') as string)),
  )
  const nextNodeIds = new Set(props.nodes.map((n) => n.id))
  const nextEdgeKeys = new Set(props.edges.map((e) => edgeKey(e.src, e.dst, e.type)))

  const nodesToAdd = props.nodes.filter((n) => !prevNodeIds.has(n.id))
  const edgesToAdd = props.edges.filter((e) => !prevEdgeKeys.has(edgeKey(e.src, e.dst, e.type)))
  const nodesToRemove = cy.nodes().filter((n) => !nextNodeIds.has(n.id()))
  const edgesToRemove = cy.edges().filter(
    (e) => !nextEdgeKeys.has(edgeKey(e.source().id(), e.target().id(), e.data('type') as string)),
  )

  const emptyCanvas = prevNodeIds.size === 0

  if (emptyCanvas) {
    if (props.nodes.length > 0) {
      const total = props.nodes.length
      cy.add({
        nodes: props.nodes.map((node, index) => ({
          data: { ...node },
          position: gridPosition(index, total),
          classes: isSymbolType(node.type) ? 'symbol' : '',
        })),
        edges: props.edges.map((edge) => ({
          data: {
            id: edgeKey(edge.src, edge.dst, edge.type),
            source: edge.src,
            target: edge.dst,
            type: edge.type,
          },
          classes: '',
        })),
      })
      styleEdges()
      applyFilters()
      runLayout({ fit: true })
    }
    applySelection()
    return
  }

  // Removals / identical set: keep surviving positions; only additions re-layout.
  if (edgesToRemove.length > 0) edgesToRemove.remove()
  if (nodesToRemove.length > 0) nodesToRemove.remove()

  const survivors: FixedNode[] = []
  cy.nodes().forEach((n) => {
    if (!nextNodeIds.has(n.id())) return
    const p = n.position()
    survivors.push({ nodeId: n.id(), position: { x: p.x, y: p.y } })
  })

  // Refresh survivor DTO fields (labels/types) without touching positions.
  if (survivors.length > 0) {
    const byId = new Map(props.nodes.map((n) => [n.id, n]))
    cy.batch(() => {
      cy!.nodes().forEach((n) => {
        const dto = byId.get(n.id())
        if (dto) n.data({ ...dto })
      })
    })
  }

  if (nodesToAdd.length > 0 || edgesToAdd.length > 0) {
    const total = props.nodes.length
    const newIds = new Set(nodesToAdd.map((n) => n.id))
    // Seed new nodes from payload order, then snap onto neighbor means when
    // an incident edge already has a positioned endpoint on the canvas.
    const seeded = new Map<string, { x: number; y: number }>()
    props.nodes.forEach((n, index) => {
      if (newIds.has(n.id)) seeded.set(n.id, gridPosition(index, total))
    })
    for (const edge of props.edges) {
      if (!newIds.has(edge.src) || newIds.has(edge.dst)) continue
      const anchor = cy.getElementById(edge.dst)
      if (anchor.empty()) continue
      const p = anchor.position()
      const base = seeded.get(edge.src)!
      seeded.set(edge.src, { x: (base.x + p.x) / 2, y: (base.y + p.y) / 2 })
    }
    for (const edge of props.edges) {
      if (!newIds.has(edge.dst) || newIds.has(edge.src)) continue
      const anchor = cy.getElementById(edge.src)
      if (anchor.empty()) continue
      const p = anchor.position()
      const base = seeded.get(edge.dst)!
      seeded.set(edge.dst, { x: (base.x + p.x) / 2, y: (base.y + p.y) / 2 })
    }

    cy.add({
      nodes: nodesToAdd.map((node) => ({
        data: { ...node },
        position: seeded.get(node.id) ?? gridPosition(0, 1),
        classes: isSymbolType(node.type) ? 'symbol' : '',
      })),
      edges: edgesToAdd.map((edge) => ({
        data: {
          id: edgeKey(edge.src, edge.dst, edge.type),
          source: edge.src,
          target: edge.dst,
          type: edge.type,
        },
        classes: '',
      })),
    })
    styleEdges()
    applyFilters()
    // Pin survivors; keep viewport — only new nodes settle.
    runLayout({ fixed: survivors, fit: false })
  } else {
    // Removal-only or same set: no layout — holes stay put, refresh is a no-op.
    styleEdges()
    applyFilters()
  }
  applySelection()
}

function onSelect(target: cytoscape.EventObject): void {
  if (!cy) return
  clearEdgeSelection()
  const node = target.target as cytoscape.NodeSingular
  if (node.empty?.()) {
    emit('select', null)
    applySelection(null)
    return
  }
  emit('select', node.id())
  applySelection(node.id())
}

onMounted(async () => {
  if (!containerEl.value) return
  const cytoscapeMod = await import('cytoscape')
  const cytoscapeLib = cytoscapeMod.default
  // Cytoscape needs a sized container; a zero-height flex child would compute
  // every layout position as NaN and render nothing.
  cy = cytoscapeLib({
    container: containerEl.value,
    elements: [],
    style: stylesheet(),
    minZoom: 0.05,
    maxZoom: 4,
    wheelSensitivity: 0.25,
    // Big-graph viewport guards: skip edge redraw and texture upload while
    // panning a few-thousand-element canvas.
    textureOnViewport: true,
    hideEdgesOnViewport: true,
  })
  cy.on('tap', 'node', onSelect)
  cy.on('tap', 'edge', onEdgeTap)
  // Hover label: relation names appear on demand only — labeling every edge
  // on a thousand-node canvas would drown the layout.
  cy.on('mouseover', 'edge', (event: cytoscape.EventObject) => {
    const edge = event.target as cytoscape.EdgeSingular
    if (edge.empty?.() || edge.data('id') === selectedEdgeKey) return
    edge.style('label', edgeLabelText(edge.data('type') as string))
  })
  cy.on('mouseout', 'edge', (event: cytoscape.EventObject) => {
    const edge = event.target as cytoscape.EdgeSingular
    if (edge.empty?.() || edge.data('id') === selectedEdgeKey) return
    edge.style('label', '')
  })
  cy.on('tap', (event) => {
    if (event.target === cy) {
      clearEdgeSelection()
      onSelect(event)
    }
  })
  await ensureFcose(cytoscapeLib)
  layoutReady = true
  rebuild()
  ready.value = true
})

onUnmounted(() => {
  cy?.destroy()
  cy = null
})

watch(() => [props.nodes, props.edges], rebuild)
watch(
  () => [props.visibleTypes, props.visibleEdgeTypes, props.visibleRepos, props.search],
  applyFilters,
)
watch(() => props.selectedId, applySelection)
watch(
  () => props.focusNode,
  (nodeId) => {
    if (!cy || !nodeId) return
    const node = cy.getElementById(nodeId)
    if (!node.empty()) {
      cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 0.8) }, { duration: 200 })
    }
  },
)

defineExpose({
  /** Re-runs fcose from current positions (never re-randomizes). */
  relayout() {
    if (!cy || !layoutReady) return
    runLayout()
  },
})
</script>

<style scoped>
.graph-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  background: var(--bg-secondary, #0b1020);
}
/* Cytoscape needs a sized container; absolute fill keeps it exact inside
   the relative root. */
.graph-canvas-el {
  position: absolute;
  inset: 0;
}
.graph-canvas-loading {
  position: absolute;
  inset: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--bg-secondary, #0b1020);
  color: var(--text-secondary, #9ca3af);
  font-size: 13px;
}
</style>
