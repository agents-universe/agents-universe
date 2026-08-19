<template>
  <div class="knowledge-browser-page">
    <div class="knowledge-browser-sidebar">
      <div class="knowledge-browser-header">
        <h2 class="page-title">{{ t('knowledgePanel.allItems') }}</h2>
      </div>
      <div class="knowledge-tree">
        <div
          v-for="item in treeItems"
          :key="item.knowledge_id"
          class="knowledge-tree-item"
          :class="{ active: selectedSlug === item.slug, index: item.knowledge_level === 'index' }"
          :style="{ paddingLeft: `${item.depth * 12 + 12}px` }"
          @click="selectedSlug = item.slug"
        >
          <span class="knowledge-tree-icon">{{ item.knowledge_level === 'index' ? '📂' : '📄' }}</span>
          <span class="knowledge-tree-title">{{ item.title }}</span>
          <span class="knowledge-tree-score">{{ Math.round(item.completeness_score) }}%</span>
        </div>
      </div>
    </div>

    <div class="knowledge-browser-content">
      <div v-if="error" class="knowledge-browser-error">{{ error }}</div>
      <div v-if="!selectedSlug" class="knowledge-browser-empty">{{ t('knowledgeBrowserPage.selectHint') }}</div>
      <template v-else>
        <div class="knowledge-content-header">
          <h3>{{ currentFile?.title ?? selectedSlug }}</h3>
          <div class="knowledge-content-actions">
            <button v-if="!editing" class="btn-ghost" @click="startEdit">{{ t('knowledgeBrowserPage.edit') }}</button>
            <template v-else>
              <button class="btn-ghost" @click="cancelEdit">{{ t('common.cancel') }}</button>
              <button class="btn-primary" :disabled="saving" @click="saveEdit">
                {{ saving ? t('common.saving') : t('common.save') }}
              </button>
            </template>
          </div>
        </div>

        <div v-if="loading" class="knowledge-content-loading">{{ t('knowledgeFileViewer.loading') }}</div>
        <template v-else-if="currentFile">
          <template v-if="!editing">
            <div class="knowledge-content-body markdown-body" v-html="htmlContent" @click="handleKnowledgeLink" />
            <MermaidBlock
              v-for="(code, i) in mermaidBlocks"
              :key="i"
              :code="code"
            />
          </template>
          <textarea
            v-else
            v-model="editContent"
            class="knowledge-editor-textarea"
          />
        </template>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useKnowledgeStore } from '@/stores/knowledge'
import { knowledgeApi } from '@/api/knowledge'
import { renderKnowledgeMarkdown } from '@/utils/markdown'
import { useKnowledgeData } from '@/composables/useKnowledgeData'
import MermaidBlock from '@/components/chat/MermaidBlock.vue'

const route = useRoute()
const { t } = useI18n()
const knowledgeStore = useKnowledgeStore()

const projectId = computed(() => route.params.projectId as string)
useKnowledgeData(projectId)

// Direct URL navigation between projects reuses this component — reset the
// local selection so an edit can't be saved into the newly routed project
// under the OLD project's slug/content (cross-project overwrite).
watch(projectId, () => {
  selectedSlug.value = null
  currentFile.value = null
  editing.value = false
  editContent.value = ''
  error.value = null
  // An in-flight load for the previous project bumps fileLoadSeq and skips
  // its own finally — without this reset, loading stays stuck at true until
  // the next file selection .
  loading.value = false
  // Invalidate any in-flight file load from the previous project: without
  // this, a slow getFile(A) response still matches its seq and its content
  // is shown inside the new project's browser page.
  fileLoadSeq++
})

const selectedSlug = ref<string | null>(null)
const currentFile = ref<Awaited<ReturnType<typeof knowledgeApi.getFile>> | null>(null)
const loading = ref(false)
const editing = ref(false)
const editContent = ref('')
const saving = ref(false)
const error = ref<string | null>(null)

