import { defineStore } from 'pinia'
import { ref, computed, reactive } from 'vue'
import { i18n } from '@/i18n'
import type {
  Message,
  ToolCallRecord,
  ImageRecord,
  AttachmentRecord,
  SelectionPrompt,
  AgentTask,
  ContextUsage,
  DbMessage,
  DbTask,
  ConversationRun,
} from '@/types'

/**
 * Map tasks to the UI shape. Accepts both the DB row shape (`task_id`,
 * snake_case fields) and the live websocket event shape (`id`, camelCase)
 * emitted by `task_plan_created` - the live path previously lost every id,
 * so `updateTask` never matched and statuses never updated.
 */
export function mapDbTasks(tasks: DbTask[]): AgentTask[] {
  return tasks.map((t) => {
    const raw = t as DbTask & { id?: string }
    return {
      id: raw.task_id ?? raw.id ?? '',
      title: raw.title ?? '',
      status: (raw.status as AgentTask['status']) ?? 'pending',
      modelTier: raw.estimated_complexity ?? undefined,
      summary: raw.result_summary ?? undefined,
      error: raw.error_message ?? undefined,
      currentStep: raw.current_step ?? undefined,
      nextStep: raw.next_step ?? undefined,
      progressCompleted: raw.progress_completed ?? undefined,
      progressTotal: raw.progress_total ?? undefined,
      dependsOn: raw.depends_on ?? undefined,
    }
  })
}

/** An optimistic user message awaiting its server-side confirmation. */
interface PendingInjection {
  optimisticId: string
  content: string
  /** Backfilled by input_queued (null in the claim-window buffer path). */
  serverId: string | null
}

/** Per-conversation streaming / runtime state. */
interface ConversationRuntime {
  messages: Message[]
  streamingContent: string
  streamingByTask: Record<string, string>
  isThinking: boolean
  isStreaming: boolean
  streamingStartTime: number | null
  tokensUsed: number
  tokenBudget: number
  contextUsage: ContextUsage | null
  tasks: AgentTask[]
  loadedKnowledge: string[]
  activeToolCalls: ToolCallRecord[]
  streamingImages: ImageRecord[]
  streamingFiles: AttachmentRecord[]
  currentModelTier: string | null
  currentModelName: string | null
  /** Agent answering the live turn (mentioned agent on @-mention turns). */
  turnAgentSlug: string | null
  pendingPrompts: SelectionPrompt[]
  pendingInjected: PendingInjection[]
  /** True between the user's Stop and the next turn's first content event:
   *  abortStreaming snapshotted the error tool cards into an abort message,
   *  so a later empty stream_end snapshot is a duplicate and must be skipped.
   *  Real tool failures never set this — their final message must be pushed. */
  abortSnapshotted: boolean
  /** Durable status of the most recent agent turn (from /runs/latest).
   *  Null until the server responds; cleared when a new turn starts. */
  lastRun: ConversationRun | null
}

function createRuntime(): ConversationRuntime {
  return {
    messages: [],
    streamingContent: '',
    streamingByTask: {},
    isThinking: false,
    isStreaming: false,
    streamingStartTime: null,
    tokensUsed: 0,
    tokenBudget: 128000,
    contextUsage: null,
    tasks: [],
    loadedKnowledge: [],
    activeToolCalls: [],
    streamingImages: [],
    streamingFiles: [],
    currentModelTier: null,
    currentModelName: null,
    turnAgentSlug: null,
    pendingPrompts: [],
    pendingInjected: [],
    abortSnapshotted: false,
    lastRun: null,
  }
}

