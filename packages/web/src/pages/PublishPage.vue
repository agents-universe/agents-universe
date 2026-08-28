<template>
  <div class="publish-page">
    <div v-if="loading" class="publish-state">
      <div class="publish-spinner">
        <Loader2 :size="20" class="spin" />
      </div>
    </div>

    <div v-else-if="error" class="publish-state publish-error">
      <div class="publish-error-card">
        <AlertTriangle :size="20" />
        <p>{{ error }}</p>
      </div>
    </div>

    <template v-else-if="session">
      <!-- Ambient glow -->
      <div class="publish-glow" aria-hidden="true" />

      <!-- The window is a centered floating dialog card: a real "chat box"
           with its own header bar, message area and composer — not a page
           that fills the viewport. -->
      <div class="publish-window">
        <header class="publish-header">
          <div class="publish-agent-avatar">
            <Bot :size="24" />
          </div>
          <div class="publish-header-info">
            <span class="publish-header-eyebrow">{{ t('publishPage.embeddedTitle') }}</span>
            <h1 class="publish-title">{{ session.title || session.agent?.display_name || session.agent?.slug || t('publishPage.untitled') }}</h1>
            <p v-if="session.description" class="publish-desc">{{ session.description }}</p>
          </div>
          <span class="publish-badge" title="模型由发布者绑定">
            <Zap :size="11" />
            {{ t('publishPage.publisherModelBadge') }}
          </span>
          <button
            type="button"
            class="publish-close"
            :title="t('common.close')"
            @click="close"
          >
            <X :size="16" />
          </button>
        </header>

        <div class="publish-chat">
          <!-- History -->
          <div class="publish-messages" ref="scrollEl">
            <div v-if="!messages.length && !streamingText" class="publish-welcome">
              <div class="publish-welcome-icon">
                <Sparkles :size="22" />
              </div>
              <p class="publish-welcome-title">{{ t('publishPage.welcomeTitle') }}</p>
              <p class="publish-welcome-hint">{{ t('publishPage.welcomeHint') }}</p>
            </div>

            <div
              v-for="msg in messages"
              :key="msg.message_id"
              class="publish-bubble"
              :class="[msg.role === 'user' ? 'user' : 'assistant', { error: msg.error }]"
            >
              <div v-if="msg.role !== 'user'" class="publish-bubble-meta">
                <span class="publish-bubble-dot" />
                <span>{{ session.agent?.display_name || t('publishPage.assistant') }}</span>
                <span class="publish-bubble-time">{{ fmtTime(msg.created_at) }}</span>
              </div>
              <div class="publish-bubble-body" v-html="renderMarkdown(msg.content)" />
            </div>

            <div v-if="streamingText" class="publish-bubble assistant streaming">
              <div class="publish-bubble-meta">
                <span class="publish-bubble-dot" />
                <span>{{ session.agent?.display_name || t('publishPage.assistant') }}</span>
                <span class="publish-typing">{{ t('publishPage.typing') }}</span>
              </div>
              <div class="publish-bubble-body" v-html="renderMarkdown(streamingText)" />
              <span class="publish-cursor" />
            </div>
          </div>

          <form class="publish-composer" @submit.prevent="submit">
            <div class="publish-composer-box">
              <textarea
                v-model="draft"
                :placeholder="t('publishPage.placeholder')"
                :disabled="sending || running"
                rows="1"
                @keydown.enter.exact.prevent="submit"
                @keydown.enter.exact.meta="submit"
                @input="autosize"
                ref="composerEl"
              />
            </div>
            <button
              v-if="running"
              type="button"
              class="publish-abort"
              :disabled="sending"
              @click="abort"
              :title="t('publishPage.abort')"
            >
              <Square :size="14" />
              <span>{{ t('publishPage.abort') }}</span>
            </button>
            <button
              v-else
              type="submit"
              class="publish-send"
              :disabled="sending || running || !draft.trim()"
              :title="t('publishPage.send')"
            >
              <Send :size="15" />
              <span>{{ sending ? t('publishPage.sending') : t('publishPage.send') }}</span>
            </button>
          </form>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Bot, Send, Square, Sparkles, Loader2, Zap, AlertTriangle, X } from 'lucide-vue-next'
