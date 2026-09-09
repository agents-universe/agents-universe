<template>
  <div class="conv-tree">
    <div class="conv-tree-header">
      <span class="conv-tree-header-title">{{ t('conversations.historyTitle') }}</span>
      <button class="conv-tree-new-btn" :title="t('conversations.newConversation')" @click="onNewConversation">+</button>
    </div>

    <div class="conv-tree-search-wrapper">
      <Search :size="13" class="conv-tree-search-icon" />
      <input
        v-model="searchInput"
        class="conv-tree-search"
        :placeholder="t('conversations.searchPlaceholder')"
        maxlength="100"
        @keydown.esc="clearSearch"
      />
      <button
        v-if="searchInput"
        class="conv-tree-search-clear"
        :title="t('conversations.clearSearch')"
        @click="clearSearch"
      >
        <X :size="12" />
      </button>
    </div>

    <div v-if="error" class="conv-tree-error">{{ error }}</div>
    <div v-if="!conversations.length" class="conv-tree-empty">
      {{ isSearching ? t('conversations.noMatches') : t('conversations.empty') }}
    </div>
    <div v-else class="conv-tree-list">
      <ConversationTreeItem
        v-for="conv in conversations"
        :key="conv.conversation_id"
        :conversation="conv"
        :is-active="convStore.conversationId === conv.conversation_id"
        :is-expanded="expandedIds.has(conv.conversation_id)"
        :is-streaming="!!convStore.streamingIds[conv.conversation_id] || !!conv.is_running"
        :tasks="tasksFor(conv)"
        :agent-label="agentLabelFor(conv)"
        @select="selectConversation(conv)"
        @toggle-expand="toggleExpand(conv.conversation_id)"
        @delete="deleteConversation(conv.conversation_id)"
        @rename="(title) => renameConversation(conv.conversation_id, title)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { Search, X } from 'lucide-vue-next'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import { conversationsApi } from '@/api/conversations'
import { closeConnection } from '@/composables/useWebSocket'
import { invalidateLatestConversation } from '@/composables/useLatestConversation'
import { mapDbTasks } from '@/stores/conversation'
import type { ConversationItem, AgentTask } from '@/types'
import ConversationTreeItem from './ConversationTreeItem.vue'

const props = defineProps<{ projectId?: string; agentSlug?: string }>()
const emit = defineEmits<{ 'new-conversation': [] }>()

const convStore = useConversationStore()
const agentStore = useAgentStore()
const { t } = useI18n()
const conversations = ref<ConversationItem[]>([])
const expandedIds = reactive(new Set<string>())
const taskCache = reactive(new Map<string, AgentTask[]>())

let pollTimer: ReturnType<typeof setInterval> | null = null
const error = ref<string | null>(null)

// Seq guard: a stale response for a previous project/agent must not
// overwrite the current list (the 5s poll + watch both fire overlapping loads).
let loadSeq = 0

// Keyword search. `searchInput` is what the user types; `activeQuery` is the
// debounced term the list is actually filtered by. Search is project-wide
// (no agent_slug) so a conversation started under another agent is findable.
const searchInput = ref('')
const activeQuery = ref('')
const isSearching = computed(() => activeQuery.value.length > 0)
const searchPending = computed(() => isSearching.value || !!searchInput.value.trim())
let searchTimer: ReturnType<typeof setTimeout> | null = null

watch(searchInput, (value) => {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  const next = value.trim()
  if (!next) {
    // Clearing is immediate — waiting out the debounce leaves a stale result
    // list on screen after the box is empty.
    activeQuery.value = ''
    return
  }
  searchTimer = setTimeout(() => {
    searchTimer = null
    if (activeQuery.value !== next) activeQuery.value = next
  }, 300)
})

function clearSearch() {
  searchInput.value = ''
  activeQuery.value = ''
}

function onNewConversation() {
  // Starting a new chat leaves the search context — otherwise the panel keeps
  // showing results for a query the user has moved on from.
  clearSearch()
  emit('new-conversation')
}

async function load() {
  // Bumped before any early return: clearing the search with no agent selected
  // must still invalidate an in-flight response for the previous state.
  const seq = ++loadSeq
  const query = activeQuery.value
  if (!props.projectId || (!query && !props.agentSlug)) {
    conversations.value = []
    return
  }
  try {
    const list = query
      ? await conversationsApi.list(props.projectId, undefined, query)
      : await conversationsApi.list(props.projectId, props.agentSlug)
    if (seq !== loadSeq) return
    conversations.value = list
    error.value = null
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = e instanceof Error ? e.message : t('conversations.listFailed')
    console.error('Failed to load conversations', e)
  }
}

/** Owning agent's display name, only while searching and only for another
 *  agent's conversation (the current agent's rows need no badge). */
function agentLabelFor(conv: ConversationItem): string | undefined {
  if (!isSearching.value || !conv.agent_slug) return undefined
  if (conv.agent_slug === agentStore.currentAgent?.slug) return undefined
  return agentStore.agents.find((a) => a.slug === conv.agent_slug)?.label ?? conv.agent_slug
}

async function selectConversation(conv: ConversationItem) {
  if (convStore.conversationId === conv.conversation_id) return
  const targetSlug = conv.agent_slug
  if (targetSlug && targetSlug !== agentStore.currentAgent?.slug) {
    const target = agentStore.agents.find((a) => a.slug === targetSlug)
    if (target) {
      agentStore.setCurrentAgent(target)
      // ChatPage's watch(agentSlug) resets the store and fires its own
      // "restore the latest conversation" request. Let it run, then discard
      // that response — otherwise the agent's latest conversation replaces
      // the one the user just clicked.
      await nextTick()
      invalidateLatestConversation()
    } else {
      // Agent row gone (or not loaded): open the conversation anyway — its
      // messages are keyed by conversation_id. Only the next turn's agent
      // would fall back to the currently selected one.
      console.warn('Conversation belongs to an agent not in the current list', targetSlug)
    }
  }
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

async function renameConversation(id: string, title: string) {
  try {
    const updated = await conversationsApi.rename(id, title)
    const row = conversations.value.find((c) => c.conversation_id === id)
    if (row) row.title = updated.title
    error.value = null
  } catch (e) {
    // Leave the old title in place — the list is the source of truth.
    error.value = e instanceof Error ? e.message : t('conversations.renameFailed')
    console.error('Failed to rename conversation', e)
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

watch([() => props.projectId, () => props.agentSlug], ([pid], [oldPid]) => {
  // A search belongs to the project it was typed in.
  if (pid !== oldPid) clearSearch()
  load()
}, { immediate: true })

// The debounced term drives the request; clearing it reloads the
// agent-scoped list.
watch(activeQuery, load)

/** Reload the conversation list when a conversation starts streaming,
 *  so that newly-created conversations appear in the sidebar immediately
 *  instead of waiting for the next 5-second poll. */
const streamingKeyCount = computed(() => Object.keys(convStore.streamingIds).length)
watch(streamingKeyCount, (n, old) => {
  if (n > old && !searchPending.value) load()
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
  pollTimer = setInterval(() => {
    // A keyword search scans message bodies server-side — don't re-run it
    // every 5s. searchPending (not just activeQuery) also covers the debounce
    // window, where a poll would paint the unfiltered list over the results.
    if (searchPending.value) return
    load()
  }, 5_000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (searchTimer) clearTimeout(searchTimer)
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