export const useConversationStore = defineStore('conversation', () => {
  const activeId = ref<string | null>(null)
  const runtimes = new Map<string, ConversationRuntime>()
  // Throttle clock for appendDelta's draft persistence (see appendDelta).
  let _lastDraftSaveTs = 0

  /** Track which conversations are streaming (for sidebar indicator).
   *  Uses a reactive object (not Set) so that property access in templates
   *  is reliably tracked by Vue's reactivity system through Pinia. */
  const streamingIds = reactive<Record<string, boolean>>({})

  function ensureRuntime(id: string): ConversationRuntime {
    let rt = runtimes.get(id)
    if (!rt) {
      rt = reactive(createRuntime()) as ConversationRuntime
      runtimes.set(id, rt)
    }
    return rt
  }

  function getRuntime(id?: string): ConversationRuntime | undefined {
    const targetId = id ?? activeId.value
    if (!targetId) return undefined
    return runtimes.get(targetId)
  }

  /** Mark a conversation as streaming (or clear the flag). */
  function _updateStreamingFlag(id: string, isStreaming: boolean) {
    const rt = runtimes.get(id)
    const hasActivity = rt && (rt.isStreaming || rt.isThinking || rt.activeToolCalls.some(tc => tc.status === 'running' || tc.status === 'preparing'))
    if (isStreaming || hasActivity) {
      streamingIds[id] = true
    } else {
      delete streamingIds[id]
    }
  }

  // ── Computed: expose active runtime properties reactively ──────────

  const conversationId = computed(() => activeId.value)

  const activeRuntime = computed(() => {
    if (!activeId.value) return undefined
    return ensureRuntime(activeId.value)
  })

  const messages = computed(() => activeRuntime.value?.messages ?? [])
  const streamingContent = computed(() => activeRuntime.value?.streamingContent ?? '')
  const isThinking = computed(() => activeRuntime.value?.isThinking ?? false)
  const pendingInjected = computed(() => activeRuntime.value?.pendingInjected ?? [])
  const isStreaming = computed(() => activeRuntime.value?.isStreaming ?? false)
  const streamingStartTime = computed(() => activeRuntime.value?.streamingStartTime ?? null)
  const tokensUsed = computed(() => activeRuntime.value?.tokensUsed ?? 0)
  const tokenBudget = computed(() => activeRuntime.value?.tokenBudget ?? 128000)
  const contextUsage = computed(() => activeRuntime.value?.contextUsage ?? null)
  const tasks = computed(() => activeRuntime.value?.tasks ?? [])
  const loadedKnowledge = computed(() => activeRuntime.value?.loadedKnowledge ?? [])
  const activeToolCalls = computed(() => activeRuntime.value?.activeToolCalls ?? [])
  const streamingImages = computed(() => activeRuntime.value?.streamingImages ?? [])
  const currentModelTier = computed(() => activeRuntime.value?.currentModelTier ?? null)
  const currentModelName = computed(() => activeRuntime.value?.currentModelName ?? null)
  const pendingPrompts = computed(() => activeRuntime.value?.pendingPrompts ?? [])
  /** Agent answering the live turn — mentioned agent on @-mention turns,
   *  conversation default otherwise. Consumed by StreamingStatus to surface
   *  "which agent is being called" during the turn. */
  const turnAgentSlug = computed(() => activeRuntime.value?.turnAgentSlug ?? null)
  const lastRun = computed(() => activeRuntime.value?.lastRun ?? null)

  // ── Mutation methods (accept optional targetId) ───────────────────

  function startThinking(targetId?: string) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    rt.isThinking = true
    // A fresh turn supersedes any previous run's terminal notice.
    rt.lastRun = null
    // New content arrived — a previous turn's abort snapshot is stale.
    rt.abortSnapshotted = false
    // The turn is provably alive — any draft-recovery note is stale.
    _dropStaleRecovery(id)
    _updateStreamingFlag(id, true)
  }

  function stopThinking(targetId?: string) {
    const rt = getRuntime(targetId)
    if (rt) {
      rt.isThinking = false
      _updateStreamingFlag(targetId ?? activeId.value!, false)
    }
  }

  /** Store the latest durable run status (fetched on conversation open /
   *  reconnect). Target runtime may not exist yet for a background
   *  conversation — ensureRuntime creates it. */
  function setLastRun(run: ConversationRun | null, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.lastRun = run
  }

  function clearLastRun(targetId?: string) {
    const rt = getRuntime(targetId)
    if (rt) rt.lastRun = null
  }

  /** Record which agent answers the turn being started (ChatPanel routes
   *  @-mention turns to the mentioned agent). Snapshotted onto each assistant
   *  message so the UI can badge it, and reset when the turn winds down. */
  function setTurnAgent(slug: string | null | undefined, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.turnAgentSlug = slug ?? null
  }

  function stopStreaming(targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    rt.isStreaming = false
    rt.isThinking = false
    rt.streamingStartTime = null
    // a finished turn must not leak its in-flight state into the
    // next one — otherwise the TaskPlanCard re-renders last turn's tasks and
    // the next message snapshots stale tool calls/images. Message bubbles
    // already carry their own copies (pushStreamingMessage snapshots).
    rt.activeToolCalls = []
    rt.streamingImages = []
    rt.streamingFiles = []
    rt.tasks = []
    // Per-task text buffers are streaming state too — without this an
    // aborted text-only task left them counted by hasStreamingContent and
    // the turn could never leave the "running" state.
    rt.streamingByTask = {}
    // Same leak guard for the @-mention turn agent - the next turn defaults
    // back to the conversation agent until ChatPanel routes otherwise.
    rt.turnAgentSlug = null
    _updateStreamingFlag(id, false)
  }

  function setConversationId(id: string) {
    activeId.value = id
    _saveConversationId(id)
  }

  function startConversation(id: string) {
    activeId.value = id
    _saveConversationId(id)
    // Ensure runtime exists but don't reset existing state
    ensureRuntime(id)
    // The knowledge panel's "loaded this turn" chips describe the PREVIOUS
    // conversation once it switches away — knowledge_loaded events only
    // update it for the active conversation, so the stale list would linger
    // against the new session. Clear it here; the next turn's events refill
    // it. Dynamic import avoids a store cycle (dynamic import is what
    // project.ts uses for the same reason).
    import('./knowledge').then(({ useKnowledgeStore }) => useKnowledgeStore().setLoadedThisTurn([]))
  }

  function _saveConversationId(id: string) {
    try {
      const projectId = localStorage.getItem('agents-universe:currentProjectId')
      if (projectId) {
        localStorage.setItem(`agents-universe:conv:${projectId}`, id)
      }
    } catch { /* ignore */ }
  }

  function _saveDraft(targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = runtimes.get(id)
    if (!rt) return
    if (!rt.activeToolCalls.length && !rt.streamingContent && !rt.streamingImages.length && !rt.streamingFiles.length) return
    try {
      localStorage.setItem(`agents-universe:draft:${id}`, JSON.stringify({
        activeToolCalls: rt.activeToolCalls,
        streamingContent: rt.streamingContent,
        streamingImages: rt.streamingImages,
        streamingFiles: rt.streamingFiles,
        tasks: rt.tasks,
        savedAt: Date.now(),
      }))
    } catch { /* quota exceeded */ }
  }

  function _clearDraft(targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    try { localStorage.removeItem(`agents-universe:draft:${id}`) } catch { /* ignore */ }
  }

  function _dropStaleRecovery(targetId: string) {
    // A live turn resumed (new thinking, fresh delta, completed snapshot, or
    // a non-empty sync) — any `recovered-` message is a false "interrupted"
    // note from a page reload that raced the server: the turn never died.
    // Drop the note only, NOT the draft: the draft is the recovery record
    // for a genuinely interrupted execution, and a live turn must not erase
    // it — if the turn later really dies (e.g. crash after the last tool
    // event), the user's partial output would become unrecoverable. Drafts
    // are cleared on their terminal paths (clearStreamingState,
    // pushStreamingMessage's completion branches, removeRuntime).
    const rt = runtimes.get(targetId)
    if (!rt || !rt.messages.some((m) => m.id.startsWith('recovered-'))) return
    rt.messages = rt.messages.filter((m) => !m.id.startsWith('recovered-'))
  }

  function _applyDraft(targetId: string) {
    if (!targetId) return
    const rt = ensureRuntime(targetId)
    try {
      const raw = localStorage.getItem(`agents-universe:draft:${targetId}`)
      if (!raw) return
      const draft = JSON.parse(raw) as {
        activeToolCalls: ToolCallRecord[]
        streamingContent: string
        streamingImages: ImageRecord[]
        streamingFiles: AttachmentRecord[]
        tasks: AgentTask[]
        savedAt: number
      }
      // Expire drafts older than 2 hours
      if (Date.now() - draft.savedAt > 2 * 60 * 60 * 1000) {
        localStorage.removeItem(`agents-universe:draft:${targetId}`)
        return
      }
      if (!draft.activeToolCalls?.length && !draft.streamingContent) return
      const recovered: Message = {
        // Unique per interruption: with a fixed `recovered-{targetId}` id, a
        // SECOND interruption in the same runtime (draft re-saved, loadHistory
        // re-run) hits the dedup guard below and silently drops the recovery
        // message — plus deletes the fresh draft. Keying on savedAt gives each
        // interruption its own message and lets the guard keep working as the
        // once-per-draft dedup it is.
        id: `recovered-${targetId}-${draft.savedAt}`,
        role: 'assistant',
        content: draft.streamingContent || '',
        toolCalls: draft.activeToolCalls?.map((tc) => ({
          ...tc,
          status: (tc.status === 'running' || tc.status === 'preparing') ? 'error' as const : tc.status,
          output: (tc.status === 'running' || tc.status === 'preparing')
            ? { error: i18n.global.t('conversationStore.interruptedOnRefresh') }
            : tc.output,
        })),
        images: draft.streamingImages?.length ? draft.streamingImages : undefined,
        attachments: draft.streamingFiles?.length ? draft.streamingFiles : undefined,
        isError: true,
        timestamp: draft.savedAt,
      }
      // Dedup: _applyDraft runs again on reconnect/history refresh — without
      // this guard the same recovery message is appended once per load.
      if (rt.messages.some((m) => m.id === recovered.id)) {
        localStorage.removeItem(`agents-universe:draft:${targetId}`)
        return
      }
      rt.messages = [...rt.messages, recovered]
      if (draft.tasks?.length) rt.tasks = draft.tasks
    } catch { /* ignore */ }
  }

  function getSavedConversationId(projectId: string): string | null {
    try {
      return localStorage.getItem(`agents-universe:conv:${projectId}`)
    } catch { return null }
  }

  function clearProjectStorage(projectId: string) {
    try {
      const savedId = getSavedConversationId(projectId)
      const knownIds = new Set([activeId.value, savedId].filter((id): id is string => Boolean(id)))
      for (const id of knownIds) localStorage.removeItem(`agents-universe:draft:${id}`)
      localStorage.removeItem(`agents-universe:conv:${projectId}`)
    } catch { /* ignore storage failures */ }
  }

  function loadHistory(msgs: DbMessage[], targetConversationId?: string) {
    const id = targetConversationId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)

    const history = msgs
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({
        id: m.message_id,
        role: m.role as 'user' | 'assistant',
        content: m.content,
        agentSlug: m.agent_slug || undefined,
        modelName: m.model_name || undefined,
        toolCalls: (m.tool_calls ?? []).map((tc) => {
          const storedStatus = tc.status as ToolCallRecord['status']
          const status = (storedStatus === 'running' || storedStatus === 'preparing')
            ? 'error'
            : storedStatus ?? (tc.output?.error ? 'error' : 'done')
          const output = tc.output ?? (status === 'error' ? { error: i18n.global.t('conversationStore.toolCallAborted') } : undefined)
          return {
            callId: tc.call_id,
            tool: tc.tool,
            input: tc.input,
            output,
            status,
            ...(tc.task_id ? { taskId: tc.task_id } : {}),
          }
        }),
        images: m.images?.length ? m.images.map((img) => ({
          ...img,
          id: img.id || img.url,
        })) : undefined,
        attachments: m.attachments?.length ? m.attachments : undefined,
        interrupted: m.interrupted || undefined,
        // Persisted turn-level failure (mirror of the live error bubble's
        // isError) — renders with the error styling after a reload.
        isError: m.error || undefined,
        timestamp: new Date(m.created_at).getTime(),
      }))

    // Do not let a delayed history request erase the optimistic user message or
    // the in-flight assistant response for this same conversation.
    if (rt.isThinking || rt.isStreaming) {
      // The turn is still live — a previous abort snapshot is stale (same
      // semantics as appendDelta/startThinking).
      rt.abortSnapshotted = false
      const persisted = new Set(history.map((message) => message.id))
      // the server may have persisted the optimistic user
      // message under a server-generated id, so an id-only filter would keep
      // the local copy next to the persisted one — same role + same content
      // counts as the same message, but only when the persisted copy is not
      // older than the local one (an earlier identical question from history
      // must not swallow the in-flight injection).
      rt.messages = [...history, ...rt.messages.filter((message) => {
        if (persisted.has(message.id)) return false
        if (message.id.startsWith('recovered-')) return false
        if (message.role === 'user'
            && history.some((h) => h.role === 'user' && h.content === message.content
                            && h.timestamp >= message.timestamp)) {
          // Mirror the non-streaming branch: drop the stale pending entry
          // too, or confirmInjected's content-fallback match could latch
          // onto it for a later, genuinely-new injection of the same text.
          unregisterInjectedMessage(message.id, id)
          return false
        }
        return true
      })]
      return
    }
    // `rt.messages = history` would drop locally-recovered
    // draft messages (they live only in localStorage, never in the DB) — a
    // reconnect right after an interruption would then re-append them via
    // _applyDraft with a fresh id. Preserve already-recovered messages
    // across the history reload so the _applyDraft dedup guard can match
    // them by id.
    // Preserve already-recovered messages across the history reload so the
    // _applyDraft dedup guard can match them by id. They are recovery notes
    // for an interrupted execution — semantically the LAST message, so they
    // belong at the tail, not the top (prepending moved them above the whole
    // history on a second reconnect).
    const hadLocal = rt.messages.length > 0
    const oldMessages = rt.messages
    const localRecovered = oldMessages.filter((m) => m.id.startsWith('recovered-'))
    // An unconsumed injection (pendingInjected) has no DB row yet — replacing
    // the list outright would drop its optimistic message and the later
    // user_message_injected confirm would no-op: the user's just-sent
    // instruction vanishes from the UI while the agent still executes it.
    const pendingIds = new Set(rt.pendingInjected.map((p) => p.optimisticId))
    // Same dedup rule as the streaming branch above: the server may have
    // persisted an injection while the socket was down (the confirm events
    // were dropped with the connection and are never replayed), so the
    // optimistic copy would sit next to its own persisted row on every
    // reload forever, and confirmInjected's content-fallback match could
    // latch onto the stale entry. Drop copies whose role+content already
    // exist in history at an equal-or-newer timestamp, unregistering their
    // pending entries; a genuinely unconsumed injection has no history row
    // and is preserved.
    const pendingOptimistic = oldMessages.filter((m) => {
      if (!pendingIds.has(m.id)) return false
      if (m.role === 'user'
          && history.some((h) => h.role === 'user' && h.content === m.content
                          && h.timestamp >= m.timestamp)) {
        unregisterInjectedMessage(m.id, id)
        return false
      }
      return true
    })
    rt.messages = [...history, ...localRecovered, ...pendingOptimistic]
    // Reconnect race: the history GET's DB snapshot can predate the turn's
    // final stream_end — the server pushes the final message to the client
    // BEFORE its DB commit, so a message pushed while the GET was in flight
    // is in neither history nor localRecovered/pendingOptimistic. Keep those
    // stragglers (anything at-or-after the last history row) instead of
    // dropping the just-finished answer. The timestamp guard also keeps the
    // compression path safe: rows replaced by compression are always older
    // than the summary line.
    const keptIds = new Set([
      ...history.map((m) => m.id),
      ...localRecovered.map((m) => m.id),
      ...pendingOptimistic.map((m) => m.id),
    ])
    const lastTs = history.length ? history[history.length - 1].timestamp : 0
    for (const m of oldMessages) {
      if (keptIds.has(m.id)) continue
      if (m.timestamp >= lastTs) rt.messages.push(m)
    }
    // History replaced — a previous turn's abort snapshot is stale.
    rt.abortSnapshotted = false
    // Append any in-progress execution that was interrupted before DB persist.
    // Only when the local list was empty (fresh page load) AND the draft is
    // newer than the last persisted message. A turn that finished while
    // offline has its output persisted, so its stale draft must not be
    // resurrected as a fake "interrupted" recovery message — the old gate
    // ("any interrupted message in history") could not see the offline
    // finish, and a single old interrupted message wrongly blocked every
    // later recovery. A final interrupted snapshot also skips recovery
    // (already shown).
    if (id && !hadLocal) {
      let draftSavedAt = 0
      try {
        const raw = localStorage.getItem(`agents-universe:draft:${id}`)
        if (raw) draftSavedAt = (JSON.parse(raw) as { savedAt?: number }).savedAt ?? 0
      } catch { /* ignore */ }
      const lastMsg = history[history.length - 1]
      // timestamp is the map's already-parsed created_at (ms)
      const lastTs = lastMsg ? lastMsg.timestamp : 0
      if (draftSavedAt > lastTs && !lastMsg?.interrupted) {
        _applyDraft(id)
      }
    }
  }

  function addMessage(msg: Message, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.messages.push(msg)
  }

  function removeMessage(messageId: string, targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = getRuntime(id)
    if (!rt) return
    rt.messages = rt.messages.filter((m) => m.id !== messageId)
  }

  function appendDelta(delta: string, taskId?: string, targetId?: string) {
    // A malformed frame missing `delta` must not render the literal string
    // "undefined" into the message text .
    if (!delta) return
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    // Streaming deltas prove the turn is alive — drop any stale recovery note.
    _dropStaleRecovery(id)
    if (taskId) {
      rt.streamingByTask[taskId] = (rt.streamingByTask[taskId] ?? '') + delta
    } else {
      rt.streamingContent += delta
    }
    // New content arrived — a previous turn's abort snapshot is stale.
    rt.abortSnapshotted = false
    rt.isStreaming = true
    if (!rt.streamingStartTime) {
      rt.streamingStartTime = Date.now()
    }
    _updateStreamingFlag(id, true)
    // Persist a draft so a pure-text turn interrupted by a refresh can be
    // recovered — _saveDraft otherwise only runs on tool events and a
    // text-only reply would have no recovery record (the server persists
    // assistant messages only at stream_end). Throttled: deltas arrive per
    // frame and a localStorage write per frame would jank the page.
    const now = Date.now()
    if (now - _lastDraftSaveTs >= 500) {
      _lastDraftSaveTs = now
      _saveDraft(id)
    }
  }

  function taskStreamingText(taskId: string, targetId?: string): string {
    const rt = getRuntime(targetId)
    return rt?.streamingByTask[taskId] ?? ''
  }

  function clearStreamingState(targetId?: string, opts?: { preserveDraft?: boolean }) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    rt.streamingContent = ''
    rt.streamingByTask = {}
    rt.isStreaming = false
    rt.isThinking = false
    rt.streamingStartTime = null
    rt.activeToolCalls = []
    rt.streamingImages = []
    rt.streamingFiles = []
    // completed tasks must not leak into the next turn's
    // TaskPlanCard. The reconnect path (preserveDraft) is safe: _applyDraft
    // restores tasks from the draft right after this runs.
    rt.tasks = []
    // The WS reconnect path passes preserveDraft: _reloadHistory → _applyDraft
    // runs right after and needs the draft to rebuild the interrupted-execution
    // recovery message. Deleting it first would silently destroy the only
    // record of the interruption .
    if (!opts?.preserveDraft) _clearDraft(id)
    _updateStreamingFlag(id, false)
  }

  function pushStreamingMessage(messageId: string, content?: string, isError = false, targetId?: string, opts?: { interrupted?: boolean }) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    // A main-stream stream_end must not wipe parallel task streams: capture
    // the flag BEFORE clearStreamingState clears streamingByTask. A task
    // still alive counts even before its first text — it may be sitting on
    // an in-flight tool call (activeToolCalls carries taskId) while the
    // main thread finishes first.
    const hasParallelTasks = Object.keys(rt.streamingByTask).length > 0
      || rt.activeToolCalls.some((tc) => tc.taskId && (tc.status === 'running' || tc.status === 'preparing'))
    const msg: Message = {
      id: messageId || `assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'assistant',
      content: content ?? rt.streamingContent,
      // The snapshot must not carry tools still in running/preparing — this
      // message is final (stream ended), and a forever-spinning tool chip
      // would render beside it. An interrupted snapshot normalizes them to
      // 'interrupted' (the user's injection cut the step); a plain stream_end
      // to 'error', mirroring _applyDraft's recovery semantics.
      toolCalls: rt.activeToolCalls.length > 0
        ? rt.activeToolCalls.map((tc) => {
            const active = tc.status === 'running' || tc.status === 'preparing'
            if (!active) return tc
            if (opts?.interrupted) {
              return { ...tc, status: 'interrupted' as const }
            }
            return {
              ...tc,
              status: 'error' as const,
              output: { error: i18n.global.t('conversationStore.toolCallAborted') },
            }
          })
        : undefined,
      images: rt.streamingImages.length > 0 ? [...rt.streamingImages] : undefined,
      attachments: rt.streamingFiles.length > 0 ? [...rt.streamingFiles] : undefined,
      modelTier: rt.currentModelTier ?? undefined,
      modelName: rt.currentModelName ?? undefined,
      agentSlug: rt.turnAgentSlug ?? undefined,
      isError: isError || undefined,
      interrupted: opts?.interrupted || undefined,
      timestamp: Date.now(),
    }
    // After an abort, the abort message already snapshotted the stop reason
    // and the error tool cards; when the server's final stream_end lands
    // there is nothing left to render. Skip the push instead of creating a
    // duplicate empty bubble — but never skip a message that carries text,
    // completed tool cards (real final state the abort never showed),
    // images/files, an error, or an interrupted snapshot. Real tool
    // failures are NOT duplicates: their error cards are the turn's only
    // output, so the skip is limited to aborts (abortSnapshotted) and to
    // snapshots with no tool cards at all.
    const noText = !(content ?? rt.streamingContent)
    const noFreshTools = rt.activeToolCalls.every((tc) => tc.status === 'error' || tc.status === 'interrupted')
    const emptySnapshot = noText && noFreshTools
      && rt.streamingImages.length === 0 && rt.streamingFiles.length === 0
    if (emptySnapshot && !isError && !opts?.interrupted
        && (rt.activeToolCalls.length === 0 || rt.abortSnapshotted)) {
      if (hasParallelTasks) {
        rt.streamingContent = ''
        rt.isThinking = false
        rt.streamingStartTime = null
        _updateStreamingFlag(id, true)
        // Parallel tasks still stream — the turn is provably alive, so a
        // recovery note (from a reload that raced this turn) is stale.
        _dropStaleRecovery(id)
        _clearDraft(id)
      } else {
        clearStreamingState(id)
      }
      return
    }
    rt.messages.push(msg)
    // The turn completed — a draft-recovery note from a reload that raced
    // this turn is stale and would render beside the real final message.
    _dropStaleRecovery(id)
    if (hasParallelTasks) {
      // Tasks still streaming: reset only the main-stream state, keep the
      // per-task buffers and running tool calls alive for their own
      // stream_end / tool_call_end events.
      rt.streamingContent = ''
      rt.isThinking = false
      rt.streamingStartTime = null
      _updateStreamingFlag(id, true)
      _clearDraft(id)
    } else if (opts?.interrupted) {
      // Interrupted snapshot: clear snapshotted content but keep the
      // streaming/thinking flags alive - the agent continues with the
      // injected instruction at the next step boundary.
      rt.streamingContent = ''
      rt.activeToolCalls = []
      rt.streamingImages = []
      rt.streamingFiles = []
      rt.streamingStartTime = null
      _updateStreamingFlag(id, true)
      _clearDraft(id)
    } else {
      clearStreamingState(id)
    }
  }

  function _finalizeTurnIfIdle(id: string) {
    // Turn wind-down, last straw: when every task is terminal and nothing
    // streams (no per-task buffer, no main text, no live tool call), any
    // residual images/files that landed AFTER the main snapshot (task-run
    // output) are folded into one final message — stopStreaming() would
    // silently drop them. Then stop the streaming indicator. Without this,
    // a task round with an in-flight injection can end with done/error tool
    // cards in activeToolCalls and no further events: isStreaming stays true
    // forever (spinning Stop button, "running" pulse) and the next turn
    // snapshots the finished task's cards into a fresh message.
    const rt = getRuntime(id)
    if (!rt) return
    const planActive = rt.tasks.some((t) => t.status === 'pending' || t.status === 'running')
    const liveTool = rt.activeToolCalls.some((tc) => tc.status === 'running' || tc.status === 'preparing')
    if (Object.keys(rt.streamingByTask).length > 0 || rt.streamingContent
        || planActive || liveTool) return
    if (rt.streamingImages.length > 0 || rt.streamingFiles.length > 0) {
      pushStreamingMessage(`final-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, undefined, false, id)
    }
    stopStreaming(id)
  }

  function finalizeStreaming(messageId: string, taskId?: string, targetId?: string, opts?: { interrupted?: boolean }) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    if (taskId) {
      // Task stream end: clear the per-task buffer. The text was already
      // visible inside the TaskPlanCard; we don't push it as a standalone
      // message (it belongs to the task, not the main conversation thread).
      delete rt.streamingByTask[taskId]
      // stopStreaming() would wipe a live tool call or unconsumed
      // image/file output from the finished task without a message snapshot
      // (task-run images/attachments vanish from the UI) — only wind down
      // when the whole turn is idle (also covers the mid-run-injection
      // sequence where the interrupted snapshot cleared activeToolCalls
      // before the remaining tasks' events arrive).
      _finalizeTurnIfIdle(id)
      return
    }
    pushStreamingMessage(messageId, undefined, false, id, opts)
  }

  function failStreaming(messageId: string, errorMessage: string, targetId?: string) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    for (const tc of rt.activeToolCalls) {
      if (tc.status === 'running' || tc.status === 'preparing') {
        tc.output = { error: errorMessage }
        tc.status = 'error'
      }
    }
    if (rt.streamingContent || Object.keys(rt.streamingByTask).length > 0 || rt.activeToolCalls.length > 0 || rt.streamingImages.length > 0 || rt.streamingFiles.length > 0) {
      pushStreamingMessage(messageId, rt.streamingContent || errorMessage, true, id)
    } else {
      rt.messages.push({
        id: messageId || `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'assistant',
        content: errorMessage,
        isError: true,
        timestamp: Date.now(),
      })
      stopStreaming(id)
    }
  }

  function abortStreaming(reason = '工具调用已停止', targetId?: string) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    for (const tc of rt.activeToolCalls) {
      if (tc.status === 'running' || tc.status === 'preparing') {
        tc.output = { error: reason }
        tc.status = 'error'
      }
    }
    if (rt.streamingContent || Object.keys(rt.streamingByTask).length > 0 || rt.activeToolCalls.length > 0 || rt.streamingImages.length > 0 || rt.streamingFiles.length > 0) {
      // A second Stop before the server's abort_ack arrives has nothing new
      // to snapshot — the first abort message already holds this turn's
      // state (the error cards were marked above on both calls).
      if (rt.abortSnapshotted) return
      // Mark that the abort message below IS the snapshot of this turn —
      // the server's final stream_end carries the same error cards and must
      // be skipped as a duplicate (see the emptySnapshot check).
      rt.abortSnapshotted = true
      // Task text lives in streamingByTask, not the main thread — fold it
      // into the snapshot or it (and the task cards) vanish on Stop.
      const taskText = Object.values(rt.streamingByTask).filter(Boolean)
      const snapshot = [rt.streamingContent || reason, ...taskText].filter(Boolean).join('\n\n')
      pushStreamingMessage(`abort-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, snapshot, true, id)
    } else {
      stopStreaming(id)
    }
  }

  function addStreamingImages(images: ImageRecord[], targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.streamingImages.push(...images)
  }

  // ── In-flight injection (user keeps typing while the agent runs) ──

  function registerInjectedMessage(optimisticId: string, content: string, targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    rt.pendingInjected.push({ optimisticId, content, serverId: null })
  }

  function unregisterInjectedMessage(optimisticId: string, targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    rt.pendingInjected = rt.pendingInjected.filter((p) => p.optimisticId !== optimisticId)
  }

  function markInputQueued(serverId: string | null, content: string, targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    const pending = rt.pendingInjected.find((p) => p.serverId === null && p.content === content)
    if (pending) pending.serverId = serverId
  }

  function confirmInjected(serverId: string, content: string, targetId?: string) {
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    const pending = rt.pendingInjected.find(
      (p) => (p.serverId !== null && p.serverId === serverId) || (p.serverId === null && p.content === content),
    )
    if (!pending) return
    // Replace the optimistic id with the server id and move the message to
    // the END of the list — after the interrupted snapshot, before the
    // continuing stream (the DB sequence matches: snapshot → injected user
    // message → next assistant stream).
    const idx = rt.messages.findIndex((m) => m.id === pending.optimisticId)
    if (idx >= 0) {
      const msg = rt.messages[idx]
      msg.id = serverId
      rt.messages.splice(idx, 1)
      rt.messages.push(msg)
    }
    rt.pendingInjected = rt.pendingInjected.filter((p) => p !== pending)
  }

  function rejectInjected(content: string, message: string, targetId?: string) {
    // input_rejected / input_not_processed: the message is settled without
    // the agent consuming it. Clear the pending entry and attach the
    // server's notice to the optimistic message — a user's words must never
    // vanish silently.
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = ensureRuntime(id)
    const pending = rt.pendingInjected.find((p) => p.content === content)
    if (!pending) return
    const idx = rt.messages.findIndex((m) => m.id === pending.optimisticId)
    if (idx >= 0) {
      rt.messages[idx].content = `${rt.messages[idx].content}\n\n> ${message}`
    }
    rt.pendingInjected = rt.pendingInjected.filter((p) => p !== pending)
  }

  function addStreamingFiles(files: AttachmentRecord[], targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    for (const f of files) {
      // Same name = same deliverable regenerated mid-turn; replace so the
      // chat shows one download link per file instead of one per rewrite.
      const i = rt.streamingFiles.findIndex((s) => s.name === f.name)
      if (i >= 0) rt.streamingFiles[i] = f
      else rt.streamingFiles.push(f)
    }
  }

  function setTokens(used: number, budget: number, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.tokensUsed = used
    rt.tokenBudget = budget
  }

  function setContextUsage(usage: ContextUsage, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.contextUsage = usage
  }

  function setTasks(rawTasks: unknown, targetId?: string) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    const list = Array.isArray(rawTasks) ? rawTasks : []
    rt.tasks = mapDbTasks(list as DbTask[])
  }

  function updateTask(id: string, updates: Partial<AgentTask>, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    const idx = rt.tasks.findIndex((t) => t.id === id)
    if (idx >= 0) {
      rt.tasks[idx] = { ...rt.tasks[idx], ...updates }
    }
    // A task reaching a terminal state (completed/failed/skipped) via the
    // crash path never gets its own stream_end — agent-core's
    // _execute_and_finish emits task_failed without closing the task stream.
    // The stranded per-task buffer keeps hasParallelTasks true at turn end,
    // so clearStreamingState is skipped and the stale plan (red X / grey
    // skipped) leaks into the next turn — a retried task then never
    // repaints. Clearing the buffer on terminal status lets the turn wind
    // down and drop the stale plan. Idempotent on the normal path (stream_end
    // already deleted it).
    if (updates.status === 'completed' || updates.status === 'failed' || updates.status === 'skipped') {
      delete rt.streamingByTask[id]
    }
    // The task's stream_end can arrive BEFORE its task_completed (the agent
    // emits them in that order) — at that point planActive was still true,
    // so finalizeStreaming did not wind down. The last task becoming
    // terminal is the turn's true end: re-check idle now.
    _finalizeTurnIfIdle(targetId ?? activeId.value!)
  }

  function setLoadedKnowledge(slugs: string[], targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.loadedKnowledge = slugs
  }

  function rejectAllPendingInjected(message: string, targetId?: string) {
    // Turn-level failure (WS 'error'): every injection still awaiting
    // confirmation was never consumed by the agent — settle them all with the
    // failure notice instead of leaving the entries pending forever.
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = runtimes.get(id)
    if (!rt) return
    for (const pending of [...rt.pendingInjected]) {
      rejectInjected(pending.content, message, id)
    }
  }

  function addToolCall(call: ToolCallRecord, targetId?: string) {
    if (!call.callId) return
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    const existing = rt.activeToolCalls.findIndex((c) => c.callId === call.callId)
    if (existing >= 0) {
      rt.activeToolCalls[existing] = { ...rt.activeToolCalls[existing], ...call }
    } else {
      rt.activeToolCalls.push(call)
    }
    rt.isStreaming = true
    _saveDraft(id)
    _updateStreamingFlag(id, true)
  }

  function upgradeToolCall(
    callId: string,
    input: Record<string, unknown>,
    extra?: { taskId?: string; currentStep?: string; nextStep?: string },
    targetId?: string,
  ) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    const idx = rt.activeToolCalls.findIndex((c) => c.callId === callId)
    if (idx >= 0) {
      rt.activeToolCalls[idx] = {
        ...rt.activeToolCalls[idx],
        input,
        status: 'running',
        ...(extra?.taskId !== undefined && { taskId: extra.taskId }),
        ...(extra?.currentStep !== undefined && { currentStep: extra.currentStep }),
        ...(extra?.nextStep !== undefined && { nextStep: extra.nextStep }),
      }
    } else {
      rt.activeToolCalls.push({
        callId,
        tool: '',
        input,
        status: 'running',
        ...(extra?.taskId !== undefined && { taskId: extra.taskId }),
        ...(extra?.currentStep !== undefined && { currentStep: extra.currentStep }),
        ...(extra?.nextStep !== undefined && { nextStep: extra.nextStep }),
      })
    }
    _saveDraft(id)
  }

  function completeToolCall(callId: string, output: Record<string, unknown>, status: ToolCallRecord['status'], targetId?: string) {
    const id = targetId ?? activeId.value!
    const rt = ensureRuntime(id)
    const idx = rt.activeToolCalls.findIndex((c) => c.callId === callId)
    if (idx >= 0) {
      rt.activeToolCalls[idx] = { ...rt.activeToolCalls[idx], output, status }
    }
    _saveDraft(id)
    _updateStreamingFlag(id, false)
  }

  function setModelInfo(tier: string, modelName: string, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.currentModelTier = tier
    rt.currentModelName = modelName
  }

  function addPendingPrompt(prompt: SelectionPrompt, targetId?: string) {
    const rt = ensureRuntime(targetId ?? activeId.value!)
    rt.pendingPrompts.push(prompt)
  }

  function resolvePrompt(promptId: string, targetId?: string) {
    const rt = getRuntime(targetId)
    if (rt) {
      rt.pendingPrompts = rt.pendingPrompts.filter((p) => p.promptId !== promptId)
    }
  }

  function clearPendingPrompts(targetId?: string) {
    // Session-death signal (abort_ack): the agent task was cancelled, so any
    // prompt awaiting user input can never be answered — drop them all
    // instead of leaving stale dialogs that would reply into a dead session.
    const id = targetId ?? activeId.value
    if (!id) return
    const rt = runtimes.get(id)
    if (rt) rt.pendingPrompts = []
  }

  // ── Query helpers for WS dispatch ──────────────────────────────────

  function hasStreamingContent(targetId: string): boolean {
    const rt = runtimes.get(targetId)
    return !!rt && (!!rt.streamingContent || Object.keys(rt.streamingByTask).length > 0 || rt.activeToolCalls.length > 0 || rt.streamingImages.length > 0 || rt.streamingFiles.length > 0)
  }

  function getActiveToolCalls(targetId: string): ToolCallRecord[] {
    return runtimes.get(targetId)?.activeToolCalls ?? []
  }

  /** Apply a sync event from the server (WS reconnect mid-stream). */
  function applySync(targetId: string, sync: { streamingContent: string; toolCalls: ToolCallRecord[] }) {
    const rt = ensureRuntime(targetId)
    // Only apply if we don't already have live streaming content (avoid
    // overwriting deltas that arrived between connect and sync).
    if (!rt.streamingContent && sync.streamingContent) {
      rt.streamingContent = sync.streamingContent
      rt.isStreaming = true
      rt.isThinking = false
      // Live content resumed — a previous abort snapshot is stale.
      rt.abortSnapshotted = false
      if (!rt.streamingStartTime) {
        rt.streamingStartTime = Date.now()
      }
    }
    if (sync.toolCalls?.length && !rt.activeToolCalls.length) {
      rt.activeToolCalls = sync.toolCalls
      // Mirror addToolCall: the chat panel's streaming UI is gated on
      // isStreaming — a mid-tool-call reconnect would otherwise show no
      // in-progress indicator until the next stream_delta (or forever, if
      // the tool produces no further text).
      rt.isStreaming = true
      rt.isThinking = false
      // Tool calls are live — a previous abort snapshot is stale.
      rt.abortSnapshotted = false
      if (!rt.streamingStartTime) {
        rt.streamingStartTime = Date.now()
      }
    }
    // Empty sync (reconnect after the turn already finished server-side)
    // must NOT re-arm the streaming flag — nothing would ever clear it and
    // the conversation would show "running" forever in the tree.
    if (sync.streamingContent || sync.toolCalls?.length) {
      // Non-empty sync proves the server turn is alive — any recovery note
      // injected by a history fetch that raced the sync is a false alarm.
      _dropStaleRecovery(targetId)
      _updateStreamingFlag(targetId, true)
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────────

  function reset() {
    activeId.value = null
    runtimes.clear()
    for (const key of Object.keys(streamingIds)) delete streamingIds[key]
  }

  function removeRuntime(id: string) {
    runtimes.delete(id)
    delete streamingIds[id]
    try { localStorage.removeItem(`agents-universe:draft:${id}`) } catch { /* ignore */ }
  }

  return {
    conversationId,
    messages,
    streamingContent,
    isThinking,
    isStreaming,
    streamingStartTime,
    tokensUsed,
    tokenBudget,
    contextUsage,
    tasks,
    loadedKnowledge,
    activeToolCalls,
    streamingImages,
    currentModelTier,
    currentModelName,
    pendingPrompts,
    turnAgentSlug,
    lastRun,
    pendingInjected,
    streamingIds,
    startThinking,
    stopThinking,
    setLastRun,
    clearLastRun,
    setTurnAgent,
    stopStreaming,
    clearStreamingState,
    pushStreamingMessage,
    failStreaming,
    abortStreaming,
    setConversationId,
    startConversation,
    loadHistory,
    addMessage,
    removeMessage,
    appendDelta,
    finalizeStreaming,
    taskStreamingText,
    addStreamingImages,
    addStreamingFiles,
    registerInjectedMessage,
    unregisterInjectedMessage,
    markInputQueued,
    confirmInjected,
    rejectInjected,
    rejectAllPendingInjected,
    setTokens,
    setContextUsage,
    setTasks,
    updateTask,
    setLoadedKnowledge,
    addToolCall,
    upgradeToolCall,
    completeToolCall,
    setModelInfo,
    addPendingPrompt,
    resolvePrompt,
    clearPendingPrompts,
    hasStreamingContent,
    getActiveToolCalls,
    applySync,
    reset,
    removeRuntime,
    getSavedConversationId,
    clearProjectStorage,
  }
})
