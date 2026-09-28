<template>
  <div class="knowledge-panel">
    <!-- Completeness -->
    <div class="knowledge-panel-section" v-if="Object.keys(knowledgeStore.completeness).length">
      <div class="section-label">
        <BarChart3 :size="12" class="section-label-icon" />
        {{ t('knowledgePanel.completeness') }}
      </div>
      <CompletenessBar
        v-for="(score, cat) in knowledgeStore.completeness"
        :key="cat"
        :category="String(cat)"
        :score="score"
      />
    </div>

    <!-- Loaded this turn -->
    <div class="knowledge-panel-section" v-if="knowledgeStore.loadedThisTurn.length">
      <div class="section-label">
        <Zap :size="12" class="section-label-icon" />
        {{ t('knowledgePanel.loadedThisTurn') }}
      </div>
      <div class="knowledge-slug-list">
        <span
          v-for="slug in knowledgeStore.loadedThisTurn"
          :key="slug"
          class="knowledge-slug-chip loaded"
          @click="viewerSlug = slug"
        >
          <FileCheck :size="10" />
          {{ slug }}
        </span>
      </div>
    </div>

    <!-- Dynamically loaded -->
    <div class="knowledge-panel-section" v-if="knowledgeStore.dynamicallyLoaded.length">
      <div class="section-label">
        <RefreshCw :size="12" class="section-label-icon" />
        {{ t('knowledgePanel.dynamicLoaded') }}
      </div>
      <div class="knowledge-slug-list">
        <span
          v-for="item in knowledgeStore.dynamicallyLoaded"
          :key="item.slug"
          class="knowledge-slug-chip dynamic"
          @click="viewerSlug = item.slug"
        >
          <Sparkles :size="10" />
          {{ item.slug }}
        </span>
      </div>
    </div>

    <!-- All items (root nodes only) -->
    <div class="knowledge-panel-section">
      <div class="section-label">
        <span class="section-label-text">
          <Library :size="12" class="section-label-icon" />
          {{ t('knowledgePanel.allItems') }}
        </span>
        <button
          v-if="projectId"
          class="knowledge-copy-template-btn"
          :title="t('knowledgePanel.copyTemplate')"
          @click="showTemplatePicker = true"
        >
          <CopyPlus :size="13" />
        </button>
      </div>
      <div class="knowledge-item-list">
        <div
          v-for="item in rootItems"
          :key="item.knowledge_id"
          class="knowledge-item"
          @click="viewerSlug = item.slug"
        >
          <FolderOpen v-if="item.children_slugs.length > 0" :size="13" class="knowledge-item-icon" />
          <FileText v-else :size="13" class="knowledge-item-icon" />
          <span class="knowledge-item-title">{{ item.title }}</span>
          <span v-if="item.is_global" class="knowledge-global-badge">
            {{ t('knowledgePanel.systemBadge') }}
          </span>
          <span v-if="item.children_slugs.length" class="knowledge-children-badge">
            {{ item.children_slugs.length }}
          </span>
          <span class="knowledge-item-score" :class="scoreClass(item.completeness_score)">
            {{ Math.round(item.completeness_score) }}%
          </span>
        </div>
      </div>
    </div>

    <Transition name="modal">
      <KnowledgeFileViewer
        v-if="viewerSlug"
        :key="viewerSlug"
        :slug="viewerSlug"
        @close="viewerSlug = null"
      />
    </Transition>

    <TemplatePickerDialog
      v-if="showTemplatePicker && projectId"
      :project-id="projectId"
      @close="showTemplatePicker = false"
      @copied="onTemplateCopied"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BarChart3, Zap, FileCheck, RefreshCw, Sparkles, Library, FolderOpen, FileText, CopyPlus } from 'lucide-vue-next'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useKnowledgeData } from '@/composables/useKnowledgeData'
import CompletenessBar from './CompletenessBar.vue'
import KnowledgeFileViewer from './KnowledgeFileViewer.vue'
import TemplatePickerDialog from './TemplatePickerDialog.vue'

const props = defineProps<{ projectId?: string }>()
const { t } = useI18n()
const knowledgeStore = useKnowledgeStore()
const viewerSlug = ref<string | null>(null)
const showTemplatePicker = ref(false)

// A viewer opened for the previous project must not survive a project switch:
// its slug would resolve against the new project's knowledge (wrong file or
// a 404). Close it — the viewer's navStack is rebuilt from scratch on reopen.
// Same for the template picker: its projectId prop would go stale.
watch(() => props.projectId, () => {
  viewerSlug.value = null
  showTemplatePicker.value = false
})

const projectIdRef = computed(() => props.projectId)
useKnowledgeData(projectIdRef)

const rootItems = computed(() =>
  knowledgeStore.items.filter((item) => !item.parent_slug)
)

function onTemplateCopied(slug: string) {
  showTemplatePicker.value = false
  viewerSlug.value = slug
}

function scoreClass(score: number) {
  if (score >= 90) return 'score-green'
  if (score >= 60) return 'score-amber'
  return 'score-red'
}
</script>

<style scoped>
.section-label-text {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.knowledge-copy-template-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  padding: 3px;
  border-radius: 5px;
  display: flex;
  align-items: center;
  transition: color 0.15s, background 0.15s;
}
.knowledge-copy-template-btn:hover {
  color: var(--accent);
  background: var(--bg-tertiary);
}
.knowledge-global-badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 8px;
  background: var(--bg-tertiary);
  color: var(--text-muted);
  border: 1px solid var(--border);
}
</style>
