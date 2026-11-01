<template>
  <div ref="containerEl" class="graph-canvas" />
</template>

<script setup lang="ts">
// Cytoscape lifecycle lives entirely here: the page passes plain DTOs and
// reads selection events back. fcose is registered lazily on first mount so
// the chat/workspace bundles never pay for it (the graph route is lazy too,
// but a second-level split keeps even graph-page navigations cheap).
import { onMounted, onUnmounted, ref, watch } from 'vue'
import type cytoscape from 'cytoscape'
import type { GraphEdgeDto, GraphNodeDto } from '@/api/projectGraph'

const props = defineProps<{
  nodes: GraphNodeDto[]
  edges: GraphEdgeDto[]
  /** Hide nodes whose type is not in this set (empty = show all). */
  visibleTypes: Set<string>
  /** Hide nodes outside these repos (empty = all). */
  visibleRepos: Set<string>
  /** Case-insensitive substring match on node name (empty = all). */
  search: string
  /** Re-centers the canvas on this node when it changes (detail-panel click). */
  focusNode: string | null
}>()

const emit = defineEmits<{ select: [nodeId: string | null] }>()

const containerEl = ref<HTMLElement | null>(null)
let cy: cytoscape.Core | null = null
let layoutReady = false
let fcoseReady: Promise<void> | null = null

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

const EDGE_STYLE: Record<string, { color: string; lineStyle?: string }> = {
  imports: { color: '#4b5563' },
  calls: { color: '#3b82f6' },
  inherits: { color: '#8b5cf6' },
  reads: { color: '#f59e0b' },
  writes: { color: '#f59e0b' },
  foreign_key: { color: '#f59e0b' },
  primary_key: { color: '#f59e0b' },
  references: { color: '#22c55e', lineStyle: 'dashed' },
}

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
    // Edges to hidden endpoints would dangle — hide them too.
    cy!.edges().forEach((edge) => {
      const visible =
        edge.source().style('display') !== 'none' && edge.target().style('display') !== 'none'
      edge.style('display', visible ? 'element' : 'none')
    })
  })
  if (q) {
    const hits = cy.nodes().filter((n) => n.style('display') !== 'none')
    if (hits.length > 0) cy.fit(hits, 60)
  }
}

function rebuild(): void {
  // layoutReady guards the window where cy exists but the fcose import has
  // not landed: layout('fcose') would throw. onMounted re-runs rebuild after
  // registration, so nothing is lost.
  if (!cy || !layoutReady) return
  const elements: cytoscape.ElementsDefinition = {
    nodes: props.nodes.map((node) => ({
      data: { ...node },
      classes: isSymbolType(node.type) ? 'symbol' : '',
    })),
    edges: props.edges.map((edge, index) => ({
      // cytoscape addresses edges by source/target; keep src/dst out of data
      // so no consumer confuses the two conventions.
      data: { id: `e${index}`, source: edge.src, target: edge.dst, type: edge.type },
      classes: '',
    })),
  }
  cy.elements().remove()
  cy.add(elements)
  // Edge styling by type — cytoscape stylesheet selectors cannot match on
  // arbitrary data values portably, so paint in a batch instead.
  cy.batch(() => {
    cy!.edges().forEach((edge) => {
      const style = EDGE_STYLE[edge.data('type') as string]
      if (!style) return
      edge.style('line-color', style.color)
      edge.style('target-arrow-color', style.color)
      if (style.lineStyle) edge.style('line-style', style.lineStyle)
    })
  })
  applyFilters()
  if (props.nodes.length > 0) {
    // Fresh elements have no positions — run the layout, then frame them.
    cy.layout({
      name: 'fcose',
      animate: false,
      fit: true,
      randomize: true,
      quality: 'default',
    } as cytoscape.LayoutOptions).run()
    cy.fit(undefined, 40)
  }
}

function onSelect(target: cytoscape.EventObject): void {
  if (!cy) return
  const node = target.target as cytoscape.NodeSingular
  if (node.empty?.()) {
    cy.elements().removeClass('selected dimmed')
    emit('select', null)
    return
  }
  cy.elements().removeClass('selected dimmed')
  node.addClass('selected')
  const hood = node.closedNeighborhood()
  cy.elements().not(hood).addClass('dimmed')
  emit('select', node.id())
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
  cy.on('tap', (event) => {
    if (event.target === cy) onSelect(event)
  })
  await ensureFcose(cytoscapeLib)
  layoutReady = true
  rebuild()
})

onUnmounted(() => {
  cy?.destroy()
  cy = null
})

watch(() => [props.nodes, props.edges], rebuild)
watch(
  () => [props.visibleTypes, props.visibleRepos, props.search],
  applyFilters,
)
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
  /** Re-runs the fcose layout over currently visible elements. */
  relayout() {
    if (!cy || !layoutReady) return
    cy.layout({
      name: 'fcose',
      animate: false,
      fit: true,
      randomize: true,
      quality: 'default',
    } as cytoscape.LayoutOptions).run()
  },
})
</script>

<style scoped>
.graph-canvas {
  width: 100%;
  height: 100%;
  min-height: 320px;
  background: var(--bg-secondary, #0b1020);
}
</style>
