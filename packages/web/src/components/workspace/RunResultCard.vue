<template>
  <div class="run-result-card" :class="verdict">
    <div class="run-result-head">
      <span class="run-result-badge" :class="verdict">{{ verdictLabel }}</span>
      <span v-if="result" class="run-result-counts">
        <span class="run-count passed">{{ result.counts.passed }} {{ t('workspace.resultPassed') }}</span>
        <span class="run-count" :class="{ bad: result.counts.failed > 0 }">
          {{ result.counts.failed }} {{ t('workspace.resultFailed') }}
        </span>
        <span v-if="result.counts.flaky" class="run-count warn">
          {{ result.counts.flaky }} {{ t('workspace.resultFlaky') }}
        </span>
        <span v-if="result.counts.skipped" class="run-count muted">
          {{ result.counts.skipped }} {{ t('workspace.resultSkipped') }}
        </span>
      </span>
      <span v-if="durationText" class="run-result-meta">
        {{ t('workspace.resultDuration') }} {{ durationText }}
      </span>
      <span v-if="exitCode !== null && exitCode !== undefined" class="run-result-meta">
        {{ t('workspace.resultExitCode') }} {{ exitCode }}
      </span>
    </div>

    <div v-if="result?.partial" class="run-result-hint">{{ t('workspace.resultPartial') }}</div>

    <div v-if="failedTests.length" class="run-result-failures">
      <div class="run-result-section">{{ t('workspace.resultFailedTests') }}</div>
      <div v-for="(item, i) in failedTests" :key="i" class="run-failure">
        <div class="run-failure-title">{{ item.title || item.file || '—' }}</div>
        <div v-if="item.file" class="run-failure-location">
          {{ item.file }}<template v-if="item.line">:{{ item.line }}</template>
        </div>
        <div v-if="firstLine(item.error)" class="run-failure-error">{{ firstLine(item.error) }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RunResult } from '@/api/scripts'

const props = defineProps<{
  /** Run row status - the fallback verdict when no parsed result exists. */
  status: string
  result: RunResult | null
  exitCode: number | null
  startedAt?: string | null
  completedAt?: string | null
}>()

const { t } = useI18n()

const verdict = computed(() => {
  const status = props.result?.status || props.status
  if (status === 'passed' || status === 'completed') return 'passed'
  if (status === 'failed' || status === 'timed_out') return 'failed'
  if (status === 'running' || status === 'pending') return 'running'
  return 'unknown'
})

const verdictLabel = computed(() => {
  const status = props.result?.status || props.status
  if (status === 'timed_out') return t('workspace.resultTimedOut')
  if (status === 'passed' || status === 'completed') return t('workspace.resultPassed')
  if (status === 'failed') return t('workspace.resultFailed')
  if (status === 'running' || status === 'pending') return t('workspace.resultRunning')
  return t('workspace.resultUnknown')
})

const failedTests = computed(() => props.result?.failed_tests ?? [])

const durationText = computed(() => {
  const reported = props.result?.duration_ms
  if (reported) return formatDuration(reported)
  if (props.startedAt && props.completedAt) {
    const span = new Date(props.completedAt).getTime() - new Date(props.startedAt).getTime()
    if (span > 0) return formatDuration(span)
  }
  return ''
})

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${Math.round(seconds % 60)}s`
}

function firstLine(text: string): string {
  return (text || '').split('\n')[0].trim()
}
</script>
