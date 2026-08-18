<template>
  <div class="memory-section">
    <div class="section-label">情节记忆</div>
    <div v-if="!memoryStore.episodes.length" class="memory-empty">暂无情节记录</div>
    <div
      v-for="ep in memoryStore.episodes"
      :key="ep.episode_id"
      class="episode-item"
    >
      <div class="episode-header" @click="toggleExpand(ep.episode_id)">
        <span class="episode-summary">{{ ep.summary }}</span>
        <span class="episode-time">{{ ep.created_at ? relativeTime(ep.created_at) : '' }}</span>
        <ChevronDown v-if="!expanded.has(ep.episode_id)" :size="12" />
        <ChevronUp v-else :size="12" />
      </div>
      <div v-if="expanded.has(ep.episode_id)" class="episode-detail">
        <div v-if="ep.key_findings.length">
          <div class="episode-section-label">关键发现</div>
          <ul class="episode-list">
            <li v-for="f in ep.key_findings" :key="f">{{ f }}</li>
          </ul>
        </div>
        <div v-if="ep.open_questions.length">
          <div class="episode-section-label">待解问题</div>
          <ul class="episode-list">
            <li v-for="q in ep.open_questions" :key="q">{{ q }}</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive } from 'vue'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import { useMemoryStore } from '@/stores/memory'
import { relativeTime } from '@/utils/time'

const memoryStore = useMemoryStore()
const expanded = reactive(new Set<string>())

function toggleExpand(id: string) {
  expanded.has(id) ? expanded.delete(id) : expanded.add(id)
}
</script>