// Build tree with depth from parent_slug chain
const treeItems = computed(() => {
  const items = knowledgeStore.items
  const slugToDepth = new Map<string, number>()
  function getDepth(slug: string): number {
    // Mark visited BEFORE recursing: a parent_slug cycle (A→B→A) would
    // otherwise recurse forever (the node never reaches memo) and stack-overflow
    // the page. Re-entry in a cycle is treated as depth 0.
    if (slugToDepth.has(slug)) return slugToDepth.get(slug)!
    slugToDepth.set(slug, 0)
    const item = items.find((i) => i.slug === slug)
    const depth = item?.parent_slug ? getDepth(item.parent_slug) + 1 : 0
    slugToDepth.set(slug, depth)
    return depth
  }
  return items.map((i) => ({ ...i, depth: getDepth(i.slug) }))
})

const renderedContent = computed(() =>
  currentFile.value ? renderKnowledgeMarkdown(currentFile.value.content) : '',
)

const htmlContent = computed(() =>
  renderedContent.value.replace(/<pre class="mermaid-block"[^>]*><\/pre>/g, ''),
)

const mermaidBlocks = computed(() => {
  if (!currentFile.value) return []
  // \r? — knowledge files edited on Windows carry CRLF, which would otherwise
  // make the fence never match and silently drop every mermaid block.
  // (?:```|$) — an unclosed fence at EOF (no trailing ```) must still render.
  const matches = [...currentFile.value.content.matchAll(/```\s*mermaid\s*\r?\n([\s\S]*?)(?:```|$)/g)]
  return matches.map((m) => m[1].trim())
})

// Monotonic guard: a slow in-flight file load must not clobber the result of
// a newer selection (rapid switching between knowledge files).
let fileLoadSeq = 0

watch(selectedSlug, async (slug) => {
  if (!slug || !projectId.value) return
  const seq = ++fileLoadSeq
  loading.value = true
  editing.value = false
  error.value = null
  try {
    const file = await knowledgeApi.getFile(projectId.value, slug)
    if (seq !== fileLoadSeq) return
    currentFile.value = file
    editContent.value = file.content
  } catch (e) {
    if (seq === fileLoadSeq) {
      error.value = e instanceof Error ? e.message : t('knowledgeBrowserPage.loadFailed')
      console.error('Failed to load file', e)
    }
  } finally {
    if (seq === fileLoadSeq) loading.value = false
  }
})

function startEdit() {
  editContent.value = currentFile.value?.content ?? ''
  editing.value = true
}

function handleKnowledgeLink(e: MouseEvent) {
  // [[slug]] cross-references render as <a class="knowledge-link"
  // data-slug="..."> — without a handler the default anchor action jumps
  // the page to the top. Navigate to the referenced file instead (same
  // delegation as KnowledgeFileViewer).
  const target = e.target as HTMLElement
  if (target.tagName === 'A' && target.classList.contains('knowledge-link')) {
    e.preventDefault()
    const slug = target.getAttribute('data-slug')
    if (slug) {
      selectedSlug.value = slug
      editing.value = false
      editContent.value = ''
    }
  }
}

function cancelEdit() {
  editing.value = false
}

async function saveEdit() {
  if (!selectedSlug.value || !projectId.value) return
  const slug = selectedSlug.value
  const pid = projectId.value
  saving.value = true
  error.value = null
  try {
    await knowledgeApi.saveFile(pid, slug, editContent.value)
    // Only write the edited content back into the display when the user is
    // still looking at the SAME file — a slow save for A must not overwrite
    // the freshly loaded content of B .
    if (selectedSlug.value === slug && projectId.value === pid && currentFile.value) {
      currentFile.value = { ...currentFile.value, content: editContent.value }
      editing.value = false
    }
    knowledgeStore.triggerRefresh()
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('knowledgeBrowserPage.saveFailed')
    console.error('Failed to save file', e)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.knowledge-browser-error {
  font-size: 0.8rem;
  color: var(--color-danger, #e53e3e);
  padding: 0.5rem 1rem;
  background: color-mix(in srgb, var(--color-danger, #e53e3e) 8%, transparent);
  border-radius: 6px;
  margin-bottom: 0.5rem;
}
</style>
