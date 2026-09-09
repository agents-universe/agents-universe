<template>
  <div class="conv-tree-item-wrapper">
    <div class="conv-tree-item" :class="{ active: isActive, streaming: isStreaming }" @click="emit('select')">
      <button class="conv-tree-chevron" :class="{ expanded: isExpanded }" @click.stop="emit('toggle-expand')">
        <ChevronDown v-if="isExpanded" :size="12" />
        <ChevronRight v-else :size="12" />
      </button>
      <div class="conv-tree-info">
        <input
          v-if="editing"
          ref="inputEl"
          v-model="draft"
          class="conv-tree-rename-input"
          :maxlength="255"
          @click.stop
          @keydown.enter.prevent="commitRename"
          @keydown.esc.prevent="cancelRename"
          @blur="commitRename"
        />
        <span v-else class="conv-tree-title">{{ conversation.title || t('conversations.untitled') }}</span>
        <div class="conv-tree-meta">
          <span v-if="agentLabel" class="conv-tree-agent-badge">{{ agentLabel }}</span>
          <span>{{ t('conversations.messageCount', { count: conversation.message_count }) }}</span>
          <span v-if="isStreaming" class="conv-tree-live">{{ t('conversations.isStreaming') }}</span>
          <span v-else-if="isInterrupted" class="conv-tree-warn">{{ interruptedLabel }}</span>
          <span>{{ relativeTime(conversation.updated_at ?? conversation.created_at) }}</span>
        </div>
      </div>
      <span v-if="isStreaming" class="conv-tree-pulse" />
      <button class="conv-tree-rename-btn" :title="t('conversations.renameTitle')" @click.stop="startRename">
        <Pencil :size="12" />
      </button>
      <button class="conv-tree-delete-btn" :title="t('conversations.deleteTitle')" @click.stop="emit('delete')">🗑</button>
    </div>

    <div v-if="isExpanded && tasks.length" class="conv-tree-tasks">
      <TaskTreeItem v-for="task in tasks" :key="task.id" :task="task" />
    </div>
    <div v-else-if="isExpanded" class="conv-tree-tasks-empty">{{ t('conversations.noTaskPlan') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ChevronDown, ChevronRight, Pencil } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import { relativeTime } from '@/utils/time'
import type { ConversationItem, AgentTask } from '@/types'
import TaskTreeItem from './TaskTreeItem.vue'

const { t } = useI18n()

const props = defineProps<{
  conversation: ConversationItem
  isActive: boolean
  isExpanded: boolean
  isStreaming?: boolean
  tasks: AgentTask[]
  /** Owning agent's display name — only passed while searching across agents. */
  agentLabel?: string
}>()

// A run that ended in a terminal failure (process restart / agent crash) —
// the live dot only covers in-process streaming, so this badge is the only
// signal for runs that died while the panel was away.
const isInterrupted = computed(() =>
  props.conversation.last_run_status === 'interrupted' || props.conversation.last_run_status === 'failed',
)

const interruptedLabel = computed(() =>
  props.conversation.last_run_status === 'failed'
    ? t('conversations.failed')
    : t('conversations.interrupted'),
)

const emit = defineEmits<{
  select: []
  'toggle-expand': []
  delete: []
  rename: [title: string]
}>()

// Inline title editor. `finished` is the once-only latch: Enter both commits
// and drops focus, and the blur handler would otherwise commit a second time.
const editing = ref(false)
const draft = ref('')
const finished = ref(false)
const inputEl = ref<HTMLInputElement | null>(null)

function startRename() {
  draft.value = props.conversation.title ?? ''
  finished.value = false
  editing.value = true
  nextTick(() => {
    inputEl.value?.focus()
    inputEl.value?.select()
  })
}

function commitRename() {
  if (finished.value) return
  finished.value = true
  editing.value = false
  const next = draft.value.trim()
  if (!next || next === (props.conversation.title ?? '')) return
  emit('rename', next)
}

function cancelRename() {
  finished.value = true
  editing.value = false
  draft.value = ''
}
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

.conv-tree-warn {
  color: #d97706;
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
