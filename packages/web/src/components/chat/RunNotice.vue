<template>
  <div class="run-notice" :class="`run-notice-${props.run.status}`">
    <div class="run-notice-head">
      <span class="run-notice-label">{{ label }}</span>
    </div>
    <!-- Partial text recovered from the interrupted run's snapshot - the
         message row was never persisted, so this is all that survived.
         Normally the startup sweep has already materialized the partial into
         the history above; this block is the fallback for a snapshot that
         has not been recovered yet. -->
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
}>()

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
