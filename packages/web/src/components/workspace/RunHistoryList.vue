<template>
  <div v-if="loading || runs.length" class="run-history">
    <div class="run-result-section">{{ t('workspace.historyTitle') }}</div>
    <div v-if="loading && !runs.length" class="run-history-empty">
      {{ t('common.loading') }}
    </div>
    <button
      v-for="run in runs"
      :key="run.run_id"
      class="run-history-row"
      :class="{ active: run.run_id === activeRunId }"
      @click="emit('select', run.run_id)"
    >
      <span class="run-history-dot" :class="verdictOf(run)" />
      <span class="run-history-time">{{ formatTime(run.created_at) }}</span>
      <span class="run-history-counts">{{ countsOf(run) }}</span>
      <span v-if="durationOf(run)" class="run-history-duration">{{ durationOf(run) }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { RunRow } from '@/api/scripts'

defineProps<{
  runs: RunRow[]
  activeRunId: string | null
  loading: boolean
}>()

const emit = defineEmits<{ select: [runId: string] }>()

const { t } = useI18n()

function verdictOf(run: RunRow): string {
  const status = run.summary?.status || run.status
  if (status === 'passed' || status === 'completed') return 'passed'
  if (status === 'failed' || status === 'timed_out') return 'failed'
  if (status === 'running' || status === 'pending') return 'running'
  return 'unknown'
}

function countsOf(run: RunRow): string {
  const counts = run.summary?.counts
  if (!counts || !counts.total) return t('workspace.resultNoCounts')
  const parts = [`${counts.passed} ${t('workspace.resultPassed')}`]
  if (counts.failed) parts.push(`${counts.failed} ${t('workspace.resultFailed')}`)
  if (counts.flaky) parts.push(`${counts.flaky} ${t('workspace.resultFlaky')}`)
  if (counts.skipped) parts.push(`${counts.skipped} ${t('workspace.resultSkipped')}`)
  return parts.join(' · ')
}

function durationOf(run: RunRow): string {
  const ms = run.summary?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const sameDay = date.toDateString() === new Date().toDateString()
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return sameDay ? clock : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`
}
</script>
