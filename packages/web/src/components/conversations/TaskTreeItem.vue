<template>
  <div class="task-tree-item" :class="'task-' + task.status">
    <div class="task-header">
      <span class="task-status-icon" :class="'status-' + task.status" />
      <span class="task-title" :title="task.title">{{ task.title }}</span>
      <span v-if="task.modelTier" class="task-tier-badge">{{ task.modelTier }}</span>
      <span v-if="progressText" class="task-progress">{{ progressText }}</span>
    </div>
    <div v-if="task.status === 'running' && task.currentStep" class="task-step-info">
      <span class="current-step" :title="task.currentStep">{{ task.currentStep }}</span>
      <span v-if="task.nextStep" class="step-arrow">→</span>
      <span v-if="task.nextStep" class="next-step" :title="task.nextStep">{{ task.nextStep }}</span>
    </div>
    <div v-if="showProgressBar" class="task-progress-track">
      <div class="task-progress-fill" :style="{ width: progressBarWidth }" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { AgentTask } from '@/types'

const props = defineProps<{ task: AgentTask }>()

const progressText = computed(() => {
  if (props.task.progressTotal && props.task.progressTotal > 0) {
    return `${props.task.progressCompleted ?? 0}/${props.task.progressTotal}`
  }
  return null
})

const showProgressBar = computed(
  () => props.task.status === 'running' && (props.task.progressTotal ?? 0) > 0,
)

const progressBarWidth = computed(() => {
  const total = props.task.progressTotal ?? 0
  const done = props.task.progressCompleted ?? 0
  return `${Math.max(0, Math.min(100, Math.round((done / total) * 100)))}%`
})
</script>

<style scoped>
.task-tree-item {
  padding: 0.15rem 0;
}

.task-tree-item.task-running {
  background: rgba(79, 195, 247, 0.06);
  border-radius: 4px;
}

.task-header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  min-width: 0;
}

.task-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.78rem;
  color: var(--text-primary);
}

.task-running .task-title {
  color: #4fc3f7;
}

.task-completed .task-title {
  color: var(--text-secondary);
}

.task-failed .task-title {
  color: var(--color-error);
}

.task-skipped .task-title {
  color: var(--text-muted);
}

.task-tier-badge {
  flex-shrink: 0;
  font-size: 0.6rem;
  line-height: 1;
  padding: 0.18rem 0.3rem;
  border-radius: 4px;
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
  background: var(--bg-tertiary);
  text-transform: uppercase;
}

.task-progress {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 0.68rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.task-running .task-progress {
  color: #4fc3f7;
}

/* 执行中任务的当前步 → 下一步 */
.task-step-info {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin: 0.1rem 0 0.05rem 1.2rem;
  font-size: 0.72rem;
  min-width: 0;
}

.current-step {
  color: #4fc3f7;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-arrow {
  color: var(--text-muted);
  flex-shrink: 0;
}

.next-step {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 执行中任务的迷你进度条 */
.task-progress-track {
  margin: 0.2rem 0 0.1rem 1.2rem;
  height: 3px;
  border-radius: 2px;
  background: var(--bg-elevated);
  overflow: hidden;
}

.task-progress-fill {
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, #4fc3f7, #6b9fff);
  transition: width 0.3s ease;
}
</style>