import { publishApi } from '@/api/publish'
import { renderMarkdown } from '@/utils/markdown'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const publishId = computed(() => route.params.publishId as string)

const loading = ref(true)
const error = ref<string | null>(null)
const session = ref<Awaited<ReturnType<typeof publishApi.getPage>> | null>(null)
const messages = ref<Awaited<ReturnType<typeof publishApi.getSessionMessages>>>([])
const draft = ref('')
const sending = ref(false)
const running = ref(false)
const streamingText = ref('')
const scrollEl = ref<HTMLElement | null>(null)
const composerEl = ref<HTMLTextAreaElement | null>(null)
// Guard against double-submits while a run is in flight (the 409 race guard
// also covers it server-side).
let inflight = false

const token = computed(() => session.value?.token ?? '')

// Close the floating chat window. Back to where the user came from (usually
// the publishes management page); a direct visit/refresh leaves no in-app
// history, so router.back() would exit the app entirely — fall back to /app.
function close() {
  if (router.options.history.state.back !== null) {
    router.back()
  } else {
    router.push('/app')
  }
}

function fmtTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function autosize() {
  void nextTick(() => {
    const el = composerEl.value
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  })
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const s = await publishApi.getPage(publishId.value)
    session.value = s
    const msgs = await publishApi.getSessionMessages(publishId.value, s.token)
    messages.value = msgs
  } catch (e) {
    error.value = e instanceof Error
      ? (e.message || t('publishPage.loadFailed'))
      : t('publishPage.loadFailed')
  } finally {
    loading.value = false
  }
}

async function submit() {
  const content = draft.value.trim()
  if (!content || inflight || !session.value) return
  inflight = true
  sending.value = true
  const body = content
  draft.value = ''
  autosize()
  messages.value.push({
    message_id: `tmp-${Date.now()}`,
    role: 'user',
    content: body,
    agent_slug: null,
    model_name: null,
    tool_calls: [],
    images: null,
    attachments: null,
    interrupted: false,
    error: false,
    sequence_num: 0,
    created_at: new Date().toISOString(),
  })
  running.value = true
  streamingText.value = ''
  scrollToBottom()
  try {
    const text = await publishApi.runSession(
      publishId.value,
      token.value,
      body,
      (delta) => { streamingText.value += delta },
    )
    if (text) {
      messages.value.push({
        message_id: `tmp-a-${Date.now()}`,
        role: 'assistant',
        content: text,
        agent_slug: session.value.agent?.slug ?? null,
        model_name: null,
        tool_calls: [],
        images: null,
        attachments: null,
        interrupted: false,
        error: false,
        sequence_num: 0,
        created_at: new Date().toISOString(),
      })
    }
  } catch (e) {
    messages.value.push({
      message_id: `err-${Date.now()}`,
      role: 'assistant',
      content: e instanceof Error ? e.message : t('publishPage.runFailed'),
      agent_slug: null,
      model_name: null,
      tool_calls: [],
      images: null,
      attachments: null,
      interrupted: false,
      error: true,
      sequence_num: 0,
      created_at: new Date().toISOString(),
    })
  } finally {
    running.value = false
    sending.value = false
    inflight = false
    streamingText.value = ''
    scrollToBottom()
  }
}

function abort() {
  // The run call is a single fetch that resolves when the SSE closes; a
  // signal-based abort needs an AbortController. Fire the dedicated abort
  // endpoint, which stops the server turn; the stream then unwinds.
  void publishApi.abortSession(publishId.value, token.value).catch(() => undefined)
}

function scrollToBottom() {
  void nextTick(() => {
    if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  })
}

watch(streamingText, scrollToBottom)
watch(messages, scrollToBottom, { deep: true })

onMounted(load)
onUnmounted(() => { inflight = false })
</script>

<style scoped>
/* Full-viewport flexbox that centers the chat window — the page itself is
   just a stage; the dialog card below carries the "this is a chat box" look. */
.publish-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  position: relative;
}

/* The floating dialog card: solid opaque body, strong border, deep shadow,
   a gradient top edge and an inner chat area — reads as a window, not as
   page content laid on the background. */
