<template>
  <div class="completeness-bar">
    <div class="completeness-bar-label">
      <span class="completeness-bar-category">
        <component :is="categoryIcon" :size="12" class="completeness-bar-icon" />
        {{ category }}
      </span>
      <span class="completeness-bar-pct" :class="fillClass">{{ Math.round(score) }}%</span>
    </div>
    <div class="completeness-bar-track">
      <div
        class="completeness-bar-fill"
        :style="{ width: `${Math.min(score, 100)}%` }"
        :class="fillClass"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { FileText, Code, Database, Settings, Globe, BookOpen } from 'lucide-vue-next'

const props = defineProps<{ category: string; score: number }>()

const fillClass = computed(() => {
  if (props.score >= 90) return 'fill-green'
  if (props.score >= 60) return 'fill-amber'
  return 'fill-red'
})

const categoryIcon = computed(() => {
  const name = props.category.toLowerCase()
  if (name.includes('api') || name.includes('code')) return Code
  if (name.includes('data') || name.includes('db')) return Database
  if (name.includes('config') || name.includes('setup')) return Settings
  if (name.includes('doc') || name.includes('guide')) return BookOpen
  if (name.includes('deploy') || name.includes('infra')) return Globe
  return FileText
})
</script>
