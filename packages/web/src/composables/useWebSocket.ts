import { ref, watch, computed, reactive } from 'vue'
import type { Ref } from 'vue'
import { useConversationStore } from '@/stores/conversation'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useMemoryStore } from '@/stores/memory'
import { conversationsApi } from '@/api/conversations'
import { apiBase } from '@/utils/basePath'
import type { WsStatus, WsMessage, ToolCallRecord, ImageRecord, AttachmentRecord, SelectionPrompt, PersonalMemory, EpisodicMemory } from '@/types'

const MAX_RETRIES = 3
const BACKOFF_MS = [1000, 2000, 4000]
const PING_INTERVAL_MS = 30_000
// Half-open detection: the server answers every ping with a pong, so a
// healthy connection always produces a frame within one interval. 3× gives
// slack for busy agent runs where the server event loop may lag.
const STALE_MS = PING_INTERVAL_MS * 3
const MAX_CONNECTIONS = 5

interface ConnectionEntry {
  ws: WebSocket | null
  status: Ref<WsStatus>
  retries: number
  retryTimer: ReturnType<typeof setTimeout> | null
  pingTimer: ReturnType<typeof setInterval> | null
  connectedConversationId: string | null
  terminalMessageId: string | null
  lastMessageAt: number
  staleProbeSent: boolean
}

// Module-level state: one entry per conversation with an open WS
const _connections = new Map<string, ConnectionEntry>()

// conversations whose retries were exhausted. The connection
// ENTRY is removed from _connections (it must not count toward
// MAX_CONNECTIONS forever), but the UI still needs to show "连接失败" — the
// status computed checks this set first. Reactive so the computed re-runs
// when a conversation enters/leaves the failed state (a plain Set mutation
// never triggers it, leaving the UI stuck on 'disconnected').
const _failedConversations = reactive(new Set<string>())

/** Close the oldest idle (non-streaming) connection to stay under the limit.
 * Returns true if a connection was evicted. */
function _closeOldestIdle(excludeId: string): boolean {
  const conv = useConversationStore()
  for (const [id] of _connections) {
    if (id === excludeId) continue
    const isStreaming = !!conv.streamingIds[id]
    if (!isStreaming) {
      _cleanupConnection(id)
      return true
    }
  }
  return false
}

function _cleanupConnection(id: string) {
  const entry = _connections.get(id)
  if (!entry) return
  if (entry.pingTimer) {
    clearInterval(entry.pingTimer)
    entry.pingTimer = null
  }
  if (entry.retryTimer) {
    clearTimeout(entry.retryTimer)
    entry.retryTimer = null
  }
  if (entry.ws) {
    entry.ws.onopen = null
    entry.ws.onclose = null
    entry.ws.onerror = null
    entry.ws.onmessage = null
    entry.ws.close()
    entry.ws = null
  }
  _connections.delete(id)
}

/** Close all WS connections (used on project/agent switch). */
export function closeAllConnections() {
  for (const id of Array.from(_connections.keys())) {
    _cleanupConnection(id)
  }
}

/** Close the WS connection for a single conversation (e.g. on delete). */
export function closeConnection(id: string) {
  _cleanupConnection(id)
}

