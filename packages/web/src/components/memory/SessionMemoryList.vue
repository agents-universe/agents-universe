<template>
  <div class="memory-section">
    <div class="section-label">
      {{ t('memoryPanels.sessionNotes') }}
      <button class="icon-btn" :title="t('memoryPanels.clearTitle')" @click="memoryStore.clearSessionNotes()"><X :size="14" /></button>
    </div>
    <div v-if="!memoryStore.sessionNotes.length" class="memory-empty empty-state">
      <span class="empty-state-icon"><StickyNote :size="16" /></span>
      <span>{{ t('memoryPanels.noNotes') }}</span>
    </div>
    <div v-for="(note, index) in memoryStore.sessionNotes" :key="`${note.timestamp}-${index}`" class="session-note">
      <span class="session-note-time">{{ relativeTime(new Date(note.timestamp).toISOString()) }}</span>
      <p class="session-note-text">{{ note.note }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { StickyNote, X } from 'lucide-vue-next'
import { useMemoryStore } from '@/stores/memory'
import { relativeTime } from '@/utils/time'

const { t } = useI18n()
const memoryStore = useMemoryStore()
</script>