.publish-window {
  position: relative;
  width: min(880px, calc(100vw - 32px));
  height: min(720px, calc(100vh - 48px));
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-strong);
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.55), 0 0 40px rgba(107, 159, 255, 0.06);
}

/* Gradient top edge echoing the composer's glow line */
.publish-window::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent 10%, rgba(107, 159, 255, 0.35) 50%, transparent 90%);
  z-index: 2;
}

/* Ambient glow behind the header, echoing the app's aurora background */
.publish-glow {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 640px;
  height: 480px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(107, 159, 255, 0.12) 0%, rgba(139, 92, 246, 0.06) 45%, transparent 70%);
  pointer-events: none;
  filter: blur(6px);
  z-index: 0;
}

/* All three regions sit inside the window, so pointer events pass through
   the glow. */
.publish-window > * {
  position: relative;
  z-index: 1;
}

.publish-state {
  display: flex;
  align-items: center;
  justify-content: center;
}

.publish-spinner {
  color: var(--accent);
}

.spin {
  animation: publish-spin 0.9s linear infinite;
}

@keyframes publish-spin {
  to { transform: rotate(360deg); }
}

.publish-error-card {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--color-error);
  background: rgba(248, 113, 113, 0.08);
  border: 1px solid rgba(248, 113, 113, 0.25);
  border-radius: 10px;
  padding: 14px 18px;
  font-size: 13.5px;
}
.publish-error-card p { margin: 0; }

.publish-header {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 16px 18px;
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 12px));
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.publish-agent-avatar {
  flex: none;
  width: 46px;
  height: 46px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-gradient);
  color: #fff;
  box-shadow: var(--accent-glow);
}

.publish-header-info {
  flex: 1;
  min-width: 0;
}

.publish-header-eyebrow {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--text-muted);
}

.publish-title {
  margin: 3px 0 0;
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.02em;
  overflow-wrap: anywhere;
}

.publish-desc {
  margin: 6px 0 0;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.publish-badge {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--accent);
  border: 1px solid rgba(107, 159, 255, 0.35);
  background: var(--accent-dim);
  border-radius: 999px;
  padding: 4px 11px;
  white-space: nowrap;
  margin-top: 2px;
}

/* Close button in the header bar — same visual as the modal close. */
.publish-close {
  flex: none;
  align-self: flex-start;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.publish-close:hover {
  color: var(--text-primary);
  background: var(--bg-tertiary);
  border-color: var(--border-strong);
}

.publish-chat {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.publish-messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* Welcome / empty state */
.publish-welcome {
  margin: auto;
  text-align: center;
  padding: 32px 16px;
  max-width: 360px;
}

.publish-welcome-icon {
  width: 48px;
  height: 48px;
  border-radius: 14px;
  margin: 0 auto 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-dim);
  color: var(--accent);
}

.publish-welcome-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.publish-welcome-hint {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
  line-height: 1.6;
}

.publish-bubble {
  max-width: 84%;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.publish-bubble-meta {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  color: var(--text-muted);
  margin-bottom: 6px;
  background: rgba(255, 255, 255, 0.04);
  padding: 2px 8px;
  border-radius: 4px;
  align-self: flex-start;
}

.publish-bubble-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-gradient);
  box-shadow: 0 0 6px rgba(107, 159, 255, 0.4);
  flex-shrink: 0;
}

.publish-bubble-time {
  opacity: 0.7;
}

.publish-bubble-body {
  border-radius: 12px;
  padding: 11px 15px;
  font-size: 14px;
  line-height: 1.65;
  word-break: break-word;
  overflow-wrap: break-word;
}

.publish-bubble.user {
  align-self: flex-end;
  align-items: flex-end;
}

