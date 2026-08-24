<template>
  <div class="chat-panel">
    <!-- WS status banner -->
    <div v-if="wsStatus === 'connecting'" class="ws-status reconnecting">{{ t('chatPanel.connecting') }}</div>
    <div v-else-if="wsStatus === 'failed'" class="ws-status failed">{{ t('chatPanel.connectFailed') }}</div>

    <!-- Messages -->
    <div class="messages-list" ref="scrollEl">
      <MessageBubble
        v-for="msg in settledMessages"
        :key="msg.id"
        :message="msg"
      />

      <!-- Fresh (zero-message) conversation: show what the current agent
           can do instead of a blank list. The first message hides it. -->
      <div v-if="showAgentCapabilities" class="fresh-conversation-card">
        <AgentCapabilitiesCard :agent="agentStore.currentAgent" />
      </div>

      <!-- Streaming -->
      <div v-if="convStore.isStreaming || convStore.isThinking" class="message message-assistant streaming">
        <StreamingStatus :hide-step-info="convStore.tasks.length > 0" />
        <!-- Tool calls leading up to the plan (incl. plan_task) -->
        <div v-if="callsBeforePlan.length" class="streaming-tool-calls">
          <ToolCallCard
            v-for="tc in callsBeforePlan"
            :key="tc.callId"
            :call="tc"
            :default-expanded="true"
          />
        </div>
        <!-- Task plan — rendered right after the plan_task call that created it -->
        <TaskPlanCard
          v-if="convStore.tasks.length > 0"
          :tasks="convStore.tasks"
          :tool-calls="convStore.activeToolCalls"
        />
        <!-- Tool calls after the plan -->
        <div v-if="callsAfterPlan.length" class="streaming-tool-calls">
          <ToolCallCard
            v-for="tc in callsAfterPlan"
            :key="tc.callId"
            :call="tc"
            :default-expanded="true"
          />
        </div>
        <div v-if="convStore.streamingContent && runningTaskCount <= 1" class="message-content">
          <div v-html="renderedStreaming" />
        </div>
      </div>

      <!-- Pending selection prompts -->
      <SelectionDialog
        v-for="prompt in convStore.pendingPrompts"
        :key="prompt.promptId"
        v-bind="prompt"
        @resolve="handleResolve"
        @cancel="handleCancel"
      />

      <!-- Messages sent while the agent was still running (injections). They
           are not confirmed yet — the server consumes them at the next step
           boundary — so they render BELOW the ongoing run: new input must
           appear at the true bottom of the chat, not above the plan/tool
           cards of the run it joins. confirmInjected settles them into the
           list above the streaming area. -->
      <MessageBubble
        v-for="msg in pendingInjectedMessages"
        :key="msg.id"
        :message="msg"
      />
    </div>

    <!-- Composer -->
    <Composer
      ref="composerRef"
      :is-streaming="convStore.isStreaming || convStore.isThinking"
      :agent-slug="agentSlug"
      :ws-status="wsStatus"
      :project-id="projectId"
      :conversation-id="props.conversationId"
      @submit="handleSubmit"
      @abort="handleAbort"
      @new-conversation="emit('new-conversation')"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import { useProjectStore } from '@/stores/project'
import { useWebSocket } from '@/composables/useWebSocket'
import { renderMarkdown } from '@/utils/markdown'
import type { AttachmentRecord, ImageRecord } from '@/types'
import MessageBubble from './MessageBubble.vue'
import StreamingStatus from './StreamingStatus.vue'
import ToolCallCard from './ToolCallCard.vue'
import TaskPlanCard from './TaskPlanCard.vue'
import SelectionDialog from './SelectionDialog.vue'
import Composer from './composer/Composer.vue'
import AgentCapabilitiesCard from './AgentCapabilitiesCard.vue'

const props = defineProps<{
  conversationId: string
  agentSlug?: string
}>()

const emit = defineEmits<{ 'new-conversation': [] }>()

const { t } = useI18n()
const convStore = useConversationStore()
const agentStore = useAgentStore()
const projectStore = useProjectStore()
const scrollEl = ref<HTMLElement | null>(null)

const projectId = computed(() => projectStore.currentProject?.project_id ?? '')

