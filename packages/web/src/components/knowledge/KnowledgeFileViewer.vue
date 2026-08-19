<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <!-- Stacked panels: each navigation level is a panel sliding from right -->
      <TransitionGroup name="knowledge-panel-slide">
        <div
          v-for="(entry, index) in navStack"
          :key="entry.slug"
          class="knowledge-viewer-panel"
          :class="{ 'is-background': index < navStack.length - 1 }"
          :style="panelStyle(index)"
        >
          <div class="modal-header">
            <div class="modal-header-left">
              <button v-if="index > 0" class="panel-back-btn" @click="navigateBack">
                <ChevronLeft :size="16" />
              </button>
              <span class="modal-header-icon">
                <FileText :size="18" />
              </span>
              <h3 class="modal-title">{{ entry.title ?? entry.slug }}</h3>
            </div>
            <button class="modal-close" @click="emit('close')">
              <X :size="16" />
            </button>
          </div>

          <!-- Breadcrumb -->
          <div v-if="entry.ancestors.length" class="knowledge-breadcrumb">
            <span
              v-for="(anc, ai) in entry.ancestors"
              :key="anc.slug"
              class="breadcrumb-item"
            >
              <a class="breadcrumb-link" @click="navigateToAncestor(ai)">{{ anc.title }}</a>
              <ChevronRight :size="10" class="breadcrumb-sep" />
            </span>
            <span class="breadcrumb-current">{{ entry.title }}</span>
          </div>

          <div v-if="entry.loading" class="knowledge-viewer-loading">
            <Loader2 :size="18" class="spin" />
            {{ t('knowledgeFileViewer.loading') }}
          </div>
          <div v-else-if="entry.error" class="knowledge-viewer-error">
            <AlertCircle :size="14" />
            {{ entry.error }}
          </div>
          <template v-else-if="entry.content !== null">
            <div class="knowledge-viewer-meta" v-if="entry.tags.length">
              <Tag :size="11" class="knowledge-meta-icon" />
              <span v-for="tag in entry.tags" :key="tag" class="knowledge-tag">{{ tag }}</span>
            </div>
            <div
              class="knowledge-viewer-content knowledge-markdown"
              v-html="renderContent(entry.content)"
              @click="handleLink($event)"
            />
            <MermaidBlock
              v-for="(code, i) in getMermaidBlocks(entry.content)"
              :key="i"
              :code="code"
            />

            <!-- Children section -->
            <div v-if="entry.children.length" class="knowledge-children-section">
              <div class="knowledge-children-header">
                <FolderOpen :size="13" />
                {{ t('knowledgeFileViewer.childrenTitle') }}
              </div>
              <div
                v-for="child in entry.children"
                :key="child.slug"
                class="knowledge-child-item"
                @click="navigateTo(child.slug)"
              >
                <FolderOpen v-if="child.has_children" :size="12" class="child-icon" />
                <FileText v-else :size="12" class="child-icon" />
                <span class="child-title">{{ child.title }}</span>
                <span v-if="child.summary" class="child-summary">{{ child.summary }}</span>
              </div>
            </div>
          </template>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileText, X, Loader2, AlertCircle, Tag, ChevronLeft, ChevronRight, FolderOpen } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { knowledgeApi } from '@/api/knowledge'
import type { KnowledgeChildItem, KnowledgeAncestor } from '@/types'
import { renderKnowledgeMarkdown } from '@/utils/markdown'
import MermaidBlock from '@/components/chat/MermaidBlock.vue'

interface NavEntry {
  slug: string
  title: string
  content: string | null
  tags: string[]
  loading: boolean
  error: string
  ancestors: KnowledgeAncestor[]
  children: KnowledgeChildItem[]
}

const props = defineProps<{ slug: string }>()
const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const projectStore = useProjectStore()
const navStack = ref<NavEntry[]>([])

function panelStyle(index: number) {
  const offset = navStack.value.length - 1 - index
  if (offset === 0) return {}
  return {
    transform: `translateX(-${offset * 24}px)`,
    opacity: Math.max(0.3, 1 - offset * 0.25).toString(),
  }
}

function renderContent(content: string) {
  return renderKnowledgeMarkdown(content).replace(/<pre class="mermaid-block"[^>]*><\/pre>/g, '')
}

function getMermaidBlocks(content: string): string[] {
  // \r? — CRLF (Windows-edited) knowledge files would otherwise never match.
  // (?:```|$) — an unclosed fence at EOF (no trailing ```) must still render.
  const matches = [...content.matchAll(/```\s*mermaid\s*\r?\n([\s\S]*?)(?:```|$)/g)]
  return matches.map((m) => m[1].trim())
}

