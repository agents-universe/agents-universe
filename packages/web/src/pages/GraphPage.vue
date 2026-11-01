<template>
  <div class="graph-page">
    <header class="graph-header">
      <div class="graph-header-left">
        <span class="graph-header-icon"><Network :size="20" /></span>
        <div>
          <h1 class="graph-title">{{ t('graphPage.title') }}</h1>
          <p v-if="graph" class="graph-sub">
            {{ t('graphPage.counts', { nodes: graph.counts.nodes, edges: graph.counts.edges }) }}
            <span v-if="graph.truncated"> · {{ t('graphPage.truncated') }}</span>
          </p>
        </div>
      </div>
      <div class="graph-header-actions">
        <button class="btn-sm secondary" :disabled="loading || !graph" @click="() => load()">
          <RefreshCw :size="13" />
          {{ t('graphPage.refresh') }}
        </button>
        <button
          class="btn-primary btn-sm"
          :disabled="building || !projectId"
          :title="t('graphPage.build')"
          @click="buildGraphs"
        >
          <Loader2 v-if="building" :size="13" class="graph-spin" />
          <Hammer v-else :size="13" />
          {{ building ? t('graphPage.building') : t('graphPage.build') }}
        </button>
      </div>
    </header>

    <p v-if="message.text" class="graph-message" :class="message.ok ? 'success' : 'error'">
      {{ message.ok ? '✓' : '✗' }} {{ message.text }}
    </p>

    <div v-if="loading && !graph" class="graph-state">
      <Loader2 :size="18" class="graph-spin" />
      <span>{{ t('graphPage.loading') }}</span>
    </div>

    <div v-else-if="error" class="graph-message error">✗ {{ error }}</div>

    <div v-else-if="graph && graph.needs_build" class="graph-state graph-empty">
      <span class="graph-empty-icon"><Network :size="28" /></span>
      <p class="graph-empty-title">{{ t('graphPage.needsBuildTitle') }}</p>
      <p class="graph-empty-hint">{{ t('graphPage.needsBuildHint') }}</p>
      <button class="btn-primary" :disabled="building" @click="buildGraphs">
        <Loader2 v-if="building" :size="14" class="graph-spin" />
        <Hammer v-else :size="14" />
        {{ building ? t('graphPage.building') : t('graphPage.build') }}
      </button>
    </div>

    <div v-else-if="graph" class="graph-body">
      <div class="graph-toolbar">
        <div v-if="graph.repos.length" class="graph-chip-group">
          <span class="graph-chip-label">{{ t('graphPage.reposLabel') }}</span>
          <button
            v-for="repo in graph.repos"
            :key="repo.name"
            class="graph-chip"
            :class="{ on: visibleRepos.has(repo.name), warn: !repo.has_graph }"
            :title="repo.has_graph ? undefined : t('graphPage.noGraph')"
            @click="toggleRepo(repo.name)"
          >
            <GitBranch :size="11" />
            {{ repo.name }}
          </button>
        </div>
        <div v-if="typeOptions.length" class="graph-chip-group">
          <span class="graph-chip-label">{{ t('graphPage.typesLabel') }}</span>
          <button
            class="graph-chip"
            :class="{ on: visibleTypes.size === 0 }"
            @click="visibleTypes = new Set()"
          >
            {{ t('graphPage.filterAll') }}
          </button>
          <button
            v-for="type in typeOptions"
            :key="type"
            class="graph-chip"
            :class="{ on: visibleTypes.has(type) }"
            @click="toggleType(type)"
          >
            {{ typeLabel(type) }}
          </button>
        </div>
        <input
          v-model="search"
          class="graph-search"
          type="search"
          :placeholder="t('graphPage.searchPlaceholder')"
        />
      </div>

      <div class="graph-main">
        <GraphCanvas
          v-if="graph.nodes.length > 0"
          ref="canvasRef"
          :nodes="graph.nodes"
          :edges="graph.edges"
          :visible-types="visibleTypes"
          :visible-repos="visibleRepos"
          :search="search"
          :focus-node="focusNode"
          @select="onSelect"
        />
        <div v-else class="graph-state graph-empty">
          <span class="graph-empty-icon"><Network :size="28" /></span>
          <p v-if="graph.no_repos" class="graph-empty-title">{{ t('graphPage.noReposTitle') }}</p>
          <p v-if="graph.no_repos" class="graph-empty-hint">{{ t('graphPage.noReposHint') }}</p>
          <template v-else>
            <p class="graph-empty-title">{{ t('graphPage.needsBuildTitle') }}</p>
            <p class="graph-empty-hint">{{ t('graphPage.needsBuildHint') }}</p>
          </template>
        </div>

        <GraphDetailPanel
          v-if="selectedNode"
          :node="selectedNode"
          :neighbors="selectedNeighbors"
          @close="clearSelection"
          @focus="focusNode = $event"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { GitBranch, Hammer, Loader2, Network, RefreshCw } from 'lucide-vue-next'
