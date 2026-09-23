<template>
  <div
    class="chat-panel"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- WS status banner. Every non-connected state blocks sends, so all of
         them must be visible: 'disconnected' used to render nothing at all,
         which made a dropped socket look like a frozen app. -->
    <div
      v-if="props.conversationId && wsStatus !== 'connected'"
      class="ws-status"
      :class="wsStatus === 'failed' ? 'failed' : 'reconnecting'"
    >
      <span>{{ wsStatus === 'failed' ? t('chatPanel.connectFailed') : t('chatPanel.connecting') }}</span>
      <button
        v-if="wsStatus === 'failed'"
        type="button"
        class="ws-status-retry"
        @click="reconnect"
      >
        {{ t('chatPanel.reconnect') }}
      </button>
    </div>

    <!-- Last run ended in a terminal failure while the panel was away (tab
         closed / process restarted): a passive hint - the interrupted
         partial output now lands in the history itself (startup sweep
         materializes it), and the user continues by typing. -->
    <RunNotice
      v-if="lastRunNotice"
      :run="lastRunNotice"
    />

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
        <!-- Live reasoning trace, chronologically before tools/output: it
             expands while the model thinks and collapses on thinking_end. -->
        <ThinkingBlock
          v-if="convStore.streamingThinking"
          :text="convStore.streamingThinking"
          :live="convStore.thinkingOpen"
        />
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

    <!-- Drop hint. Decorative only: the drop is handled by the panel itself,
         so the overlay must not swallow the events (pointer-events: none) or
         the drag would end on it with dragleave/drop landing nowhere useful. -->
    <div v-if="dragActive" class="drop-overlay">
      <Paperclip :size="24" />
      <span>{{ t('composer.dropToAttach') }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { Paperclip } from 'lucide-vue-next'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'
import { useProjectStore } from '@/stores/project'
import { useWebSocket } from '@/composables/useWebSocket'
import { renderMarkdown } from '@/utils/markdown'
import type { AttachmentRecord, ImageRecord } from '@/types'
import MessageBubble from './MessageBubble.vue'
import StreamingStatus from './StreamingStatus.vue'
import ThinkingBlock from './ThinkingBlock.vue'
import ToolCallCard from './ToolCallCard.vue'
import TaskPlanCard from './TaskPlanCard.vue'
import SelectionDialog from './SelectionDialog.vue'
import Composer from './composer/Composer.vue'
import AgentCapabilitiesCard from './AgentCapabilitiesCard.vue'
import RunNotice from './RunNotice.vue'

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

// Terminal last-run notice: only when the run actually failed and nothing is
// live (a streaming turn owns the panel; its end state arrives via events).
const lastRunNotice = computed(() => {
  const run = convStore.lastRun
  if (!run) return null
  if (convStore.isStreaming || convStore.isThinking) return null
  return run.status === 'interrupted' || run.status === 'failed' ? run : null
})

const composerRef = ref<InstanceType<typeof Composer> | null>(null)

const convIdRef = computed(() => props.conversationId)
const { send, abort: wsAbort, status: wsStatus, reconnect } = useWebSocket(convIdRef)

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

// --- Drag & drop attachments ------------------------------------------------
// The whole panel is a drop target, not just the CodeMirror editor: dropping a
// file on the message list used to do nothing at all — or worse, made the
// browser navigate away and open the file, losing the session. Dropped files
// go through the composer, which owns the upload queue and renders the
// attachment strip; the picker, clipboard paste and drops share that one path.
const dragDepth = ref(0)
const dragActive = computed(() => dragDepth.value > 0)

/** OS file drags only. Text/selection drags must keep their native behavior
 *  (dragging a snippet within the editor, dropping text into it). */
function carriesFiles(e: DragEvent): boolean {
  const types = e.dataTransfer?.types
  return !!types && Array.from(types).includes('Files')
}

function resetDrag() {
  dragDepth.value = 0
}

function onDragEnter(e: DragEvent) {
  if (!carriesFiles(e)) return
  // dragenter/dragleave fire for every descendant crossed while dragging over
  // the panel; counting keeps the hint up until the pointer really leaves.
  e.preventDefault()
  dragDepth.value++
}

function onDragOver(e: DragEvent) {
  if (!carriesFiles(e)) return
  // Without this the browser never delivers the drop event at all.
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}

function onDragLeave(e: DragEvent) {
  if (!carriesFiles(e)) return
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}

function onDrop(e: DragEvent) {
  const files = Array.from(e.dataTransfer?.files ?? [])
  resetDrag()
  if (!files.length) return
  e.preventDefault()
  composerRef.value?.addFiles(files)
}

// A drag that ends outside the panel (Esc, drop on the sidebar, drop past the
// window edge) gets no matching dragleave here — without these the hint stays
// on screen, covering the panel, until the next reload.
onMounted(() => {
  window.addEventListener('dragend', resetDrag)
  window.addEventListener('drop', resetDrag)
})

onBeforeUnmount(() => {
  window.removeEventListener('dragend', resetDrag)
  window.removeEventListener('drop', resetDrag)
})

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

  // An injection cannot switch agents — the running agent owns the turn — so
  // it must not re-stamp the attribution either: doing so dropped the
  // @-mention label from the status line and stamped the turn's final
  // assistant message with the default agent instead of the mentioned one.
  if (!isInjection) convStore.setTurnAgent(turnAgentSlug, props.conversationId)

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

/** A prompt answer can only travel over the socket. The dialog stays open so
 *  the user can retry once reconnected, but a silent no-op reads as "the app
 *  ignored my click" — surface the reason and start a reconnect. */
function reportPromptSendFailure() {
  convStore.addMessage({
    id: `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: 'assistant',
    content: t('chatPanel.wsNotConnected'),
    isError: true,
    timestamp: Date.now(),
  })
  reconnect()
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
  else reportPromptSendFailure()
}

function handleCancel(promptId: string) {
  const sent = send({ type: 'user_selection_response', prompt_id: promptId, value: '__cancelled__' })
  if (sent) convStore.resolvePrompt(promptId)
  else reportPromptSendFailure()
}
</script>

<style scoped>
/* Vertically center the capability card in the empty flex-column message list. */
.fresh-conversation-card {
  margin: auto 0;
}

/* Drop hint while an OS file drag is over the panel (.chat-panel is the
   positioned ancestor). pointer-events: none keeps the events flowing to the
   panel's own handlers instead of ending on this element. */
.drop-overlay {
  position: absolute;
  inset: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  pointer-events: none;
  color: var(--accent);
  font-size: 14px;
  background: rgba(15, 15, 15, 0.72);
  border: 2px dashed var(--accent);
  border-radius: 8px;
}
</style>
