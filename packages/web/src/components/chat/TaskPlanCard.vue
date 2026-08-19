<template>
  <div class="task-plan-card">
    <div class="task-plan-header" @click="collapsed = !collapsed">
      <span class="task-plan-icon">{{ collapsed ? '▶' : '▼' }}</span>
      <span class="task-plan-title">{{ t('taskPlan.title') }}</span>
      <span class="task-plan-progress">
        {{ completedCount }}/{{ tasks.length }}
        <span v-if="runningCount > 0" class="task-plan-running">· {{ t('taskPlan.runningCount', { count: runningCount }) }}</span>
      </span>
    </div>
    <div v-if="!collapsed" class="task-plan-body">
      <div
        v-for="task in tasks"
        :key="task.id"
        class="task-plan-item"
        :class="'task-plan-' + task.status"
      >
        <div class="task-plan-item-header">
          <span class="task-status-icon" :class="'status-' + task.status" />
          <span class="task-plan-item-title">{{ task.title }}</span>
          <!-- 实际执行模型优先（task_started），缺失时回退预估复杂度 -->
          <span v-if="task.modelName || task.modelTier" class="task-plan-tier">{{ task.modelName ?? task.modelTier }}</span>
        </div>
        <!-- 依赖提示：pending 任务显示等待哪些任务 -->
        <div v-if="task.status === 'pending' && waitingFor(task).length" class="task-plan-waiting">
          ⏳ {{ t('taskPlan.waitingFor') }}: {{ waitingFor(task) }}
        </div>
        <div v-if="task.status === 'failed' && task.error" class="task-plan-error">{{ task.error }}</div>
        <div v-if="task.status === 'completed' && task.summary" class="task-plan-summary">{{ task.summary }}</div>
        <!-- 流式文本：running 任务的实时输出 -->
        <div v-if="task.status === 'running' && taskStreaming(task.id)" class="task-plan-streaming">
          {{ taskStreaming(task.id) }}
        </div>
        <!-- Tool calls belonging to this task -->
        <div v-if="taskToolCalls(task.id).length" class="task-plan-tools">
          <ToolCallCard
            v-for="tc in taskToolCalls(task.id)"
            :key="tc.callId"
            :call="tc"
            :default-expanded="tc.status === 'running' || tc.status === 'preparing'"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useConversationStore } from '@/stores/conversation'
import type { AgentTask, ToolCallRecord } from '@/types'
import ToolCallCard from './ToolCallCard.vue'

const { t } = useI18n()
const props = defineProps<{
  tasks: AgentTask[]
  toolCalls: ToolCallRecord[]
}>()

const convStore = useConversationStore()
const collapsed = ref(false)

const completedCount = computed(() =>
  props.tasks.filter((t) => t.status === 'completed').length,
)
const runningCount = computed(() =>
  props.tasks.filter((t) => t.status === 'running').length,
)

function taskToolCalls(taskId: string): ToolCallRecord[] {
  return props.toolCalls.filter((tc) => tc.taskId === taskId)
}

function taskStreaming(taskId: string): string {
  return convStore.taskStreamingText(taskId)
}

/** 返回该任务等待的依赖任务标题列表（逗号分隔） */
function waitingFor(task: AgentTask): string {
  if (!task.dependsOn?.length) return ''
  const titles = task.dependsOn
    .map(depId => props.tasks.find(t => t.id === depId)?.title)
    .filter(Boolean)
  return titles.join(', ')
}
</script>
