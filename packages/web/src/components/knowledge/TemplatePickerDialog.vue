<template>
  <Teleport to="body">
    <div class="modal-overlay" ref="overlayEl" @keydown.esc="emit('close')">
      <div class="modal-dialog picker-dialog">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon"><CopyPlus :size="18" /></span>
            <h3 class="modal-title">{{ t('templatePicker.title') }}</h3>
          </div>
          <button class="modal-close" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">{{ t('templatePicker.hint') }}</p>

        <div v-if="loadError" class="modal-error">{{ t('templatePicker.loadFailed') }}</div>
        <template v-else>
          <div class="picker-list">
            <template v-for="group in grouped" :key="group.category">
              <div class="picker-category-heading">{{ group.label }}</div>
              <div
                v-for="tpl in group.templates"
                :key="tpl.slug"
                class="picker-item template-item"
              >
                <div class="picker-item-info">
                  <span class="picker-item-name">{{ tpl.title }}</span>
                  <span class="picker-item-desc">{{ tpl.slug }}</span>
                </div>
                <span v-if="tpl.exists" class="template-exists-badge">
                  {{ t('templatePicker.exists') }}
                </span>
                <button
                  class="btn-primary template-copy-btn"
                  :disabled="tpl.exists || copyBusy"
                  @click="copy(tpl)"
                >
                  <template v-if="justCopied === tpl.slug">{{ t('templatePicker.copied') }}</template>
                  <template v-else>{{ t('templatePicker.copy') }}</template>
                </button>
              </div>
            </template>
            <div v-if="templates.length === 0" class="picker-empty empty-state">
              <span>{{ t('templatePicker.empty') }}</span>
            </div>
          </div>
          <p v-if="error" class="modal-error">{{ error }}</p>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { CopyPlus, X } from 'lucide-vue-next'
import { knowledgeApi } from '@/api/knowledge'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useClickOutside } from '@/composables/useClickOutside'
import type { KnowledgeTemplate } from '@/api/knowledge'

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ close: []; copied: [slug: string] }>()
const overlayEl = ref<HTMLElement | null>(null)
useClickOutside(overlayEl, () => emit('close'), true)

const { t } = useI18n()
const knowledgeStore = useKnowledgeStore()

const templates = ref<KnowledgeTemplate[]>([])
const loadError = ref(false)
const copyingSlug = ref<string | null>(null)
const justCopied = ref<string | null>(null)
const error = ref('')

const copyBusy = computed(() => copyingSlug.value !== null)

// '' must stay last: unknown categories fall into that bucket, mirroring
// AgentPickerDialog's grouping contract.
const categoryOrder = ['domain', 'environment', 'integrations', 'skills', 'system', 'technical', '']
const groupLabels = computed<Record<string, string>>(() => ({
  domain: t('templatePicker.groupDomain'),
  environment: t('templatePicker.groupEnvironment'),
  integrations: t('templatePicker.groupIntegrations'),
  skills: t('templatePicker.groupSkills'),
  system: t('templatePicker.groupSystem'),
  technical: t('templatePicker.groupTechnical'),
  '': t('templatePicker.groupOther'),
}))

const grouped = computed(() =>
  categoryOrder
    .map((category) => ({
      category,
      label: groupLabels.value[category],
      // Server list is slug-sorted; filtering preserves that order inside
      // each fixed category bucket — stable category+slug grouping.
      templates: templates.value.filter((tpl) => (tpl.category || '') === category),
    }))
    .filter((group) => group.templates.length > 0),
)

onMounted(async () => {
  try {
    templates.value = await knowledgeApi.listTemplates(props.projectId)
  } catch {
    loadError.value = true
  }
})

let copiedTimer: ReturnType<typeof setTimeout> | undefined
onUnmounted(() => clearTimeout(copiedTimer))

async function copy(tpl: KnowledgeTemplate) {
  if (copyBusy.value || tpl.exists) return
  copyingSlug.value = tpl.slug
  error.value = ''
  try {
    // created:false (already existed) has the same outcome — the file is
    // there either way, so open it. Copy never overwrites.
    await knowledgeApi.copyTemplate(props.projectId, tpl.slug)
    knowledgeStore.triggerRefresh()
    justCopied.value = tpl.slug
    // Brief「已复制」flash so the click has visible feedback before the
    // parent closes this dialog and opens the viewer.
    copiedTimer = setTimeout(() => emit('copied', tpl.slug), 350)
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('templatePicker.copyFailed')
  } finally {
    copyingSlug.value = null
  }
}
</script>

<style scoped>
.template-item {
  cursor: default;
}
.template-exists-badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--bg-tertiary);
  color: var(--text-muted);
  border: 1px solid var(--border);
}
.template-copy-btn {
  flex-shrink: 0;
  font-size: 12px;
  padding: 4px 10px;
  min-width: 56px;
}
.template-copy-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  transform: none;
}
.picker-empty {
  padding: 24px 0;
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
}
</style>
