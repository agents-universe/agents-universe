<template>
  <div class="publish-page">
    <div v-if="loading" class="publish-state">
      <div class="thinking-indicator">
        <span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" />
      </div>
    </div>

    <div v-else-if="error" class="publish-state publish-error">
      <p>{{ error }}</p>
    </div>

    <template v-else-if="session">
      <header class="publish-header">
        <div class="publish-header-info">
          <span class="publish-header-eyebrow">{{ t('publishPage.embeddedTitle') }}</span>
          <h1 class="publish-title">{{ session.title || session.agent?.display_name || session.agent?.slug || t('publishPage.untitled') }}</h1>
          <p v-if="session.description" class="publish-desc">{{ session.description }}</p>
        </div>
        <span class="publish-badge" title="模型由发布者绑定">{{ t('publishPage.publisherModelBadge') }}</span>
      </header>

      <div class="publish-chat">
        <!-- History -->
        <div class="publish-messages" ref="scrollEl">
          <div
            v-for="msg in messages"
            :key="msg.message_id"
            class="publish-bubble"
            :class="msg.role === 'user' ? 'user' : 'assistant'"
          >
            <div v-html="renderMarkdown(msg.content)" />
          </div>
          <div v-if="streamingText" class="publish-bubble assistant streaming">
            <div v-html="renderMarkdown(streamingText)" />
          </div>
        </div>

        <form class="publish-composer" @submit.prevent="submit">
          <textarea
            v-model="draft"
            :placeholder="t('publishPage.placeholder')"
            :disabled="sending || running"
            rows="2"
            @keydown.enter.exact.prevent="submit"
            @keydown.enter.exact.meta="submit"
          />
          <button
            type="submit"
            class="btn-primary publish-send"
            :disabled="sending || running || !draft.trim()"
          >
            {{ running ? t('publishPage.running') : sending ? t('publishPage.sending') : t('publishPage.send') }}
          </button>
          <button
            v-if="running"
            type="button"
            class="btn-sm publish-abort"
            :disabled="sending"
            @click="abort"
          >{{ t('publishPage.abort') }}</button>
        </form>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { publishApi } from '@/api/publish'
import { renderMarkdown } from '@/utils/markdown'

const route = useRoute()
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
// Guard against double-submits while a run is in flight (the 409 race guard
// also covers it server-side).
let inflight = false

const token = computed(() => session.value?.token ?? '')

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
.publish-page {
  max-width: 860px;
  margin: 0 auto;
  padding: 24px 16px 32px;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.publish-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.publish-error {
  color: var(--color-error, #f38ba8);
}

.publish-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border-color, #2a2a3a);
  padding-bottom: 16px;
}

.publish-header-eyebrow {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted, #8a8a9a);
}

.publish-title {
  margin: 4px 0 0;
  font-size: 22px;
  font-weight: 650;
}

.publish-desc {
  margin: 6px 0 0;
  color: var(--text-secondary, #a0a0b0);
  font-size: 13px;
  line-height: 1.6;
}

.publish-badge {
  flex: none;
  font-size: 11px;
  color: var(--accent, #7aa2f7);
  border: 1px solid var(--accent, #7aa2f7);
  border-radius: 999px;
  padding: 3px 10px;
  white-space: nowrap;
}

.publish-chat {
  flex: 1;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-color, #2a2a3a);
  border-radius: 10px;
  overflow: hidden;
}

.publish-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 200px;
}

.publish-bubble {
  max-width: 82%;
  padding: 9px 12px;
  border-radius: 10px;
  font-size: 13.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.publish-bubble.user {
  align-self: flex-end;
  background: var(--accent, #7aa2f7);
  color: #fff;
}

.publish-bubble.assistant {
  align-self: flex-start;
  background: var(--bg-elevated, #1e1e2e);
  border: 1px solid var(--border-color, #2a2a3a);
}

.publish-bubble.streaming {
  opacity: 0.85;
}

.publish-composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--border-color, #2a2a3a);
  background: var(--bg-elevated, #1e1e2e);
}

.publish-composer textarea {
  flex: 1;
  resize: none;
  background: var(--bg-base, #16161e);
  color: var(--text-primary, #e0e0ea);
  border: 1px solid var(--border-color, #2a2a3a);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13.5px;
  font-family: inherit;
}

.publish-send {
  flex: none;
}

.publish-abort {
  flex: none;
}
</style>
