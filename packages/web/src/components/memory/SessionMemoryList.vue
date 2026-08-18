<template>
  <div class="memory-section">
    <div class="section-label">
      会话笔记
      <button class="icon-btn" title="清空" @click="memoryStore.clearSessionNotes()">✕</button>
    </div>
    <div v-if="!memoryStore.sessionNotes.length" class="memory-empty">暂无笔记</div>
    <div v-for="(note, index) in memoryStore.sessionNotes" :key="`${note.timestamp}-${index}`" class="session-note">
      <span class="session-note-time">{{ relativeTime(new Date(note.timestamp).toISOString()) }}</span>
      <p class="session-note-text">{{ note.note }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useMemoryStore } from '@/stores/memory'
import { relativeTime } from '@/utils/time'

const memoryStore = useMemoryStore()
</script>
