<template>
  <div class="conv-tree">
    <div class="conv-tree-header">
      <span class="conv-tree-header-title">{{ t('conversations.historyTitle') }}</span>
      <button class="conv-tree-new-btn" :title="t('conversations.newConversation')" @click="emit('new-conversation')">+</button>
    </div>

    <div v-if="error" class="conv-tree-error">{{ error }}</div>
    <div v-if="!conversations.length" class="conv-tree-empty">{{ t('conversations.empty') }}</div>
    <div v-else class="conv-tree-list">
      <ConversationTreeItem
        v-for="conv in conversations"
        :key="conv.conversation_id"
        :conversation="conv"
        :is-active="convStore.conversationId === conv.conversation_id"
        :is-expanded="expandedIds.has(conv.conversation_id)"
        :is-streaming="!!convStore.streamingIds[conv.conversation_id] || !!conv.is_running"
        :tasks="tasksFor(conv)"
        @select="selectConversation(conv)"
        @toggle-expand="toggleExpand(conv.conversation_id)"
        @delete="deleteConversation(conv.conversation_id)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useConversationStore } from '@/stores/conversation'
import { conversationsApi } from '@/api/conversations'
import { closeConnection } from '@/composables/useWebSocket'
import { mapDbTasks } from '@/stores/conversation'
import type { ConversationItem, AgentTask } from '@/types'
import ConversationTreeItem from './ConversationTreeItem.vue'

const props = defineProps<{ projectId?: string; agentSlug?: string }>()
const emit = defineEmits<{ 'new-conversation': [] }>()

const convStore = useConversationStore()
const { t } = useI18n()
const conversations = ref<ConversationItem[]>([])
const expandedIds = reactive(new Set<string>())
const taskCache = reactive(new Map<string, AgentTask[]>())

let pollTimer: ReturnType<typeof setInterval> | null = null
const error = ref<string | null>(null)

// Seq guard: a stale response for a previous project/agent must not
// overwrite the current list (the 5s poll + watch both fire overlapping loads).
let loadSeq = 0

async function load() {
  if (!props.projectId || !props.agentSlug) return
  const seq = ++loadSeq
  try {
    const list = await conversationsApi.list(props.projectId, props.agentSlug)
    if (seq !== loadSeq) return
    conversations.value = list
    error.value = null
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = e instanceof Error ? e.message : t('conversations.listFailed')
    console.error('Failed to load conversations', e)
  }
}

async function selectConversation(conv: ConversationItem) {
  if (convStore.conversationId === conv.conversation_id) return
  convStore.startConversation(conv.conversation_id)
  // Switch the token meter to this conversation's figures — the list item
  // already carries them and the previous conversation's runtime would
  // otherwise keep showing stale usage.
  convStore.setTokens(conv.tokens_used, conv.token_budget, conv.conversation_id)
  try {
    const [msgs, tasks, latestRun] = await Promise.all([
      conversationsApi.getMessages(conv.conversation_id),
      conversationsApi.getTasks(conv.conversation_id),
      conversationsApi.getLatestRun(conv.conversation_id),
    ])
    convStore.loadHistory(msgs, conv.conversation_id)
    convStore.setLastRun(latestRun, conv.conversation_id)
    if (convStore.conversationId === conv.conversation_id) {
      convStore.setTasks(tasks, conv.conversation_id)
      taskCache.set(conv.conversation_id, mapDbTasks(tasks))
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('conversations.loadFailed')
    console.error('Failed to load conversation', e)
  }
}

async function toggleExpand(id: string) {
  if (expandedIds.has(id)) {
    expandedIds.delete(id)
    return
  }
  expandedIds.add(id)
  await ensureTasks(id)
}

/** 懒加载会话任务到 taskCache；force 时无视已有缓存重新拉取。 */
async function ensureTasks(id: string, opts: { force?: boolean } = {}) {
  if (taskCache.has(id) && !opts.force) return
  try {
    const tasks = await conversationsApi.getTasks(id)
    taskCache.set(id, mapDbTasks(tasks))
    // Limit cache size
    if (taskCache.size > 50) {
      const firstKey = taskCache.keys().next().value
      if (firstKey) taskCache.delete(firstKey)
    }
  } catch { /* ignore */ }
}

/** 活动会话流式期间用 store 实时任务（WS 已更新状态），其余用 DB 快照缓存。 */
function tasksFor(conv: ConversationItem): AgentTask[] {
  if (conv.conversation_id === convStore.conversationId && convStore.tasks.length > 0) {
    return convStore.tasks
  }
  return taskCache.get(conv.conversation_id) ?? []
}

async function deleteConversation(id: string) {
  if (!confirm(t('conversations.deleteConfirm'))) return
  try {
    await conversationsApi.delete(id)
    conversations.value = conversations.value.filter((c) => c.conversation_id !== id)
    expandedIds.delete(id)
    taskCache.delete(id)
    // Tear down the client-side session for the deleted conversation: close
    // its WS connection (background connections stay open on switch — a
    // deleted conversation must not keep one alive) and drop its runtime
    // (messages, streaming state, localStorage draft).
    closeConnection(id)
    convStore.removeRuntime(id)
    if (convStore.conversationId === id) convStore.reset()
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('conversations.deleteFailed')
    console.error('Failed to delete conversation', e)
  }
}

watch([() => props.projectId, () => props.agentSlug], load, { immediate: true })

/** Reload the conversation list when a conversation starts streaming,
 *  so that newly-created conversations appear in the sidebar immediately
 *  instead of waiting for the next 5-second poll. */
const streamingKeyCount = computed(() => Object.keys(convStore.streamingIds).length)
watch(streamingKeyCount, (n, old) => {
  if (n > old) load()
})

// 当前会话默认展开并预取任务缓存；活动会话切换时展开新的那个。
watch(
  () => convStore.conversationId,
  (id) => {
    if (!id) return
    expandedIds.add(id)
    ensureTasks(id)
  },
  { immediate: true },
)

// 任意会话一轮流式结束后（不限于活动会话——后台会话的 turn 结束时
// store 运行时任务被清空，回落到 DB 快照），强制重拉该会话的任务缓存；
// 否则展开区永远显示上一次展开/上次轮询时的陈旧进度。streamingIds 的
// 停止是 delete 键（见 _updateStreamingFlag），Object.keys 每次返回新
// 数组引用，watch 无需 deep 也能拿到前后两组键做差集。
watch(
  () => Object.keys(convStore.streamingIds),
  (now, prev) => {
    const ended = prev?.filter((cid) => !now.includes(cid)) ?? []
    for (const cid of ended) {
      ensureTasks(cid, { force: true })
    }
  },
)

onMounted(() => {
  pollTimer = setInterval(load, 5_000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.conv-tree-error {
  font-size: 0.78rem;
  color: var(--color-danger, #e53e3e);
  padding: 0.5rem 0.75rem;
  text-align: center;
}
</style>
