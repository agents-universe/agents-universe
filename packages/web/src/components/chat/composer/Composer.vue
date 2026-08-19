<template>
  <div class="composer">
    <!-- Model pills -->
    <div class="composer-providers" v-if="modelOptions.length">
      <button
        v-for="opt in modelOptions"
        :key="opt.id"
        class="provider-pill"
        :class="{ active: selectedConfigId === opt.id }"
        @click="selectConfig(opt.id)"
      ><Sparkles v-if="opt.auto" :size="12" class="provider-pill-auto" />{{ opt.label }}</button>
    </div>

    <!-- Attachments (pending uploads) -->
    <div v-if="attachments.length" class="composer-attachments">
      <div
        v-for="att in attachments"
        :key="att.key"
        class="composer-attachment"
        :class="'status-' + att.status"
      >
        <span v-if="att.status === 'uploading'" class="attachment-spinner" />
        <img v-else-if="att.objectUrl" :src="att.objectUrl" class="attachment-thumb" :alt="att.file.name" />
        <FileText v-else :size="14" class="attachment-file-icon" />
        <span class="attachment-name" :title="att.file.name">{{ att.file.name }}</span>
        <span v-if="att.error" class="attachment-error" :title="att.error">{{ t('composer.uploadFailed') }}</span>
        <button class="attachment-remove" type="button" :title="t('composer.remove')" @click="removeAttachment(att.key)">
          <X :size="12" />
        </button>
      </div>
    </div>

    <!-- Editor -->
    <div class="composer-editor" ref="editorWrap" />

    <!-- Popups -->
    <MentionPopup
      v-if="showMention"
      :exclude-slug="agentStore.currentAgent?.slug"
      @select="insertMention"
      @close="closeMention"
    />
    <SlashPopup v-if="showSlash" @select="insertSlash" @close="showSlash = false" />
    <div v-if="mentionHint" class="composer-mention-hint">{{ mentionHint }}</div>

    <!-- Actions -->
    <div class="composer-toolbar">
      <button class="composer-new-chat" type="button" :title="t('composer.newConversation')" @click="emit('new-conversation')">
        <Plus :size="16" />
      </button>
      <button class="composer-attach" type="button" :title="t('composer.attach')" @click="fileInput?.click()">
        <Paperclip :size="16" />
      </button>
      <input
        ref="fileInput"
        type="file"
        multiple
        class="composer-file-input"
        accept="image/*,text/*,.md,.csv,.json,.log,.xlsx,.xls,.pdf,.zip,.docx,.doc"
        @change="onFileInput"
      />
      <button
        v-if="isStreaming"
        class="submit-btn abort"
        @click="emit('abort')"
        :title="t('composer.stop')"
      >
        <Square :size="14" />
      </button>
      <!-- Send stays enabled while streaming: the message is queued and
           injected at the agent's next step boundary, never interrupting it. -->
      <button
        class="submit-btn"
        :disabled="isEmpty || hasPending"
        @click="submit"
        :title="hasPending ? t('composer.uploadingTitle') : isStreaming ? t('composer.sendInjectionTitle') : t('composer.sendTitle')"
      >
        <Send :size="14" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { Send, Square, Paperclip, FileText, X, Plus, Sparkles } from 'lucide-vue-next'
import { EditorView, keymap } from '@codemirror/view'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import { useAgentStore, AUTO_MODEL_CONFIG_ID } from '@/stores/agent'
import { mediaApi } from '@/api/media'
import { ApiError } from '@/api/client'
import type { AttachmentRecord } from '@/types'
import MentionPopup from './MentionPopup.vue'
import SlashPopup from './SlashPopup.vue'
import { resolveMentionAgent } from './mention'

const MAX_UPLOAD_MB = 10

interface PendingAttachment {
  key: string
  file: File
  status: 'uploading' | 'ready' | 'error'
  record?: AttachmentRecord
  error?: string
  objectUrl?: string
}

