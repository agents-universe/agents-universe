<template>
  <div class="slash-popup" ref="popupEl">
    <div class="mention-list">
      <div
        v-for="(cmd, i) in COMMANDS"
        :key="cmd.slug"
        class="mention-item"
        :class="{ active: i === cursor }"
        @click="emit('select', cmd.slug)"
      >
        <span class="mention-slug">/{{ cmd.slug }}</span>
        <span class="mention-title">{{ cmd.description }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useClickOutside } from '@/composables/useClickOutside'

const emit = defineEmits<{ select: [slug: string]; close: [] }>()

const { t } = useI18n()

const COMMANDS = [
  { slug: 'plan', description: t('slashCommands.plan') },
  { slug: 'review', description: t('slashCommands.review') },
  { slug: 'summarize', description: t('slashCommands.summarize') },
  { slug: 'test', description: t('slashCommands.test') },
  { slug: 'refactor', description: t('slashCommands.refactor') },
  { slug: 'explain', description: t('slashCommands.explain') },
  { slug: 'fix', description: t('slashCommands.fix') },
  { slug: 'generate', description: t('slashCommands.generate') },
]

const cursor = ref(0)
const popupEl = ref<HTMLElement | null>(null)

function moveDown() { cursor.value = (cursor.value + 1) % COMMANDS.length }
function moveUp() { cursor.value = (cursor.value - 1 + COMMANDS.length) % COMMANDS.length }
function selectCurrent() { emit('select', COMMANDS[cursor.value].slug) }

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') { e.preventDefault(); moveDown() }
  else if (e.key === 'ArrowUp') { e.preventDefault(); moveUp() }
  else if (e.key === 'Enter') { e.preventDefault(); selectCurrent() }
  else if (e.key === 'Escape') { emit('close') }
}

// Close only on a complete outside click (press + release both outside);
// a press inside the popup released outside is a drag, not a dismissal.
useClickOutside(popupEl, () => emit('close'))

onMounted(() => document.addEventListener('keydown', handleKeydown))
onUnmounted(() => document.removeEventListener('keydown', handleKeydown))
</script>
