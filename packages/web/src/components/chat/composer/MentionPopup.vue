<template>
  <div class="mention-popup" ref="popupEl">
    <input
      v-model="query"
      class="mention-search"
      :placeholder="t('mentionPopup.searchPlaceholder')"
      autofocus
      @keydown.esc="emit('close')"
      @keydown.enter="selectCurrent"
      @keydown.arrow-down.prevent="moveDown"
      @keydown.arrow-up.prevent="moveUp"
    />
    <div class="mention-list">
      <div
        v-for="(item, i) in filtered"
        :key="item.slug"
        class="mention-item"
        :class="{ active: i === cursor }"
        @click="emit('select', item)"
      >
        <!-- Only the UI display name - the slug (md filename) is internal
             routing detail and stays searchable-but-invisible. -->
        <span class="mention-icon"><Bot :size="14" /></span>
        <span class="mention-title">{{ item.label }}</span>
      </div>
      <div v-if="!filtered.length" class="mention-empty">{{ t('mentionPopup.noResults') }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agent'

const { t } = useI18n()
const props = defineProps<{ excludeSlug?: string }>()

const emit = defineEmits<{ select: [agent: { slug: string; label: string }]; close: [] }>()

const agentStore = useAgentStore()
const query = ref('')
const cursor = ref(0)
const popupEl = ref<HTMLElement | null>(null)

// @ is reserved for agent mentions - mentioning the current agent is a no-op,
// so it stays out of the list entirely.
const allItems = computed(() =>
  agentStore.agents
    .filter((a) => a.slug !== props.excludeSlug)
    .map((a) => ({ slug: a.slug, label: a.label })),
)

const filtered = computed(() => {
  const q = query.value.toLowerCase()
  return q
    ? allItems.value.filter((i) => i.slug.includes(q) || i.label.toLowerCase().includes(q))
    : allItems.value.slice(0, 20)
})

function moveDown() { if (filtered.value.length) cursor.value = (cursor.value + 1) % filtered.value.length }
function moveUp() { if (filtered.value.length) cursor.value = (cursor.value - 1 + filtered.value.length) % filtered.value.length }
function selectCurrent() {
  if (filtered.value[cursor.value]) emit('select', filtered.value[cursor.value])
}

function handleClickOutside(e: MouseEvent) {
  if (popupEl.value && !popupEl.value.contains(e.target as Node)) emit('close')
}
onMounted(() => document.addEventListener('mousedown', handleClickOutside))
onUnmounted(() => document.removeEventListener('mousedown', handleClickOutside))
</script>
