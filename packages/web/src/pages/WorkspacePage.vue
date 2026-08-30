<template>
  <div class="workspace-page">
    <!-- Left: file tree -->
    <div class="workspace-sidebar">
      <div class="workspace-header">
        <h2 class="page-title">{{ t('workspace.title') }}</h2>
        <button class="workspace-refresh" :title="t('workspace.refresh')" @click="reloadTree">
          <RefreshCw :size="13" :class="{ spin: loadingRoot }" />
        </button>
      </div>
      <div v-if="error" class="workspace-error">{{ error }}</div>
      <div v-if="loadingRoot && !nodes.length" class="workspace-loading">
        <Loader2 :size="16" class="spin" />
      </div>
      <FileTree
        v-if="nodes.length"
        :nodes="nodes"
        @select="onSelect"
        @toggle="onToggle"
      />
      <div v-else-if="!loadingRoot" class="workspace-empty">{{ t('workspace.emptyTree') }}</div>
    </div>

    <!-- Right: content -->
    <div class="workspace-content">
      <!-- File viewer/editor -->
      <template v-if="fileSelection">
        <div class="workspace-content-header">
          <h3>{{ fileSelection.path }}</h3>
          <div class="workspace-content-actions">
            <button v-if="!editing" class="btn-ghost" @click="startEdit">
              <Pencil :size="13" /> {{ t('workspace.edit') }}
            </button>
            <template v-else>
              <button class="btn-ghost" @click="cancelEdit">{{ t('common.cancel') }}</button>
              <button class="btn-primary" :disabled="saving" @click="saveEdit">
                {{ saving ? t('common.saving') : t('common.save') }}
              </button>
            </template>
          </div>
        </div>

        <div v-if="fileLoading" class="workspace-content-loading">
          <Loader2 :size="18" class="spin" /> {{ t('workspace.loadingFile') }}
        </div>
        <div v-else-if="fileError" class="workspace-file-error">{{ fileError }}</div>
        <template v-else-if="fileContent !== null">
          <div v-if="!editing" class="workspace-md-body markdown-body" v-html="htmlContent" @click="handleLink" />
          <textarea v-else v-model="editContent" class="workspace-editor-textarea" spellcheck="false" />
        </template>
      </template>

      <!-- Script runner -->
      <template v-else-if="scriptSelection">
        <div class="workspace-content-header">
          <h3>
            <Terminal v-if="scriptSelection.kind === 'script'" :size="14" />
            <FlaskConical v-else :size="14" />
            {{ scriptSelection.name }}
          </h3>
          <div class="workspace-content-actions">
            <button class="btn-primary" :disabled="running" @click="runSelection">
              <Play :size="13" /> {{ running ? t('workspace.running') : t('workspace.run') }}
            </button>
          </div>
        </div>
        <div class="workspace-run-panel">
          <div v-if="runError" class="script-run-error">{{ runError }}</div>
          <div v-if="!activeRunId" class="workspace-run-empty">
            {{ t('workspace.runHint') }}
          </div>
          <div v-else class="script-log-output" ref="logPanel">
            <div
              v-for="(line, i) in logs"
              :key="i"
              class="script-log-line"
              :class="line.level"
            >{{ line.text }}</div>
          </div>
        </div>
      </template>

      <!-- Empty -->
      <div v-else class="workspace-empty-content">
        {{ t('workspace.selectHint') }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { Loader2, Pencil, Play, RefreshCw, Terminal, FlaskConical } from 'lucide-vue-next'
import { apiFetch } from '@/api/client'
import { apiBase } from '@/utils/basePath'
import { workspaceApi } from '@/api/workspace'
import { renderKnowledgeMarkdown } from '@/utils/markdown'
import type { WorkspaceTreeNode, WorkspaceNodeKind } from '@/types/workspace'
import FileTree from '@/components/workspace/FileTree.vue'

interface LogLine { text: string; level: string }
interface ScriptItem { script_id: string; run_id?: string; name: string; status: string; script_type: string }
interface SpecItem { slug: string; run_id?: string; title: string; file: string; status: string }

const route = useRoute()
const { t } = useI18n()
const projectId = computed(() => route.params.projectId as string)

const nodes = ref<WorkspaceTreeNode[]>([])
const loadingRoot = ref(false)
const error = ref<string | null>(null)
let loadSeq = 0

// Selection
interface Selection {
  kind: WorkspaceNodeKind
  path: string
  name: string
  scriptId?: string
  specSlug?: string
  file?: string
}
const selection = ref<Selection | null>(null)

// Template helpers: narrow the nullable selection by kind so the viewer/runner
// branches can access their fields without repeated null guards.
const fileSelection = computed(() =>
  selection.value?.kind === 'file' ? selection.value : null,
)
const scriptSelection = computed(() =>
  selection.value?.kind === 'script' || selection.value?.kind === 'playwright'
    ? selection.value
    : null,
)

// File viewer state
const fileLoading = ref(false)
const fileError = ref<string | null>(null)
const fileContent = ref<string | null>(null)
const editing = ref(false)
const editContent = ref('')
const saving = ref(false)

// Script runner state
const scripts = ref<ScriptItem[]>([])
const specs = ref<SpecItem[]>([])
const activeRunId = ref<string | null>(null)
const logs = ref<LogLine[]>([])
const running = ref(false)
const runError = ref<string | null>(null)
const logPanel = ref<HTMLElement | null>(null)
let ws: WebSocket | null = null
const mounted = ref(true)

// ── Tree building ────────────────────────────────────────────────
function makeDirNode(entry: { name: string; path: string }): WorkspaceTreeNode {
  return {
    key: `dir:${entry.path}`,
    name: entry.name,
    path: entry.path,
    type: 'dir',
    kind: 'dir',
    expanded: false,
    selected: false,
    children: [],
    runnable: false,
  }
}

function makeFileNode(entry: { name: string; path: string }): WorkspaceTreeNode {
  return {
    key: `file:${entry.path}`,
    name: entry.name,
    path: entry.path,
    type: 'file',
    kind: 'file',
    expanded: false,
    selected: false,
    runnable: false,
  }
}

// Build the root tree: directory entries + injected script/spec nodes.
async function loadTree() {
  const pid = projectId.value
  if (!pid) return
  const seq = ++loadSeq
  loadingRoot.value = true
  error.value = null
  try {
    const [data, scriptList] = await Promise.all([
      workspaceApi.listDir(pid),
      fetchScripts(),
    ])
    if (seq !== loadSeq) return
    nodes.value = data.entries.map((e) =>
      e.type === 'dir' ? makeDirNode(e) : makeFileNode(e),
    )
    appendScriptNodes(nodes.value, scriptList)
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = e instanceof Error ? e.message : t('workspace.loadFailed')
  } finally {
    if (seq === loadSeq) loadingRoot.value = false
  }
}

async function fetchScripts(): Promise<{ scripts: ScriptItem[]; specs: SpecItem[] }> {
  const pid = projectId.value
  if (!pid) return { scripts: [], specs: [] }
  try {
    const [s, sp] = await Promise.all([
      apiFetch<ScriptItem[]>(`/api/projects/${encodeURIComponent(pid)}/scripts`),
      apiFetch<SpecItem[]>(`/api/projects/${encodeURIComponent(pid)}/playwright/specs`),
    ])
    // Keep the raw lists for selection lookup (scriptId/specSlug), not just
    // the tree nodes built from them.
    scripts.value = s
    specs.value = sp
    return { scripts: s, specs: sp }
  } catch {
    return { scripts: [], specs: [] }
  }
}

// Virtual nodes representing runnable scripts, pinned at the top of the tree.
function appendScriptNodes(root: WorkspaceTreeNode[], list: { scripts: ScriptItem[]; specs: SpecItem[] }) {
  const scriptNodes: WorkspaceTreeNode[] = list.scripts.map((sc) => ({
    key: `script:${sc.script_id}`,
    name: sc.name,
    path: `script:${sc.script_id}`,
    type: 'file',
    kind: 'script',
    expanded: false,
    selected: false,
    runnable: true,
    badge: sc.script_type,
  }))
  const specNodes: WorkspaceTreeNode[] = list.specs.map((sp) => ({
    key: `spec:${sp.slug}`,
    name: sp.title,
    path: sp.file,
    type: 'file',
    kind: 'playwright',
    expanded: false,
    selected: false,
    runnable: true,
    badge: 'playwright',
  }))
  // Inject a virtual "scripts" dir at the top so both runnable types group
  // together regardless of what exists on disk.
  if (scriptNodes.length || specNodes.length) {
    const dir: WorkspaceTreeNode = {
      key: 'dir:__scripts__',
      name: t('workspace.scriptsGroup'),
      path: '__scripts__',
      type: 'dir',
      kind: 'dir',
      expanded: true,
      selected: false,
      children: [...scriptNodes, ...specNodes],
      runnable: false,
    }
    root.unshift(dir)
  }
}

function reloadTree() {
  loadTree()
}

// Lazy directory expansion. Real dirs have node.path = relative path on disk
// (e.g. "knowledge"); the virtual scripts group has key "dir:__scripts__" and
// is never re-loaded.
async function onToggle(node: WorkspaceTreeNode) {
  node.expanded = !node.expanded
  if (node.type !== 'dir' || node.key === 'dir:__scripts__') return
  if (!node.expanded || node.children?.length) return // already loaded
  node.loading = true
  try {
    const data = await workspaceApi.listDir(projectId.value, node.path)
    node.children = data.entries.map((e) =>
      e.type === 'dir' ? makeDirNode(e) : makeFileNode(e),
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('workspace.loadFailed')
  } finally {
    node.loading = false
  }
}

// Selection
function deselectAll() {
  const walk = (list: WorkspaceTreeNode[]) => {
    for (const n of list) {
      n.selected = false
      if (n.children) walk(n.children)
    }
  }
  walk(nodes.value)
}

function onSelect(node: WorkspaceTreeNode) {
  deselectAll()
  node.selected = true
  if (node.kind === 'script' || node.kind === 'playwright') {
    // Ensure latest script/spec metadata
    if (node.kind === 'script') {
      const sc = scripts.value.find((s) => `script:${s.script_id}` === node.key)
      selection.value = {
        kind: 'script',
        path: node.path,
        name: node.name,
        scriptId: sc?.script_id,
      }
    } else {
      const sp = specs.value.find((s) => s.file === node.path)
      selection.value = {
        kind: 'playwright',
        path: node.path,
        name: node.name,
        specSlug: sp?.slug,
        file: sp?.file,
      }
    }
    return
  }
  // Regular file → load content
  selection.value = {
    kind: 'file',
    path: node.path,
    name: node.name,
  }
  void loadFile(node.path)
}

// Monotonic guard: a slow in-flight file load must not clobber the result of
// a newer selection (rapid switching between files in the tree).
let fileLoadSeq = 0

async function loadFile(path: string) {
  const pid = projectId.value
  const seq = ++fileLoadSeq
  fileLoading.value = true
  fileError.value = null
  editing.value = false
  try {
    const data = await workspaceApi.readFile(pid, path)
    if (seq !== fileLoadSeq || projectId.value !== pid) return
    fileContent.value = data.content
    editContent.value = data.content
  } catch (e) {
    if (seq !== fileLoadSeq || projectId.value !== pid) return
    fileError.value = e instanceof Error ? e.message : t('workspace.loadFileFailed')
    fileContent.value = null
  } finally {
    if (seq === fileLoadSeq) fileLoading.value = false
  }
}

// ── md editing ──────────────────────────────────────────────────
const htmlContent = computed(() => {
  if (!fileContent.value) return ''
  return renderKnowledgeMarkdown(fileContent.value)
    .replace(/<pre class="mermaid-block"[^>]*><\/pre>/g, '')
})

function handleLink(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (target.tagName === 'A' && target.classList.contains('knowledge-link')) {
    e.preventDefault()
    const slug = target.getAttribute('data-slug')
    if (slug && selection.value) {
      const path = `knowledge/${slug}.md`
      deselectAll()
      selection.value = { kind: 'file', path, name: slug }
      void loadFile(path)
    }
  }
}

function startEdit() {
  editContent.value = fileContent.value ?? ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editContent.value = fileContent.value ?? ''
}

async function saveEdit() {
  if (!selection.value || selection.value.kind !== 'file') return
  const pid = projectId.value
  const path = selection.value.path
  saving.value = true
  fileError.value = null
  try {
    await workspaceApi.saveFile(pid, path, editContent.value)
    if (selection.value?.path === path && projectId.value === pid) {
      fileContent.value = editContent.value
      editing.value = false
    }
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : t('workspace.saveFailed')
  } finally {
    saving.value = false
  }
}

// ── Script runner ───────────────────────────────────────────────
async function runSelection() {
  if (!selection.value) return
  const { kind } = selection.value
  if (kind === 'script') {
    await runScript(selection.value.scriptId)
  } else if (kind === 'playwright') {
    await runSpec(selection.value.specSlug)
  }
}

async function runScript(scriptId: string | undefined) {
  if (!scriptId) return
  running.value = true
  runError.value = null
  const pidAtStart = projectId.value
  try {
    const result = await apiFetch<{ run_id: string; status: string }>(
      `/api/scripts/${scriptId}/run`,
      { method: 'POST' },
    )
    if (projectId.value !== pidAtStart) return
    const sc = scripts.value.find((s) => s.script_id === scriptId)
    if (sc) { sc.run_id = result.run_id; sc.status = result.status }
    connectToRun(result.run_id)
  } catch (e) {
    runError.value = e instanceof Error ? e.message : t('workspace.runFailed')
  } finally {
    running.value = false
  }
}

async function runSpec(slug: string | undefined) {
  if (!slug) return
  running.value = true
  runError.value = null
  const pidAtStart = projectId.value
  try {
    const result = await apiFetch<{ run_id: string; status: string }>(
      `/api/projects/${encodeURIComponent(pidAtStart)}/playwright/specs/${encodeURIComponent(slug)}/run`,
      { method: 'POST', body: JSON.stringify({}) },
    )
    if (projectId.value !== pidAtStart) return
    const sp = specs.value.find((s) => s.slug === slug)
    if (sp) { sp.run_id = result.run_id; sp.status = result.status }
    connectToRun(result.run_id)
  } catch (e) {
    runError.value = e instanceof Error ? e.message : t('workspace.runFailed')
  } finally {
    running.value = false
  }
}

function pushLog(line: LogLine) {
  const el = logPanel.value
  const nearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80
  logs.value.push(line)
  if (nearBottom) {
    void nextTick(() => {
      if (logPanel.value) logPanel.value.scrollTop = logPanel.value.scrollHeight
    })
  }
}

function connectToRun(runId: string) {
  if (ws) { ws.close(); ws = null }
  activeRunId.value = runId
  logs.value = []
  runError.value = null

  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}${apiBase}/ws/script-runs/${runId}`)
  ws.onmessage = (e) => {
    if (!mounted.value || activeRunId.value !== runId) return
    let msg: { type?: string; level?: string; text?: string; log?: string; status?: string }
    try {
      msg = JSON.parse(e.data as string) as typeof msg
    } catch {
      pushLog({ text: String(e.data), level: 'info' })
      return
    }
    if (msg.type === 'done') {
      return
    }
    pushLog({ text: msg.text ?? msg.log ?? String(e.data), level: msg.level ?? 'info' })
  }
  ws.onclose = () => {
    if (!mounted.value || activeRunId.value !== runId) return
    pushLog({ text: t('workspace.connectionLost'), level: 'error' })
  }
}

// ── Lifecycle & project switching ───────────────────────────────
onMounted(() => {
  loadTree()
})

watch(projectId, () => {
  // Project switched while this page stays mounted: drop the socket tied to
  // the previous project's run and reset the selection. mounted stays true —
  // it guards only the component's own lifetime (see onBeforeUnmount).
  ws?.close()
  ws = null
  fileLoadSeq++ // invalidate any in-flight file load from the old project
  nodes.value = []
  scripts.value = []
  specs.value = []
  selection.value = null
  fileContent.value = null
  editing.value = false
  activeRunId.value = null
  logs.value = []
  runError.value = null
  running.value = false
  loadTree()
})

onBeforeUnmount(() => {
  mounted.value = false
  ws?.close()
})
</script>