export function useWebSocket(conversationId: Ref<string | null>) {
  const conv = useConversationStore()
  const knowledge = useKnowledgeStore()
  const memory = useMemoryStore()

  const status = computed<WsStatus>(() => {
    if (!conversationId.value) return 'disconnected'
    if (_failedConversations.has(conversationId.value)) return 'failed'
    return _connections.get(conversationId.value)?.status.value ?? 'disconnected'
  })

  function connect(id: string) {
    // A fresh attempt clears the failed marker; retries start over.
    _failedConversations.delete(id)
    // If already connected and open, just ensure status is correct
    const existing = _connections.get(id)
    if (existing?.ws && existing.ws.readyState === WebSocket.OPEN) {
      existing.status.value = 'connected'
      return
    }

    // Enforce max connections
    if (_connections.size >= MAX_CONNECTIONS && !_closeOldestIdle(id)) {
      // Every existing connection is streaming a live run. Evicting one
      // would permanently kill that conversation's UI updates: its entry is
      // deleted, streamingIds[id] never clears (events stop arriving) and
      // the conversationId watch never refires, so the sidebar shows
      // "运行中" forever. Refuse this connection instead and retry shortly,
      // in case a stream ends and a slot frees up .
      // If an entry already exists for this id (CONNECTING handshake or a
      // retry backoff), it must be cleaned up BEFORE the map is overwritten —
      // otherwise its onopen still fires, starts a ping timer and reloads
      // history, and the conversation ends up with TWO parallel connections
      // (every stream_delta appended twice).
      if (existing) _cleanupConnection(id)
      const statusRef = ref<WsStatus>('connecting')
      const entry: ConnectionEntry = {
        ws: null,
        status: statusRef,
        retries: 0,
        retryTimer: null,
        pingTimer: null,
        connectedConversationId: null,
        terminalMessageId: null,
        lastMessageAt: 0,
        staleProbeSent: false,
      }
      _connections.set(id, entry)
      // Re-arms the same entry instead of re-entering connect(): connect()
      // rebuilds the entry with retries: 0, so the give-up check below was
      // dead code and a saturated server retried forever with the UI stuck
      // on 'connecting' (no 'failed' state ever surfaced).
      const retryWhenFull = () => {
        entry.retryTimer = setTimeout(() => {
          entry.retryTimer = null
          if (entry.retries >= MAX_RETRIES) {
            // no slot freed up in time — drop the pending
            // entry instead of leaving a 'failed' tombstone in _connections
            // forever; the UI failure state lives in _failedConversations.
            _failedConversations.add(id)
            _cleanupConnection(id)
            return
          }
          entry.retries++
          // A stream may have ended since the last attempt, freeing a slot —
          // evict an idle connection if possible, otherwise keep waiting.
          if (_connections.size >= MAX_CONNECTIONS && !_closeOldestIdle(id)) {
            retryWhenFull()
            return
          }
          _open(id, entry)
        }, 5000)
      }
      retryWhenFull()
      return
    }

    // Clean up any broken connection for this ID
    if (existing) {
      _cleanupConnection(id)
    }

    const statusRef = ref<WsStatus>('connecting')
    const entry: ConnectionEntry = {
      ws: null,
      status: statusRef,
      retries: 0,
      retryTimer: null,
      pingTimer: null,
      connectedConversationId: null,
      terminalMessageId: null,
      lastMessageAt: 0,
      staleProbeSent: false,
    }
    _connections.set(id, entry)
    _open(id, entry)
  }

  function _open(id: string, entry: ConnectionEntry) {
    let errorFired = false
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}${apiBase}/ws/conversations/${id}`)
    entry.ws = ws

    ws.onopen = () => {
      entry.connectedConversationId = id
      entry.status.value = 'connected'
      entry.retries = 0
      entry.lastMessageAt = Date.now()
      entry.staleProbeSent = false
      _startPing(id, entry)
      // Reconcile local streaming state on EVERY successful connection, not
      // just reconnects: a connection that died while the server turn kept
      // running (and finished before the retries gave up) leaves
      // isStreaming/isThinking/stale streamingContent behind with no event
      // to clear them — the server sends nothing once the turn is over —
      // and the panel would show "正在输出…" / "运行中" forever.
      // preserveDraft keeps the localStorage draft so loadHistory →
      // _applyDraft can rebuild an interrupted-execution recovery message.
      conv.clearStreamingState(id, { preserveDraft: true })
      void _reloadHistory(id)
    }

    ws.onmessage = (event) => {
      // Any frame (including the server's {type:'pong'} reply) proves the
      // connection is alive — refresh the watchdog timestamp before parsing.
      entry.lastMessageAt = Date.now()
      entry.staleProbeSent = false
      try {
        const msg = JSON.parse(event.data as string) as WsMessage
        _dispatch(id, msg, entry)
      } catch {
        // ignore malformed frames
      }
    }

    ws.onclose = () => {
      if (errorFired) return
      if (entry.status.value === 'connected') {
        entry.status.value = 'disconnected'
      }
      _scheduleRetry(id, entry)
    }

    ws.onerror = () => {
      errorFired = true
      entry.status.value = 'disconnected'
      _scheduleRetry(id, entry)
    }
  }

  async function _reloadHistory(id: string) {
    try {
      const [messages, tasks, latestRun] = await Promise.all([
        conversationsApi.getMessages(id),
        conversationsApi.getTasks(id),
        conversationsApi.getLatestRun(id),
      ])
      conv.loadHistory(messages, id)
      conv.setTasks(tasks, id)
      conv.setLastRun(latestRun, id)
    } catch {
      // A later reconnect or manual refresh can recover the persisted history.
    }
  }

  function _scheduleRetry(id: string, entry: ConnectionEntry) {
    if (entry.retries >= MAX_RETRIES) {
      // Give up: stop the watchdog and drop the entry entirely. Leaving a
      // 'failed' tombstone in _connections would permanently count against
      // MAX_CONNECTIONS, so _closeOldestIdle could evict a HEALTHY idle
      // connection to make room for a dead one  — and the tombstone
      // itself is never removed unless the user reopens that conversation.
      // remove the entry; the failure state for the UI is
      // kept in _failedConversations.
      _failedConversations.add(id)
      _cleanupConnection(id)
      return
    }
    entry.retryTimer = setTimeout(() => {
      entry.retries++
      _open(id, entry)
    }, BACKOFF_MS[entry.retries] ?? 4000)
  }

  function _dispatch(convId: string, msg: WsMessage, entry: ConnectionEntry) {
    // Background connections stay open after switching conversations, so
    // global (non-conversation-scoped) stores must only be updated when this
    // event belongs to the conversation currently shown in the UI.
    const isActiveConversation = convId === conv.conversationId
    switch (msg.type) {
      case 'sync':
        conv.applySync(convId, {
          streamingContent: (msg.streaming_text as string) || '',
          // The server sends snake_case tool-call fields (call_id, task_id,
          // current_step, next_step). Passed through unmapped, callId would
          // be undefined on every card: completeToolCall and the
          // user_selection_required match would never resolve them, and the
          // end-of-turn snapshot would flag live successful tools as errors.
          toolCalls: ((msg.tool_calls as Array<Record<string, unknown>> | null | undefined) ?? []).map((tc) => ({
            callId: tc.call_id as string,
            tool: (tc.tool as string) ?? '',
            input: (tc.input ?? {}) as Record<string, unknown>,
            output: tc.output as Record<string, unknown> | undefined,
            status: (tc.status as ToolCallRecord['status']) ?? 'running',
            taskId: tc.task_id as string | undefined,
            currentStep: tc.current_step as string | undefined,
            nextStep: tc.next_step as string | undefined,
          })),
        })
        break
      case 'stream_delta':
        conv.appendDelta(msg.delta as string, msg.task_id as string | undefined, convId)
        break
      case 'stream_end': {
        const messageId = msg.message_id as string | undefined
        const taskId = msg.task_id as string | undefined
        const interrupted = (msg.stop_reason as string) === 'interrupted'
        if (messageId && messageId === entry.terminalMessageId) {
          entry.terminalMessageId = null
          break
        }
        if (taskId) {
          // Task stream end: route to per-task finalize
          conv.finalizeStreaming(messageId ?? `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, taskId, convId)
        } else if (conv.hasStreamingContent(convId)) {
          // interrupted: snapshot the partial output as an "interrupted"
          // message (the agent keeps going with the injected instruction).
          conv.finalizeStreaming(messageId ?? `assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, undefined, convId, { interrupted })
        } else if (interrupted) {
          // Empty interrupted snapshot (injection consumed at the first step
          // boundary — nothing had streamed yet). The agent is still running;
          // do not stop the streaming flag, no partial content to snapshot.
          break
        } else {
          conv.stopStreaming(convId)
        }
        break
      }
      case 'input_queued':
        // Injection accepted into the queue — backfill the optimistic
        // message's server id (null in the claim-window buffer path).
        conv.markInputQueued((msg.message_id as string | null) ?? null, (msg.content as string) ?? '', convId)
        break
      case 'user_message_injected':
        // The message was persisted AND consumed by the agent — replace the
        // optimistic id and move it after the interrupted snapshot.
        conv.confirmInjected((msg.message_id as string) ?? '', (msg.content as string) ?? '', convId)
        break
      case 'input_rejected':
      case 'input_not_processed':
        // Settled without the agent consuming it (validation failure, or the
        // turn ended first) — attach the server's notice to the message.
        // Unlike 'error', the streaming state is NOT cleared: the turn may
        // still be running.
        conv.rejectInjected((msg.content as string) ?? '', (msg.message as string) ?? 'Message was not processed.', convId)
        break
      case 'token_update':
        conv.setTokens(msg.used as number, msg.budget as number, convId)
        break
      case 'knowledge_loaded':
        conv.setLoadedKnowledge((msg.files ?? msg.slugs) as string[], convId)
        if (isActiveConversation) knowledge.setLoadedThisTurn((msg.files ?? msg.slugs) as string[])
        break
      case 'knowledge_updated':
        if (isActiveConversation) knowledge.triggerRefresh()
        break
      case 'task_plan_created':
        conv.setTasks(msg.tasks, convId)
        break
      case 'task_started':
        conv.updateTask(msg.task_id as string, {
          status: 'running',
          currentStep: msg.current_step as string | undefined,
          nextStep: msg.next_step as string | undefined,
          progressCompleted: msg.progress_completed as number | undefined,
          progressTotal: msg.progress_total as number | undefined,
          // Model that actually executes this subtask (auto routing).
          modelName: msg.actual_model as string | undefined,
        }, convId)
        break
      case 'task_progress':
        conv.updateTask(msg.task_id as string, {
          currentStep: msg.current_step as string | undefined,
          nextStep: msg.next_step as string | undefined,
          progressCompleted: msg.progress_completed as number | undefined,
          progressTotal: msg.progress_total as number | undefined,
        }, convId)
        break
      case 'task_completed':
        conv.updateTask(msg.task_id as string, { status: 'completed', summary: msg.summary as string | undefined }, convId)
        break
      case 'task_failed':
        conv.updateTask(msg.task_id as string, { status: 'failed', error: msg.error as string | undefined }, convId)
        break
      case 'task_skipped':
        conv.updateTask(msg.task_id as string, { status: 'skipped', error: msg.error as string | undefined }, convId)
        break
      case 'tool_call_preparing':
        conv.addToolCall({
          callId: msg.call_id as string,
          tool: msg.tool as string,
          input: {},
          status: 'preparing',
          taskId: msg.task_id as string | undefined,
        } satisfies ToolCallRecord, convId)
        break
      case 'tool_call_start':
        conv.upgradeToolCall(
          msg.call_id as string,
          (msg.input ?? {}) as Record<string, unknown>,
          {
            taskId: msg.task_id as string | undefined,
            currentStep: msg.current_step as string | undefined,
            nextStep: msg.next_step as string | undefined,
          },
          convId,
        )
        break
      case 'tool_call_end': {
        const output = (msg.output ?? {}) as Record<string, unknown>
        const tcStatus = (msg.status as ToolCallRecord['status'])
          ?? (output.error ? 'error' : 'done')
        conv.completeToolCall(msg.call_id as string, output, tcStatus, convId)
        break
      }
      case 'context_usage':
        conv.setContextUsage({
          staticFiles: (msg.static_files ?? 0) as number,
          dynamicFiles: (msg.dynamic_files ?? 0) as number,
          deferredFiles: (msg.deferred_files ?? 0) as number,
          overflowFiles: (msg.overflow_files ?? 0) as number,
          conversationHistoryTokens: (msg.conversation_history_tokens ?? 0) as number,
          pendingTaskTokens: (msg.pending_task_tokens ?? 0) as number,
          totalBudget: (msg.total_budget ?? 0) as number,
        }, convId)
        break
      case 'knowledge_dynamic_load':
        if (isActiveConversation) knowledge.addDynamicLoad({ slug: msg.slug as string, boundToTask: (msg.task_id as string) ?? null })
        break
      case 'knowledge_dynamic_unload': {
        const unloadSlugs = (msg.slugs as string[] | undefined) ?? (msg.slug ? [msg.slug as string] : [])
        if (isActiveConversation) for (const s of unloadSlugs) knowledge.removeDynamicLoad(s)
        break
      }
      case 'model_selected':
        // tier is the auto-routing tier ("low"/"mid"/"high"), only set on auto
        // turns; explicit selections show the model id alone. The provider is
        // not stored — the model id already identifies it.
        conv.setModelInfo((msg.tier ?? '') as string, (msg.model ?? '') as string, convId)
        break
      case 'image_output': {
        // Guard against a malformed/partial payload — addStreamingImages
        // would otherwise push undefined into the store's list.
        const images = Array.isArray(msg.images) ? (msg.images as ImageRecord[]) : []
        if (images.length > 0) conv.addStreamingImages(images, convId)
        // removed the IndexedDB cache write here — cacheImages
        // downloaded every image to an object store that nothing ever read
        // (display goes straight through /api/media), so it was pure network
        // + storage waste. Delete utils/imageCache.ts too.
        break
      }
      case 'file_output': {
        const files = Array.isArray(msg.files) ? (msg.files as AttachmentRecord[]) : []
        if (files.length > 0) conv.addStreamingFiles(files, convId)
        break
      }
      case 'user_selection_required':
        conv.addPendingPrompt({
          promptId: msg.prompt_id as string,
          fieldKey: msg.field_key as string ?? '',
          question: msg.question as string,
          options: (msg.options ?? []) as SelectionPrompt['options'],
          allowOther: msg.allow_other as boolean ?? true,
          kind: (msg.kind as SelectionPrompt['kind']) ?? 'selection',
          title: msg.title as string | undefined,
          message: msg.message as string | undefined,
          secret: msg.secret as boolean | undefined,
          taskId: msg.task_id as string | undefined,
          serviceKey: msg.service_key as string | undefined,
          environment: msg.environment as string | undefined,
          saveToProjectSecrets: msg.save_to_project_secrets as boolean | undefined,
          saveToUserTokens: msg.save_to_user_tokens as boolean | undefined,
        }, convId)
        // Mark the running user_confirm tool call as done so UI stops showing "工具运行中"
        for (const tc of conv.getActiveToolCalls(convId)) {
          if (tc.tool === 'user_confirm' && tc.status === 'running') {
            conv.completeToolCall(tc.callId, {}, 'done', convId)
            break
          }
        }
        break
      case 'memory_saved':
        if (isActiveConversation) {
          if (msg.memory) {
            memory.addPersonalMemory(msg.memory as PersonalMemory)
          } else {
            memory.triggerRefresh()
          }
        }
        break
      case 'memory_archived':
        if (isActiveConversation) memory.archivePersonalMemory(msg.memory_id as string)
        break
      case 'session_memory_added':
        if (isActiveConversation) memory.addSessionNote(msg.note as string)
        break
      case 'episode_generated':
        // Guard the payload: EpisodicTimeline renders ep.summary and would
        // crash on an undefined entry .
        if (isActiveConversation && msg.episode) memory.addEpisode(msg.episode as EpisodicMemory)
        break
      case 'secrets_updated':
        Promise.all([
          import('@/stores/projectSecrets'),
          import('@/stores/project'),
        ]).then(([{ useProjectSecretsStore }, { useProjectStore }]) => {
          const pid = useProjectStore().currentProject?.project_id
          if (pid) useProjectSecretsStore().load(pid)
        })
        break
      case 'user_tokens_updated':
        import('@/stores/userTokens').then(({ useUserTokensStore }) => {
          useUserTokensStore().load()
        })
        break
      case 'budget_exceeded':
        conv.setTokens(msg.used as number, msg.budget as number, convId)
        break
      case 'abort_ack':
        // The agent session died (TTL expiry / user stop): the server
        // cancelled the task loop, so no further task stream events will
        // arrive — leaving streamingByTask/activeToolCalls alive here would
        // strand the conversation in a permanent "running" state. Tear down
        // every in-flight streaming state; prompts awaiting user input can
        // never be answered either, so drop those dialogs too.
        conv.clearStreamingState(convId)
        conv.clearPendingPrompts(convId)
        break
      case 'warning': {
        // Non-fatal server-side notices (loop budget exhausted, response
        // truncation, compression timeout...). Surface as a visible
        // error-styled bubble WITHOUT touching streaming state — most
        // warnings arrive while the run is still going; the loop-exhausted
        // one is followed by its own stream_end right after.
        conv.addMessage({
          id: `warn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          role: 'assistant',
          content: (msg.message as string) ?? 'Warning',
          isError: true,
          timestamp: Date.now(),
        }, convId)
        break
      }
      case 'error': {
        const messageId = (msg.stream_message_id as string) ?? `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
        entry.terminalMessageId = messageId
        // A server-side error settles the turn: injections still pending were
        // never consumed — surface the failure on each optimistic message
        // instead of leaving the entry stuck in pendingInjected forever.
        conv.rejectAllPendingInjected('服务器错误，消息未处理', convId)
        conv.clearStreamingState(convId)
        conv.addMessage({
          id: messageId,
          role: 'assistant',
          content: (msg.message as string) ?? 'An error occurred.',
          isError: true,
          timestamp: Date.now(),
        }, convId)
        break
      }
    }
  }

  function send(payload: WsMessage): boolean {
    const id = conversationId.value
    if (!id) return false
    const entry = _connections.get(id)
    if (!entry?.ws || entry.ws.readyState !== WebSocket.OPEN) return false
    entry.ws.send(JSON.stringify(payload))
    return true
  }

  function abort(): boolean {
    // the caller needs to know whether the abort frame actually
    // left the client — on a dead connection the server keeps running.
    return send({ type: 'abort' })
  }

  function _startPing(_id: string, entry: ConnectionEntry) {
    if (entry.pingTimer) clearInterval(entry.pingTimer)
    entry.pingTimer = setInterval(() => {
      if (entry.ws && entry.ws.readyState === WebSocket.OPEN) {
        // Half-open detection: TCP can drop without FIN/RST (laptop sleep,
        // captive portal) — onclose never fires, sends vanish silently and
        // the UI shows "connected" forever. BUT background-tab timer
        // throttling can also starve ping/pong for minutes on a HEALTHY
        // connection; closing immediately on stale would kill it and churn
        // clearStreamingState + history reload. So the first stale tick only
        // sends a probe ping; only if the NEXT tick still has no frame
        // (probe unanswered) is the connection declared dead and closed.
        if (Date.now() - entry.lastMessageAt > STALE_MS) {
          if (entry.staleProbeSent) {
            entry.ws.close()
            return
          }
          entry.staleProbeSent = true
          entry.ws.send(JSON.stringify({ type: 'ping' }))
          return
        }
        entry.ws.send(JSON.stringify({ type: 'ping' }))
      }
    }, PING_INTERVAL_MS)
  }

  watch(
    conversationId,
    (id) => {
      if (id) {
        connect(id)
      }
      // Don't disconnect old connections - they continue in background
    },
    { immediate: true },
  )

  return { send, abort, status }
}