async function loadEntry(slug: string): Promise<NavEntry> {
  const entry: NavEntry = {
    slug,
    title: slug,
    content: null,
    tags: [],
    loading: true,
    error: '',
    ancestors: [],
    children: [],
  }

  const pid = projectStore.currentProject?.project_id
  if (!pid) {
    entry.error = t('knowledgeFileViewer.noProject')
    entry.loading = false
    return entry
  }

  try {
    const [file, children, ancestors] = await Promise.all([
      knowledgeApi.getFile(pid, slug),
      knowledgeApi.getChildren(pid, slug),
      knowledgeApi.getAncestors(pid, slug),
    ])
    entry.title = file.title
    entry.content = file.content
    entry.tags = file.tags
    entry.children = children
    entry.ancestors = ancestors
  } catch (e) {
    entry.error = e instanceof Error ? e.message : t('knowledgeFileViewer.loadFailed')
  } finally {
    entry.loading = false
  }

  return entry
}

// Monotonic guard: rapid A→B clicks spawn two loadEntry calls; the slower
// response must not push a stale entry on top of the newer selection
// .
let navSeq = 0

async function navigateTo(slug: string) {
  const seq = ++navSeq
  const entry = await loadEntry(slug)
  if (seq !== navSeq) return
  // The knowledge graph may contain cycles (the browser page's getDepth
  // guards against them), so A→B→A would push a duplicate slug and the
  // :key="entry.slug" v-for gets duplicate keys. Truncate to the existing
  // entry, mirroring navigateToAncestor's findIndex dedup.
  const existingIdx = navStack.value.findIndex((e) => e.slug === slug)
  if (existingIdx >= 0) {
    navStack.value = navStack.value.slice(0, existingIdx + 1)
  } else {
    navStack.value.push(entry)
  }
}

function navigateBack() {
  if (navStack.value.length > 1) {
    navStack.value.pop()
  }
}

function navigateToAncestor(ancestorIndex: number) {
  const currentEntry = navStack.value[navStack.value.length - 1]
  if (!currentEntry) return
  const targetSlug = currentEntry.ancestors[ancestorIndex].slug
  // Pop until we find it or navigate fresh
  const existingIdx = navStack.value.findIndex((e) => e.slug === targetSlug)
  if (existingIdx >= 0) {
    navStack.value = navStack.value.slice(0, existingIdx + 1)
  } else {
    navigateTo(targetSlug)
  }
}

function handleLink(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (target.tagName === 'A' && target.classList.contains('knowledge-link')) {
    e.preventDefault()
    const slug = target.getAttribute('data-slug')
    if (slug) navigateTo(slug)
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    if (navStack.value.length > 1) {
      navigateBack()
    } else {
      emit('close')
    }
  }
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown)
  const entry = await loadEntry(props.slug)
  navStack.value = [entry]
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.knowledge-viewer-panel {
  position: absolute;
  top: 5vh;
  right: 2vw;
  width: min(640px, 90vw);
  max-height: 90vh;
  overflow-y: auto;
  background: var(--color-bg-secondary, #1e1e2e);
  border: 1px solid var(--color-border, #313244);
  border-radius: 12px;
  padding: 0;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  transition: transform 0.3s ease, opacity 0.3s ease;
}

.knowledge-viewer-panel.is-background {
  pointer-events: none;
}

.knowledge-panel-slide-enter-active {
  transition: transform 0.3s ease, opacity 0.3s ease;
}
.knowledge-panel-slide-leave-active {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.knowledge-panel-slide-enter-from {
  transform: translateX(100%);
  opacity: 0;
}
.knowledge-panel-slide-leave-to {
  transform: translateX(100%);
  opacity: 0;
}

.panel-back-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--color-text-secondary);
  padding: 4px;
  border-radius: 4px;
  display: flex;
  align-items: center;
}
.panel-back-btn:hover {
  background: var(--color-bg-hover, #313244);
  color: var(--color-text-primary);
}

.knowledge-breadcrumb {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 4px 20px 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
  flex-wrap: wrap;
}
.breadcrumb-link {
  cursor: pointer;
  color: var(--color-accent, #89b4fa);
  text-decoration: none;
}
.breadcrumb-link:hover {
  text-decoration: underline;
}
.breadcrumb-sep {
  opacity: 0.5;
  margin: 0 2px;
}
.breadcrumb-current {
  color: var(--color-text-primary);
}

.knowledge-children-section {
  border-top: 1px solid var(--color-border, #313244);
  margin-top: 16px;
  padding: 12px 20px;
}
.knowledge-children-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
  color: var(--color-text-secondary);
}
.knowledge-child-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.knowledge-child-item:hover {
  background: var(--color-bg-hover, #313244);
}
.child-icon {
  flex-shrink: 0;
  color: var(--color-text-secondary);
}
.child-title {
  font-weight: 500;
}
.child-summary {
  color: var(--color-text-secondary);
  font-size: 12px;
  margin-left: auto;
  text-align: right;
  max-width: 50%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-children-badge {
  background: var(--color-accent, #89b4fa);
  color: var(--color-bg-primary, #1e1e2e);
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 8px;
  margin-left: 4px;
}
</style>
