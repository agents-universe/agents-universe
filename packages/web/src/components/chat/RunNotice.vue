<template>
  <div class="run-notice" :class="`run-notice-${props.run.status}`">
    <div class="run-notice-head">
      <span class="run-notice-label">{{ label }}</span>
      <button v-if="props.canRerun" class="run-notice-rerun" @click="emit('rerun')">
        {{ t('chatPanel.rerun') }}
      </button>
    </div>
    <!-- Partial text recovered from the interrupted run's snapshot — the
         message row was never persisted, so this is all that survived. -->
    <pre v-if="props.run.streaming_snapshot" class="run-notice-snapshot">{{ props.run.streaming_snapshot }}</pre>
    <!-- Failed runs: the reason the turn died (provider exception, empty
         output…). Empty text means the turn was marked failed with no
         captured detail. -->
    <pre v-else-if="props.run.status === 'failed' && props.run.error_message" class="run-notice-snapshot">{{ props.run.error_message }}</pre>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ConversationRun } from '@/types'

const props = defineProps<{
  run: ConversationRun
  canRerun: boolean
}>()

const emit = defineEmits<{ rerun: [] }>()

const { t } = useI18n()

const label = computed(() =>
  props.run.status === 'failed'
    ? t('chatPanel.runFailed')
    : t('chatPanel.runInterrupted'),
)
</script>

<style scoped>
.run-notice {
  margin: 8px 16px 0;
  padding: 8px 12px;
  border: 1px solid var(--color-border, #e2e8f0);
  border-radius: 8px;
  background: rgba(217, 119, 6, 0.06);
  font-size: 13px;
}
.run-notice-failed {
  background: rgba(220, 38, 38, 0.06);
}
.run-notice-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.run-notice-label {
  font-weight: 500;
  color: var(--color-text, inherit);
}
.run-notice-rerun {
  flex-shrink: 0;
  padding: 2px 10px;
  border: 1px solid currentColor;
  border-radius: 6px;
  background: transparent;
  color: #b45309;
  cursor: pointer;
  font-size: 12px;
}
.run-notice-failed .run-notice-rerun {
  color: #b91c1c;
}
.run-notice-rerun:hover {
  background: rgba(0, 0, 0, 0.05);
}
.run-notice-snapshot {
  margin: 6px 0 0;
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  color: var(--color-text-secondary, #64748b);
}
</style>