// Injections sent while the agent is running are unconfirmed until the server
// consumes them — they must render BELOW the streaming block (see template)
// so new input always lands at the true bottom of the chat. Once
// confirmInjected settles them they rejoin the regular list above the run.
const pendingInjectedMessages = computed(() => {
  const pending = new Set(convStore.pendingInjected.map((p) => p.optimisticId))
  return convStore.messages.filter((m) => pending.has(m.id))
})

const settledMessages = computed(() => {
  const pending = new Set(convStore.pendingInjected.map((p) => p.optimisticId))
  return convStore.messages.filter((m) => !pending.has(m.id))
})

// A fresh conversation has no messages yet — show what the current agent can
// do instead of a blank list. The first optimistic message hides the card.
const showAgentCapabilities = computed(() =>
  !convStore.isStreaming && !convStore.isThinking && convStore.messages.length === 0,
)

const untaskedToolCalls = computed(() =>
  convStore.activeToolCalls.filter((tc) => !tc.taskId),
)

// Split untasked calls around the first plan_task call so the plan card
// renders right after the call that created it (chronological order). Before
// the plan exists, callsBeforePlan = all untasked calls, order unchanged.
const streamingPlanIndex = computed(() =>
  untaskedToolCalls.value.findIndex((tc) => tc.tool === 'plan_task'),
)

const callsBeforePlan = computed(() => {
  const calls = untaskedToolCalls.value
  const i = streamingPlanIndex.value
  return i === -1 ? calls : calls.slice(0, i + 1) // includes plan_task itself
})

const callsAfterPlan = computed(() => {
  const calls = untaskedToolCalls.value
  const i = streamingPlanIndex.value
  return i === -1 ? [] : calls.slice(i + 1)
})

const runningTaskCount = computed(() =>
  convStore.tasks.filter(t => t.status === 'running').length,
)

const composerRef = ref<InstanceType<typeof Composer> | null>(null)

const convIdRef = computed(() => props.conversationId)
const { send, abort: wsAbort, status: wsStatus } = useWebSocket(convIdRef)

// strip mermaid placeholders from the streaming preview.
// During streaming the fence may be half-closed/empty — and even a complete
// ```mermaid block can't initialize mermaid here (v-html has no component
// lifecycle; the finalized MessageBubble renders the diagram instead). A
// bare <pre class="mermaid-block"> would show up as an empty box in the live
// preview. Fall back to a plain code block showing the source.
const renderedStreaming = computed(() =>
  renderMarkdown(convStore.streamingContent).replace(
    /<pre class="mermaid-block" data-code="([^"]*)"><\/pre>/g,
    (_match, code: string) => {
      const src = decodeURIComponent(code)
      const escaped = src.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      return `<pre><code>${escaped}</code></pre>`
    },
  ),
)

// Auto-scroll must track every piece of state that changes the list's height,
// not just new messages: during a task run the plan card grows with task
// status/summary updates and per-task streaming text, tool cards grow when
// calls start/end with input/output, and images/files land mid-run. Missing
// any of these leaves the viewport where it was while the new content renders
// below the fold. The signature collapses each source to comparable strings
// so the watch fires on actual changes, not on every mutation.
const scrollSignature = computed(() => [
  convStore.messages.length,
  convStore.streamingContent.length,
  convStore.tasks.map((t) =>
    `${t.id}:${t.status}:${t.summary?.length ?? 0}:${t.error?.length ?? 0}`,
  ).join('|'),
  // Per-task streaming text lives in streamingByTask (keyed by task id), not
  // in streamingContent — taskStreamingText reads it reactively, so summing
  // its length tracks growth inside the running plan card.
  convStore.tasks
    .filter((t) => t.status === 'running')
    .map((t) => convStore.taskStreamingText(t.id).length)
    .join('|'),
  convStore.activeToolCalls.map((c) =>
    `${c.callId}:${c.status}:${c.output ? 1 : 0}`,
  ).join('|'),
  convStore.pendingPrompts.length,
].join('§'))

watch(scrollSignature, () => nextTick(() => {
  if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
}))