import {
  projectGraphApi,
  type GraphEdgeDto,
  type GraphNodeDto,
  type ProjectGraph,
} from '@/api/projectGraph'
import GraphCanvas from '@/components/graph/GraphCanvas.vue'
import GraphDetailPanel from '@/components/graph/GraphDetailPanel.vue'

const { t } = useI18n()
const route = useRoute()

const projectId = computed(() => route.params.projectId as string)

const graph = ref<ProjectGraph | null>(null)
const loading = ref(false)
const building = ref(false)
const error = ref('')
const message = ref<{ text: string; ok: boolean }>({ text: '', ok: true })

const visibleTypes = ref<Set<string>>(new Set())
const visibleRepos = ref<Set<string>>(new Set())
const search = ref('')

const selectedId = ref<string | null>(null)
const focusNode = ref<string | null>(null)
const canvasRef = ref<InstanceType<typeof GraphCanvas> | null>(null)

// Client-side adjacency of the loaded subgraph: selection neighbors come from
// here (instant, works for docs too); the server neighbors endpoint only
// serves on-demand symbol expansion.
const nodeById = computed(() => {
  const map = new Map<string, GraphNodeDto>()
  for (const node of graph.value?.nodes ?? []) map.set(node.id, node)
  return map
})

const adjacency = computed(() => {
  const map = new Map<string, GraphNodeDto[]>()
  for (const edge of graph.value?.edges ?? []) {
    const src = nodeById.value.get(edge.src)
    const dst = nodeById.value.get(edge.dst)
    if (src && dst) {
      if (!map.has(edge.src)) map.set(edge.src, [])
      if (!map.has(edge.dst)) map.set(edge.dst, [])
      map.get(edge.src)!.push(dst)
      map.get(edge.dst)!.push(src)
    }
  }
  return map
})

const selectedNode = computed(() =>
  selectedId.value ? nodeById.value.get(selectedId.value) ?? null : null,
)

const selectedNeighbors = computed(() => {
  if (!selectedId.value) return []
  const seen = new Set<string>()
  const list: GraphNodeDto[] = []
  for (const neighbor of adjacency.value.get(selectedId.value) ?? []) {
    if (!seen.has(neighbor.id)) {
      seen.add(neighbor.id)
      list.push(neighbor)
    }
  }
  return list
})

const typeOptions = computed(() => {
  const types = new Set<string>()
  for (const node of graph.value?.nodes ?? []) types.add(node.type)
  return [...types].sort()
})

const TYPE_I18N: Record<string, string> = {
  file: 'graphPage.typeFile',
  class: 'graphPage.typeClass',
  function: 'graphPage.typeFunction',
  symbol: 'graphPage.typeSymbol',
  table: 'graphPage.typeTable',
  view: 'graphPage.typeView',
  column: 'graphPage.typeColumn',
  procedure: 'graphPage.typeProcedure',
  trigger: 'graphPage.typeTrigger',
  module: 'graphPage.typeModule',
}

function typeLabel(type: string): string {
  const key = TYPE_I18N[type]
  return key ? t(key) : type
}

let loadSeq = 0

