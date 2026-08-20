<template>
  <div class="chat-page">
    <ChatPanel
      v-if="conversationId"
      :conversation-id="conversationId"
      :agent-slug="agentSlug ?? undefined"
      @new-conversation="handleNewConversation"
    />
    <div v-else class="chat-empty">
      <div v-if="loading" class="thinking-indicator">
        <span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" />
      </div>
      <template v-else>
        <p v-if="loadError" class="chat-empty-error">{{ loadError }}</p>
        <p class="chat-empty-hint">{{ t('chatPage.newConversationHint') }}</p>
        <!-- without !agentSlug the button stayed clickable with no
        agent selected and created an agent-less conversation that the
        backend could never run. (loading already hides the button entirely.) -->
        <button class="btn-primary" :disabled="!agentSlug" @click="startChat">{{ t('chatPage.startChat') }}</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.chat-empty-error {
  color: var(--color-error, #f38ba8);
  font-size: 13px;
  margin-bottom: 12px;
}
</style>

<script lang="ts">
// Module-level monotonic seq shared with AppLayout's "+ 新建对话" button:
// an in-flight getLatest response must not resurrect the OLD latest
// conversation over a freshly reset empty state .
let latestSeq = 0
export function invalidateLatestConversation() {
  latestSeq++
}
// "+ 新建对话" consumed: the empty state is the USER'S intent, so the
// agentSlug watch (agent re-resolution after an async AgentSwitcher fetch)
// must not auto-restore the old latest conversation over it. When the agent
// IS resolved, the intent auto-starts the conversation instead of waiting
// for the empty-state button.
let pendingNew = false
// The project that requested the new chat. An agent re-resolution from a
// DIFFERENT project switch (the projectId watch drops the flag too, but it
// fires after this watch) must not auto-start a conversation in the new
// project — only a same-project resolution may.
let pendingNewPid: string | null = null
</script>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import { useProjectStore } from '@/stores/project'
import { conversationsApi } from '@/api/conversations'
import { ApiError } from '@/api/client'
import { buildOnboardingKickoff } from '@/utils/onboarding'
import { closeAllConnections } from '@/composables/useWebSocket'
import ChatPanel from '@/components/chat/ChatPanel.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const convStore = useConversationStore()
const agentStore = useAgentStore()
const projectStore = useProjectStore()

const loading = ref(false)
// A dead project URL (typed by hand, deleted project, stale tab) makes both
// getLatest and create 404; without this the page shows a clickable button
// that does nothing, silently.
const loadError = ref<string | null>(null)
const pendingOnboarding = ref(false)
// The project the pending onboarding kickoff belongs to. The ?onboarding=1
// query watcher and the projectId/agentSlug watchers fire in the SAME flush
// for a new-project navigation (and the agent auto-resolves right after), so
// clearing the flag unconditionally would swallow the kickoff the query
// watcher just set. Only a switch to a
// DIFFERENT project invalidates it.
let onboardingPid: string | null = null
const projectId = computed(() => route.params.projectId as string)
const agentSlug = computed(() => agentStore.currentAgent?.slug ?? null)
const conversationId = computed(() => convStore.conversationId)

function invalidateOnboardingIfProjectChanged() {
  if (onboardingPid !== null && onboardingPid !== projectId.value) {
    pendingOnboarding.value = false
    onboardingPid = null
  }
}

async function loadLatestConversation() {
  if (convStore.conversationId) return
  if (!projectId.value || !agentSlug.value) return

  const seq = ++latestSeq
  const pid = projectId.value
  const slug = agentSlug.value
  loading.value = true
  loadError.value = null
  try {
    const latest = await conversationsApi.getLatest(pid, slug)
    if (seq !== latestSeq) return
    if (latest?.conversation_id) {
      convStore.startConversation(latest.conversation_id)
      const [msgs, tasks] = await Promise.all([
        conversationsApi.getMessages(latest.conversation_id),
        conversationsApi.getTasks(latest.conversation_id),
      ])
      if (seq !== latestSeq) return
      convStore.loadHistory(msgs, latest.conversation_id)
      // Restore the token meter for a resumed (idle) conversation — the WS
      // only reports token figures while a run is active, so without this
      // a reload shows 0 / 128,000 until the next run.
      if (latest.tokens_used != null && latest.token_budget != null) {
        convStore.setTokens(latest.tokens_used, latest.token_budget, latest.conversation_id)
      }
      if (convStore.conversationId === latest.conversation_id) convStore.setTasks(tasks, latest.conversation_id)
    }
  } catch (e) {
    // A stale response (superseded by a project/agent switch) must not paint
    // its failure onto the now-active empty state.
    if (seq !== latestSeq) return
    loadError.value = e instanceof ApiError && e.status === 404
      ? t('chatPage.projectNotFound')
      : t('chatPage.loadFailed')
    console.error('Failed to load latest conversation', e)
  } finally {
    if (seq === latestSeq) loading.value = false
  }
}

onMounted(() => {
  if (route.query.onboarding === '1') {
    pendingOnboarding.value = true
    onboardingPid = projectId.value
    router.replace({ query: {} })
  }
  if (route.query.new === '1') {
    // "+ 新建对话" from a non-chat page: show the fresh empty state instead
    // of auto-restoring the old latest conversation .
    pendingNew = true
    pendingNewPid = projectId.value
    router.replace({ query: {} })
    maybeAutoStartChat()
    return
  }
  loadLatestConversation()
})

// Application-internal onboarding (?onboarding=1 from ProjectPickerDialog /
// CreateProjectDialog) can arrive while this page is already mounted — the
// onMounted check above misses it, so watch the query too.
watch(
  () => route.query.onboarding,
  (val) => {
    if (val === '1') {
      pendingOnboarding.value = true
      onboardingPid = projectId.value
      router.replace({ query: {} })
    }
  },
)

// "+ 新建对话" while the chat page is already mounted pushes ?new=1 onto the
// SAME route (AppLayout.handleNewConversation), so the component does not
// remount and the onMounted check above misses it. Left uncleaned, the marker
// would make the next F5 reload skip restoring the latest conversation.
watch(
  () => route.query.new,
  (val) => {
    if (val === '1') {
      pendingNew = true
      pendingNewPid = projectId.value
      router.replace({ query: {} })
      maybeAutoStartChat()
    }
  },
)

watch(agentSlug, (slug) => {
  if (slug) {
    // A switch to a different project invalidates an in-flight onboarding
    // kickoff: consuming it later would start the OLD project's onboarding
    // message in the NEW project (the category is read at consume time).
    // Same-project agent re-resolution (auto parse after project creation)
    // must NOT clear it .
    invalidateOnboardingIfProjectChanged()
    closeAllConnections()
    // Read before the reset: an in-flight auto-start create may have landed
    // already (rapid "+" clicks), and convStore.reset() below nulls it.
    const hadConversation = !!convStore.conversationId
    convStore.reset()
    // The user asked for a fresh conversation (?new=1); the re-resolved
    // agent must not auto-restore the old latest conversation over it —
    // instead it starts the new conversation immediately.
    if (pendingNew) {
      const pid = pendingNewPid
      pendingNew = false
      pendingNewPid = null
      // An active conversation means the auto-start's create already landed;
      // behave like a normal agent switch instead of orphaning it with a
      // second create.
      if (hadConversation) {
        loadLatestConversation()
        return
      }
      if (pid === projectId.value) startChat()
      return
    }
    loadLatestConversation()
  }
})

// Global agents (敏捷三智能体 etc.) span projects — switching project with
// the same agent leaves agentSlug unchanged, so the agentSlug watch never
// fires: the new project's latest conversation would never load and the old
// project's WebSocket stays open. Reset + reload symmetric with the watch
// above. (projectStore.reset also resets convStore, so reset() here is a
// cheap idempotent backstop for non-reset switch paths.)
watch(projectId, () => {
  // See the agentSlug watch: only a different-project switch invalidates
  // the onboarding kickoff .
  invalidateOnboardingIfProjectChanged()
  // A pending "new chat" flag belongs to the old project — if it survives the
  // switch, the next watch(agentSlug) fires on a new project+agent and skips
  // loading the latest conversation (consuming the stale flag). Drop it so
  // the fresh project loads normally.
  pendingNew = false
  pendingNewPid = null
  closeAllConnections()
  convStore.reset()
  loadLatestConversation()
})

function handleNewConversation() {
  closeAllConnections()
  convStore.reset()
  invalidateLatestConversation()
  pendingNew = true
  pendingNewPid = projectId.value
  maybeAutoStartChat()
}

// "+ 新建对话" with the agent already resolved starts immediately — no
// empty-state round trip. The agentSlug watch covers the still-resolving
// case (agent re-resolution fires after this returns).
function maybeAutoStartChat() {
  if (pendingNew && pendingNewPid === projectId.value && agentSlug.value) {
    startChat()
  }
}

async function startChat() {
  // Auto-start paths (watchers, route query) can race with the button and
  // with each other (rapid "+" clicks); only the first one creates.
  if (convStore.conversationId || loading.value) return
  // The user commits to a new conversation — a later agent re-resolution
  // must be free to load normally again.
  pendingNew = false
  pendingNewPid = null
  // Same monotonic guard as loadLatestConversation: the create() round-trip
  // can land after a project/agent switch reset everything. Without the seq
  // check, project A's new conversation overwrites the (now active) B
  // conversation and every message goes to the wrong project.
  const seq = ++latestSeq
  const pid = projectId.value
  loading.value = true
  loadError.value = null
  try {
    const data = await conversationsApi.create(pid, agentSlug.value)
    if (seq !== latestSeq) return
    convStore.startConversation(data.conversation_id)
    // A fresh conversation's budget comes from create() — the previous
    // runtime's budget (or the 128k default) would otherwise stick forever,
    // showing the wrong ContextMeter on non-default budgets.
    convStore.setTokens(0, data.token_budget, data.conversation_id)
    if (pendingOnboarding.value) {
      pendingOnboarding.value = false
      onboardingPid = null
      convStore.pendingOnboardingMessage = buildOnboardingKickoff(projectStore.currentProject?.category)
    }
  } catch (e) {
    // Same stale-response guard as loadLatestConversation: the failure must
    // only surface on the empty state it belongs to.
    if (seq !== latestSeq) return
    loadError.value = e instanceof ApiError && e.status === 404
      ? t('chatPage.projectNotFound')
      : t('chatPage.createFailed')
    console.error('Failed to start conversation', e)
  } finally {
    if (seq === latestSeq) loading.value = false
  }
}
</script>
