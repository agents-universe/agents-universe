<template>
  <div class="conv-tree-item-wrapper">
    <div class="conv-tree-item" :class="{ active: isActive, streaming: isStreaming }" @click="emit('select')">
      <button class="conv-tree-chevron" :class="{ expanded: isExpanded }" @click.stop="emit('toggle-expand')">
        <ChevronDown v-if="isExpanded" :size="12" />
        <ChevronRight v-else :size="12" />
      </button>
      <div class="conv-tree-info">
        <span class="conv-tree-title">{{ conversation.title || '未命名对话' }}</span>
        <div class="conv-tree-meta">
          <span>{{ conversation.message_count }} 条消息</span>
          <span v-if="isStreaming" class="conv-tree-live">运行中</span>
          <span>{{ relativeTime(conversation.updated_at ?? conversation.created_at) }}</span>
        </div>
      </div>
      <span v-if="isStreaming" class="conv-tree-pulse" />
      <button class="conv-tree-delete-btn" title="删除" @click.stop="emit('delete')">🗑</button>
    </div>

    <div v-if="isExpanded && tasks.length" class="conv-tree-tasks">
      <TaskTreeItem v-for="task in tasks" :key="task.id" :task="task" />
    </div>
    <div v-else-if="isExpanded" class="conv-tree-tasks-empty">暂无任务规划</div>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, ChevronRight } from 'lucide-vue-next'
import { relativeTime } from '@/utils/time'
import type { ConversationItem, AgentTask } from '@/types'
import TaskTreeItem from './TaskTreeItem.vue'

defineProps<{
  conversation: ConversationItem
  isActive: boolean
  isExpanded: boolean
  isStreaming?: boolean
  tasks: AgentTask[]
}>()

const emit = defineEmits<{
  select: []
  'toggle-expand': []
  delete: []
}>()
</script>

<style scoped>
.conv-tree-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4fc3f7;
  margin-left: auto;
  margin-right: 4px;
  flex-shrink: 0;
  animation: conv-pulse 1.4s ease-in-out infinite;
}

@keyframes conv-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.2); }
}

.conv-tree-item.streaming {
  background: rgba(79, 195, 247, 0.08);
}

.conv-tree-live {
  color: #4fc3f7;
  font-size: 11px;
  font-weight: 500;
}

.conv-tree-tasks-empty {
  margin-left: 1.4rem;
  padding: 0.15rem 0 0.35rem;
  font-size: 0.72rem;
  color: var(--color-text-muted);
}
</style>