async function load(): Promise<void> {
  const pid = projectId.value
  if (!pid) return
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  try {
    const data = await projectGraphApi.getGraph(pid)
    if (seq !== loadSeq) return
    graph.value = data
    // Fresh payload: previously selected node may no longer exist.
    if (selectedId.value && !data.nodes.some((n) => n.id === selectedId.value)) {
      selectedId.value = null
    }
    // First load opens every repo; after that user chip toggles stick.
    if (visibleRepos.value.size === 0) {
      visibleRepos.value = new Set(data.repos.map((repo) => repo.name))
    }
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

async function buildGraphs(): Promise<void> {
  const pid = projectId.value
  if (!pid || building.value) return
  building.value = true
  message.value = { text: '', ok: true }
  try {
    const result = await projectGraphApi.build(pid)
    const failed = result.repos.filter(
      (repo) => repo.status !== 'built' && repo.status !== 'up_to_date',
    )
    message.value = {
      text: failed.length
        ? `${t('graphPage.buildDone')} (${failed.map((repo) => `${repo.name}: ${String(repo.status)}`).join(', ')})`
        : t('graphPage.buildDone'),
      ok: true,
    }
    await load()
  } catch (e) {
    message.value = {
      text: e instanceof Error ? e.message : String(e),
      ok: false,
    }
  } finally {
    building.value = false
  }
}

function toggleRepo(name: string): void {
  const next = new Set(visibleRepos.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  visibleRepos.value = next
}

function toggleType(type: string): void {
  const next = new Set(visibleTypes.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  visibleTypes.value = next
}

async function onSelect(nodeId: string | null): Promise<void> {
  selectedId.value = nodeId
  focusNode.value = null
  // Code nodes: pull symbol-level neighbors from the server on demand and
  // merge them into the canvas (the overview hides symbols by design; doc
  // nodes skip this — their edges already travel with the main payload).
  if (!nodeId) return
  const node = nodeById.value.get(nodeId)
  if (!node?.repo) return
  const pid = projectId.value
  if (!pid) return
  try {
    const result = await projectGraphApi.getNeighbors(pid, nodeId)
    if (!graph.value || selectedId.value !== nodeId) return
    const known = nodeById.value
    const extraNodes = result.neighbors.filter((entry) => !known.has(entry.id))
    const existing = new Set(graph.value.edges.map((e) => `${e.src}|${e.dst}|${e.type}`))
    const extraEdges = result.edges.filter(
      (edge) => !existing.has(`${edge.src}|${edge.dst}|${edge.type}`),
    )
    if (extraNodes.length || extraEdges.length) {
      graph.value = {
        ...graph.value,
        nodes: [...graph.value.nodes, ...extraNodes],
        edges: [...graph.value.edges, ...extraEdges] as GraphEdgeDto[],
      }
    }
  } catch {
    // Expansion is best-effort; the detail panel still shows what we have.
  }
}

function clearSelection(): void {
  selectedId.value = null
  focusNode.value = null
}

watch(projectId, () => {
  graph.value = null
  selectedId.value = null
  visibleTypes.value = new Set()
  visibleRepos.value = new Set()
  message.value = { text: '', ok: true }
  void load()
})

onMounted(() => {
  void load()
})
</script>

<style scoped>
/* Same center-panel fill as SchedulesPage: .center-content is a flex column
   with overflow hidden, so the page owns its own scroll and the canvas gets
   the remaining height. */
.graph-page {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  padding: 20px 22px;
}

.graph-header {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.graph-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.graph-header-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--accent-gradient);
  color: #fff;
  box-shadow: var(--accent-glow);
}

.graph-title {
  margin: 0;
  font-size: 21px;
}

.graph-sub {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--text-secondary, #9ca3af);
}

.graph-header-actions {
  display: flex;
  gap: 8px;
}

.graph-message {
  flex: none;
  margin: 0 0 8px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
}

.graph-message.success {
  background: #052e16;
  color: #86efac;
}

.graph-message.error {
  background: #450a0a;
  color: #fca5a5;
}

.graph-state {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-secondary, #9ca3af);
  font-size: 13px;
}

.graph-empty {
  flex-direction: column;
  gap: 6px;
  text-align: center;
}

.graph-empty-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 14px;
  background: var(--bg-secondary, #1f2937);
  color: var(--text-secondary, #9ca3af);
}

.graph-empty-title {
  margin: 6px 0 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary, #e5e7eb);
}

.graph-empty-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--text-secondary, #9ca3af);
}

.graph-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

.graph-toolbar {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.graph-chip-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.graph-chip-label {
  margin-right: 2px;
  font-size: 11px;
  color: var(--text-secondary, #6b7280);
}

.graph-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border: 1px solid var(--border-color, #374151);
  border-radius: 999px;
  background: none;
  color: var(--text-secondary, #9ca3af);
  font-size: 11px;
  cursor: pointer;
}

.graph-chip.on {
  border-color: #0ea5e9;
  background: #0c4a6e33;
  color: #7dd3fc;
}

.graph-chip.warn {
  border-style: dashed;
}

.graph-search {
  flex: none;
  width: 200px;
  margin-left: auto;
  padding: 5px 10px;
  border: 1px solid var(--border-color, #374151);
  border-radius: 8px;
  background: var(--bg-secondary, #111827);
  color: var(--text-primary, #e5e7eb);
  font-size: 12px;
}

.graph-main {
  display: flex;
  flex: 1;
  min-height: 0;
  border: 1px solid var(--border-color, #1f2937);
  border-radius: 12px;
  overflow: hidden;
}

.graph-main > :first-child {
  flex: 1;
  min-width: 0;
}

.graph-spin {
  animation: graph-spin 1s linear infinite;
}

@keyframes graph-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