// crypto.randomUUID is unavailable in non-secure contexts (e.g. LAN http)
function genKey(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return `att-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

const props = defineProps<{
  isStreaming: boolean
  agentSlug?: string
  wsStatus?: string
  projectId: string
  conversationId: string
}>()

const emit = defineEmits<{
  submit: [{ content: string; config_id?: string; attachments?: AttachmentRecord[]; agentSlug?: string }]
  abort: []
  'new-conversation': []
}>()

const { t } = useI18n()
const agentStore = useAgentStore()
const editorWrap = ref<HTMLElement | null>(null)
const showMention = ref(false)
const showSlash = ref(false)
const isEmpty = ref(true)
// Agents @-mentioned via the popup for the current draft ({slug, label}).
// Reset on clearDraft (send success / conversation switch).
let mentionedAgents: Array<{ slug: string; label: string }> = []
const mentionHint = ref('')
let view: EditorView | null = null

const selectedConfigId = ref<string | null>(null)

const modelOptions = computed<Array<{ id: string; label: string; auto?: boolean }>>(() => [
  // "auto" always sits first; the sentinel flows through localStorage and the
  // WS payload untouched and is resolved to a real config server-side.
  { id: AUTO_MODEL_CONFIG_ID, label: t('composer.autoModel'), auto: true },
  ...agentStore.modelConfigs.map((c): { id: string; label: string; auto?: boolean } => ({
    id: c.config_id,
    label: c.is_system ? `${c.model_id} Default` : c.model_id,
  })),
])

// --- Attachments -----------------------------------------------------------

const attachments = ref<PendingAttachment[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
// Only in-flight uploads block sending — failed attachments are excluded from
// the submit payload and must not keep the send button disabled forever.
const hasPending = computed(() => attachments.value.some(a => a.status === 'uploading'))

function isImageFile(f: File) {
  return f.type.startsWith('image/')
}

function addFiles(files: FileList | File[]) {
  for (const file of Array.from(files)) {
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
      attachments.value.push({
        key: genKey(),
        file,
        status: 'error',
        error: t('composer.sizeExceeded', { mb: MAX_UPLOAD_MB }),
      })
      continue
    }
    // reactive() so upload()'s status mutations flow through the proxy —
    // mutating a raw object pushed into the ref would never trigger re-renders
    // and the send button would stay stuck on "附件上传中" (the original bug).
    const entry = reactive<PendingAttachment>({
      key: genKey(),
      file,
      status: 'uploading',
      objectUrl: isImageFile(file) ? URL.createObjectURL(file) : undefined,
    })
    attachments.value.push(entry)
    void upload(entry)
  }
}

const UPLOAD_TIMEOUT_MS = 60_000

async function upload(entry: PendingAttachment) {
  try {
    entry.record = await mediaApi.upload(
      props.projectId,
      props.conversationId,
      entry.file,
      AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
    )
    entry.status = 'ready'
  } catch (e) {
    entry.status = 'error'
    entry.error = e instanceof ApiError
      ? e.message
      : e instanceof DOMException && e.name === 'TimeoutError'
        ? t('composer.uploadTimeout')
        : t('composer.uploadFailed')
  }
}

function removeAttachment(key: string) {
  const entry = attachments.value.find(a => a.key === key)
  if (entry?.objectUrl) URL.revokeObjectURL(entry.objectUrl)
  attachments.value = attachments.value.filter(a => a.key !== key)
}

/** Local-only clear after submit — the server consumes uploads itself. */
function clearAttachments() {
  attachments.value.forEach(a => a.objectUrl && URL.revokeObjectURL(a.objectUrl))
  attachments.value = []
}

// Clear draft (editor text + attachments) when switching conversations.
// Without this, text typed for conversation A follows the reused component
// into conversation B and gets sent there. Uploaded bytes live in the
// server's in-memory store and expire via TTL — nothing to clean up.
watch(() => props.conversationId, clearDraft)

function onFileInput(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files?.length) addFiles(input.files)
  input.value = ''  // allow re-picking the same file
}

// --- Editor ----------------------------------------------------------------

watch(
  [() => agentStore.selectedConfigId, modelOptions],
  ([storeConfigId, opts]) => {
    if (!opts.length) {
      selectedConfigId.value = null
      return
    }
    if (storeConfigId && opts.some((opt) => opt.id === storeConfigId)) {
      selectedConfigId.value = storeConfigId
    } else if (!selectedConfigId.value || !opts.some((opt) => opt.id === selectedConfigId.value)) {
      // Never auto-select the "auto" sentinel: new users keep the first real
      // model (status quo) until they opt into auto.
      const fallback = opts.find((opt) => opt.id !== AUTO_MODEL_CONFIG_ID)
      if (fallback) selectConfig(fallback.id)
      else if (opts[0]) selectConfig(opts[0].id)
    }
  },
  { immediate: true },
)

onMounted(() => {
  agentStore.fetchModelConfigs()

  const submitKeymap = keymap.of([
    {
      key: 'Enter',
      run: () => {
        if (showMention.value || showSlash.value) return false
        submit()
        return true
      },
    },
    {
      key: 'Ctrl-Enter',
      run: () => { submit(); return true },
    },
    {
      key: 'Mod-Enter',
      run: () => { submit(); return true },
    },
  ])

  const updateListener = EditorView.updateListener.of((update) => {
    if (!update.docChanged) return
    const text = update.state.doc.toString()
    isEmpty.value = !text.trim()

    const lastChar = text[update.state.selection.main.head - 1]
    if (lastChar === '@') { showMention.value = true; showSlash.value = false }
    else if (lastChar === '/' && text.trim() === '/') { showSlash.value = true; showMention.value = false }
    else {
      // The slash panel closes when typing past "/" — the mention panel
      // needs the symmetric guard. Without it, a programmatic doc change
      // (e.g. clearDraft on conversation switch) leaves showMention open
      // with an empty document; the next popup click dispatches from:
      // pos-1 = -1 and crashes CodeMirror with a RangeError.
      if (showMention.value) showMention.value = false
      if (showSlash.value) {
        // The user kept typing past "/" (or deleted it) — the slash panel must
        // close, or its document-level Enter handler would later replace the
        // WHOLE editor text with "/<cmd> " .
        showSlash.value = false
      }
    }
  })

  // Paste images / files from clipboard, and drag-and-drop files onto the editor
  const attachmentHandlers = EditorView.domEventHandlers({
    paste(event) {
      const files = Array.from(event.clipboardData?.files ?? [])
      if (files.length) {
        event.preventDefault()
        addFiles(files)
        return true
      }
      return false
    },
    drop(event) {
      const files = Array.from(event.dataTransfer?.files ?? [])
      if (files.length) {
        event.preventDefault()
        addFiles(files)
        return true
      }
      return false
    },
  })

  if (!editorWrap.value) return
  view = new EditorView({
    state: EditorState.create({
      extensions: [
        submitKeymap,
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        markdown(),
        oneDark,
        updateListener,
        attachmentHandlers,
        EditorView.lineWrapping,
        EditorView.theme({
          '&': { background: 'transparent', fontSize: '14px' },
          '.cm-focused': { outline: 'none' },
          '.cm-scroller': { fontFamily: 'inherit' },
        }),
      ],
    }),
    parent: editorWrap.value,
  })
})

onBeforeUnmount(() => {
  view?.destroy()
  // Revoke any pending attachment object URLs — nothing else does on teardown.
  clearAttachments()
})

function selectConfig(configId: string) {
  selectedConfigId.value = configId
  agentStore.setSelectedConfigId(configId)
}

function submit() {
  // Streaming no longer blocks sending — the parent routes the message as
  // an in-flight injection (queued, not interrupting). Only an empty
  // editor or pending uploads guard submission.
  if (!view || hasPending.value) return
  const content = view.state.doc.toString().trim()
  const ready = attachments.value
    .filter(a => a.status === 'ready' && a.record)
    .map(a => a.record!)
  if (!content && !ready.length) return

  // Resolve @-mentions selected from the popup (see mention.ts for why
  // routing is map-based, not text-parsed).
  const { agentSlug: mentionedSlug, error: mentionError } =
    resolveMentionAgent(content, mentionedAgents)
  if (mentionError === 'multiple') {
    mentionHint.value = t('composer.mentionMultiple')
    window.setTimeout(() => { mentionHint.value = '' }, 3000)
    return
  }

  // Do NOT clear the editor here: the parent only clears on a successful
  // send (it calls clearDraft()), so a failed send (WS down) keeps the
  // user's draft instead of silently losing it.
  emit('submit', {
    content,
    config_id: selectedConfigId.value ?? undefined,
    attachments: ready.length ? ready : undefined,
    agentSlug: mentionedSlug,
  })
}

function clearDraft() {
  if (!view) return
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: '' } })
  isEmpty.value = true
  mentionedAgents = []
  clearAttachments()
}

defineExpose({ clearDraft })

function insertMention(agent: { slug: string; label: string }) {
  if (!view) return
  const pos = view.state.selection.main.head
  // Replace the trailing '@' + insert the UI display name - the user picks
  // agents by the name they see, and the mention stays readable in chat.
  view.dispatch({
    changes: { from: pos - 1, to: pos, insert: `@${agent.label} ` },
    selection: { anchor: pos + agent.label.length + 1 },
  })
  if (!mentionedAgents.some((a) => a.slug === agent.slug)) {
    mentionedAgents.push({ ...agent })
  }
  showMention.value = false
  view.focus()
}

function closeMention() {
  showMention.value = false
  // Escape closes the popup, but its autofocused input had taken focus from
  // the editor — hand focus back or the next keystrokes go nowhere.
  view?.focus()
}

function insertSlash(cmd: string) {
  if (!view) return
  const pos = view.state.selection.main.head
  view.dispatch({
    changes: { from: 0, to: pos, insert: `/${cmd} ` },
    selection: { anchor: cmd.length + 2 },
  })
  showSlash.value = false
  view.focus()
}

</script>
