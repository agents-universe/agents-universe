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

const emit = defineEmits<{ select: [slug: string]; close: [] }>()

const COMMANDS = [
  { slug: 'plan', description: '制定任务计划' },
  { slug: 'review', description: '代码审查' },
  { slug: 'summarize', description: '总结对话' },
  { slug: 'test', description: '生成测试' },
  { slug: 'refactor', description: '重构代码' },
  { slug: 'explain', description: '解释代码' },
  { slug: 'fix', description: '修复问题' },
  { slug: 'generate', description: '生成代码' },
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

function handleClickOutside(e: MouseEvent) {
  if (popupEl.value && !popupEl.value.contains(e.target as Node)) emit('close')
}
onMounted(() => {
  document.addEventListener('mousedown', handleClickOutside)
  document.addEventListener('keydown', handleKeydown)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', handleClickOutside)
  document.removeEventListener('keydown', handleKeydown)
})
</script>
