<template>
  <div class="memory-section">
    <div class="section-label">
      {{ t('memoryPanels.sessionNotes') }}
      <button class="icon-btn" :title="t('memoryPanels.clearTitle')" @click="memoryStore.clearSessionNotes()">✕</button>
    </div>
    <div v-if="!memoryStore.sessionNotes.length" class="memory-empty">{{ t('memoryPanels.noNotes') }}</div>
    <div v-for="(note, index) in memoryStore.sessionNotes" :key="`${note.timestamp}-${index}`" class="session-note">
      <span class="session-note-time">{{ relativeTime(new Date(note.timestamp).toISOString()) }}</span>
      <p class="session-note-text">{{ note.note }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useMemoryStore } from '@/stores/memory'
import { relativeTime } from '@/utils/time'

const { t } = useI18n()
const memoryStore = useMemoryStore()
</script>