.publish-bubble.user .publish-bubble-body {
  background: linear-gradient(135deg, #1a3154 0%, #1d3d68 50%, #1e3a5f 100%);
  color: var(--text-primary);
  border: 1px solid rgba(107, 159, 255, 0.18);
  box-shadow: 0 2px 16px rgba(107, 159, 255, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

.publish-bubble.assistant {
  align-self: flex-start;
}

.publish-bubble.assistant .publish-bubble-body {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  border: 1px solid var(--border);
  position: relative;
}

.publish-bubble.assistant .publish-bubble-body::before {
  content: '';
  position: absolute;
  top: 0;
  left: 10px;
  right: 10px;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06), transparent);
}

.publish-bubble.streaming {
  opacity: 0.9;
}

.publish-bubble.error .publish-bubble-body {
  background: rgba(220, 53, 69, 0.1);
  color: var(--color-error);
  border: 1px solid rgba(220, 53, 69, 0.35);
}

.publish-typing {
  color: var(--accent);
}

.publish-cursor {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin: 2px 0 0 2px;
  background: var(--accent);
  border-radius: 1px;
  animation: publish-blink 1s steps(2, start) infinite;
  vertical-align: text-bottom;
}

@keyframes publish-blink {
  to { visibility: hidden; }
}

/* Markdown content inside bubbles */
.publish-bubble-body :deep(p) { margin: 0 0 8px; }
.publish-bubble-body :deep(p:last-child) { margin-bottom: 0; }
.publish-bubble-body :deep(pre) {
  background: var(--bg-primary);
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  padding: 12px 14px;
  overflow-x: auto;
  margin: 8px 0;
}
.publish-bubble-body :deep(code) {
  font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
}
.publish-bubble-body :deep(p code),
.publish-bubble-body :deep(li code) {
  background: rgba(255, 255, 255, 0.07);
  padding: 1px 5px;
  border-radius: 4px;
  color: #a8d8f0;
}
.publish-bubble-body :deep(pre code) {
  background: transparent;
  padding: 0;
  color: #cdd6f4;
}
.publish-bubble-body :deep(ul),
.publish-bubble-body :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}
.publish-bubble-body :deep(a) {
  color: var(--accent);
  text-decoration: none;
}
.publish-bubble-body :deep(a:hover) { text-decoration: underline; }
.publish-bubble-body :deep(blockquote) {
  border-left: 3px solid var(--border-strong);
  margin: 8px 0;
  padding-left: 12px;
  color: var(--text-secondary);
}
.publish-bubble-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.publish-bubble-body :deep(th),
.publish-bubble-body :deep(td) {
  border: 1px solid var(--border-strong);
  padding: 5px 10px;
  font-size: 13px;
}

.publish-composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 12px 14px;
  border-top: 1px solid var(--border);
  background: var(--bg-secondary);
}

.publish-composer-box {
  flex: 1;
  display: flex;
  align-items: flex-end;
  background: var(--bg-primary);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  padding: 9px 12px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.publish-composer-box:focus-within {
  border-color: rgba(107, 159, 255, 0.55);
  box-shadow: 0 0 0 3px rgba(107, 159, 255, 0.12);
}

.publish-composer textarea {
  flex: 1;
  resize: none;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.5;
  font-family: inherit;
  max-height: 160px;
  padding: 0;
}
.publish-composer textarea::placeholder { color: var(--text-muted); }
.publish-composer textarea:disabled { opacity: 0.6; }

.publish-send {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 40px;
  padding: 0 18px;
  border: none;
  border-radius: 10px;
  background: var(--accent-gradient);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: -0.01em;
  transition: background 0.2s, transform 0.1s, box-shadow 0.2s;
  box-shadow: 0 2px 12px rgba(107, 159, 255, 0.22);
}
.publish-send:hover:not(:disabled) {
  background: var(--accent-gradient-hover);
  transform: translateY(-1px);
  box-shadow: var(--accent-glow);
}
.publish-send:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none;
}

.publish-abort {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 40px;
  padding: 0 16px;
  border: 1px solid rgba(248, 113, 113, 0.4);
  border-radius: 10px;
  background: rgba(248, 113, 113, 0.1);
  color: var(--color-error);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.publish-abort:hover:not(:disabled) {
  background: rgba(248, 113, 113, 0.18);
  border-color: rgba(248, 113, 113, 0.6);
}
.publish-abort:disabled { opacity: 0.4; cursor: not-allowed; }

@media (max-width: 640px) {
  .publish-page { padding: 8px; }
  .publish-window {
    width: calc(100vw - 16px);
    height: calc(100dvh - 16px);
    border-radius: 12px;
  }
  .publish-header { padding: 12px 12px; }
  .publish-badge { display: none; }
  .publish-messages { padding: 14px; }
}
</style>