function handleSubmit(payload: { content: string; config_id?: string; attachments?: AttachmentRecord[]; agentSlug?: string }) {
  if (!props.conversationId) return

  const attachments = payload.attachments ?? []
  const imageRecords: ImageRecord[] = attachments
    .filter(a => a.media_type.startsWith('image/'))
    .map(a => ({ id: a.id, url: a.url, alt: a.name }))
  const fileAttachments = attachments.filter(a => !a.media_type.startsWith('image/'))

  // Two submits within the same millisecond would collide on the id and make
  // the rollback/confirmation target the wrong message — random suffix keeps
  // each optimistic message unique.
  const optimisticId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  // The agent is still running: this message is an in-flight injection —
  // track it for the input_queued / user_message_injected confirmation
  // flow (checked BEFORE startThinking sets the flag below).
  const isInjection = convStore.isStreaming || convStore.isThinking

  // An @-mention routes THIS turn to the mentioned agent (backend resolves
  // the definition by slug per message). Mid-run injections cannot switch -
  // the running agent owns the turn until it finishes - so the mention is
  // delivered as plain text and the default agent keeps answering.
  const turnAgentSlug = isInjection
    ? agentStore.currentAgent?.slug
    : payload.agentSlug ?? agentStore.currentAgent?.slug
  convStore.addMessage({
    id: optimisticId,
    role: 'user',
    content: payload.content,
    agentSlug: turnAgentSlug,
    images: imageRecords.length ? imageRecords : undefined,
    attachments: fileAttachments.length ? fileAttachments : undefined,
    timestamp: Date.now(),
  })
  convStore.startThinking()
  if (isInjection) {
    convStore.registerInjectedMessage(optimisticId, payload.content)
  }

  convStore.setTurnAgent(turnAgentSlug, props.conversationId)

  const sent = send({
    type: 'message',
    content: payload.content,
    agent_id: turnAgentSlug,
    config_id: payload.config_id ?? agentStore.selectedConfigId,
    ...(attachments.length ? { attachments } : {}),
  })
  if (!sent) {
    // Roll back the optimistic user message — a failed send must not leave
    // it in the list, or the wsStatus retry path pushes a duplicate of the
    // same content . The composer draft already stays for retry.
    convStore.removeMessage(optimisticId)
    if (isInjection) convStore.unregisterInjectedMessage(optimisticId)
    // Only clear the thinking flag this send raised — for an injection it
    // belongs to the still-running turn (isInjection was read BEFORE
    // startThinking), and stopThinking would wipe that turn's indicator
    // for the rest of its execution.
    if (!isInjection) convStore.stopThinking()
    convStore.addMessage({
      id: `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'assistant',
      content: t('chatPanel.wsNotConnected'),
      isError: true,
      timestamp: Date.now(),
    })
  } else {
    // Only clear the composer after the frame actually left the client —
    // on a failed send the draft (text + attachments) stays for retry.
    composerRef.value?.clearDraft()
  }
}

function handleAbort() {
  const sent = wsAbort()
  if (!sent && wsStatus.value !== 'connected') {
    // on a dead connection the abort frame never reaches the
    // server — it keeps running. Don't fabricate a stopped state locally;
    // the reconnect flow restores the true state from the server.
    convStore.addMessage({
      id: `abort-failed-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'assistant',
      content: t('chatPanel.abortFailed'),
      isError: true,
      timestamp: Date.now(),
    })
    return
  }
  convStore.abortStreaming()
}

function handleResolve(
  promptId: string,
  value: string,
  meta?: { secret?: boolean; serviceKey?: string; environment?: string; saveToProjectSecrets?: boolean; saveToUserTokens?: boolean },
) {
  const sent = send({
    type: 'user_selection_response',
    prompt_id: promptId,
    value,
    ...(meta?.secret && { secret: true }),
    ...(meta?.serviceKey && { service_key: meta.serviceKey }),
    ...(meta?.environment && { environment: meta.environment }),
    ...(meta?.saveToProjectSecrets && { save_to_project_secrets: true }),
    ...(meta?.saveToUserTokens && { save_to_user_tokens: true }),
  })
  // Only dismiss the prompt when the response actually left the client —
  // otherwise the agent is left waiting on an answer that was never sent.
  if (sent) convStore.resolvePrompt(promptId)
}

function handleCancel(promptId: string) {
  const sent = send({ type: 'user_selection_response', prompt_id: promptId, value: '__cancelled__' })
  if (sent) convStore.resolvePrompt(promptId)
}
</script>

<style scoped>
/* Vertically center the capability card in the empty flex-column message list. */
.fresh-conversation-card {
  margin: auto 0;
}
</style>
