<template>
  <div class="message" :class="['message-' + message.role, { 'message-error': message.isError, 'has-diagram': mermaidBlocks.length > 0 }]">
    <!-- Role label -->
    <div class="message-meta">
      <span v-if="message.role === 'assistant'" class="message-avatar-dot" />
      <span class="message-role">{{ roleLabel }}</span>
      <span
        v-if="collabAgentLabel"
        class="collab-agent-badge"
        :title="collabBadgeTitle"
      >{{ collabAgentLabel }}</span>
      <!-- The model that actually produced this reply (auto routing resolves
      one per turn; explicit selection stores the chosen model id). -->
      <span
        v-if="message.modelName"
        class="model-name-badge"
        :title="t('messageBubble.modelUsed')"
      >{{ message.modelName }}</span>
      <span
        v-if="message.modelTier"
        class="model-tier-badge"
        :title="t('messageBubble.modelTierTitle')"
      >{{ message.modelTier }}</span>
      <span v-if="message.interrupted" class="interrupted-badge" :title="t('messageBubble.interruptedTitle')">{{ t('messageBubble.interrupted') }}</span>
      <!-- new Date(ts).toISOString() threw RangeError on invalid/
      missing timestamps (locally recovered messages can lack one) and broke
      the whole message tree. relativeTime already guards null/invalid. -->
      <span class="message-time">{{ relativeTime(message.timestamp) }}</span>
    </div>

    <!-- Tool calls leading up to the plan (including plan_task itself) -->
    <ToolCallCard
      v-for="tc in callsBeforePlan"
      :key="tc.callId"
      :call="tc"
    />

    <!-- Task plan — rendered right after the plan_task call that created it -->
    <TaskPlanCard
      v-if="historicTasks.length > 0"
      :tasks="historicTasks"
      :tool-calls="nonPlanToolCalls"
    />

    <!-- Tool calls after the plan -->
    <ToolCallCard
      v-for="tc in callsAfterPlan"
      :key="tc.callId"
      :call="tc"
    />

    <!-- Main content -->
    <div v-if="message.content" class="message-content" @click="handleContentClick">
      <div v-html="renderedContent" />
    </div>

    <!-- Mermaid diagrams — rendered after mount -->
    <MermaidBlock
      v-for="(code, i) in mermaidBlocks"
      :key="i"
      :code="code"
    />

    <!-- Images -->
    <div v-if="message.images?.length" class="message-images">
      <div
        v-for="img in message.images"
        :key="img.id"
        class="message-image-wrap"
        @click="openImage(img)"
      >
        <img
          :src="withApi(img.url)"
          :alt="img.alt"
          class="message-image"
          @error="($event.target as HTMLImageElement).classList.add('image-broken')"
        />
      </div>
    </div>

    <!-- Attachments (non-image files) — click downloads (same-origin cookie auth) -->
    <div v-if="message.attachments?.length" class="message-attachments">
      <a
        v-for="att in message.attachments"
        :key="att.id"
        class="attachment-chip"
        :href="withApi(att.url)"
        :download="att.name"
        rel="noopener"
        :title="att.name"
      >
        <Download :size="14" />
        <span class="attachment-name">{{ att.name }}</span>
        <span class="attachment-size">{{ formatSize(att.size) }}</span>
      </a>
    </div>

    <!-- Knowledge loaded -->
    <div v-if="message.knowledgeLoaded?.length" class="knowledge-loaded-bar">
      <span class="knowledge-loaded-label">{{ t('messageBubble.knowledgeLoaded') }}</span>
      <span v-for="slug in message.knowledgeLoaded" :key="slug" class="knowledge-slug-chip">{{ slug }}</span>
    </div>
  </div>

  <!-- Image lightbox -->
  <Teleport to="body">
    <div v-if="lightboxImg" class="mermaid-modal-overlay" @click.self="closeLightbox">
      <div class="mermaid-modal-content" @wheel.prevent="onWheel" @mousedown="startDrag">
        <div ref="lightboxEl" class="mermaid-modal-svg">
          <img :src="withApi(lightboxImg.url)" :alt="lightboxImg.alt" style="display:block;max-width:none;max-height:none;" />
        </div>
      </div>
      <div class="mermaid-modal-controls">
        <a
          v-if="lightboxImg"
          class="lightbox-download"
          :href="withApi(lightboxImg.url)"
          :download="lightboxImg.alt || 'image'"
        >{{ t('messageBubble.download') }}</a>
        <button @click="zoomIn">+</button>
        <button @click="resetZoom">1:1</button>
        <button @click="zoomOut">−</button>
        <button @click="closeLightbox">✕</button>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Download } from 'lucide-vue-next'
import { renderMarkdown } from '@/utils/markdown'
import { relativeTime } from '@/utils/time'
import { withApi } from '@/utils/basePath'
import { useAgentStore } from '@/stores/agent'
import type { Message, ImageRecord, AgentTask, ToolCallRecord } from '@/types'
import ToolCallCard from './ToolCallCard.vue'
import TaskPlanCard from './TaskPlanCard.vue'
import MermaidBlock from './MermaidBlock.vue'

const props = defineProps<{ message: Message }>()

const { t } = useI18n()
const agentStore = useAgentStore()

// Badge label for an @-mention turn involving a different agent than the
// conversation's current agent. Assistants carry "answered by X", users carry
// "sent to X"; default turns stay unbadged on both sides.
const collabAgentLabel = computed(() => {
  const slug = props.message.agentSlug
  if (!slug || slug === agentStore.currentAgent?.slug) return null
  const label = agentStore.agents.find((a) => a.slug === slug)?.label ?? slug
  return props.message.role === 'assistant'
    ? `🤖 ${label}`
    : `${t('messageBubble.sentTo')} ${label}`
})

const collabBadgeTitle = computed(() => {
  const slug = props.message.agentSlug
  if (!slug) return ''
  const label = agentStore.agents.find((a) => a.slug === slug)?.label ?? slug
  return props.message.role === 'assistant'
    ? t('messageBubble.answeredByTitle', { label })
    : t('messageBubble.sentToTitle', { label })
})

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const lightboxImg = ref<ImageRecord | null>(null)
const lightboxEl = ref<HTMLElement | null>(null)

let scale = 1.5
let tx = 0
let ty = 0
let dragging = false
let dragStart = { x: 0, y: 0 }

// Split tool calls around the first plan_task call so the plan card renders
// right after the call that created it (chronological order). Messages without
// a plan_task call keep callsBeforePlan = all calls, order unchanged.
const firstPlanIndex = computed(() =>
  (props.message.toolCalls ?? []).findIndex((tc) => tc.tool === 'plan_task'),
)

const callsBeforePlan = computed<ToolCallRecord[]>(() => {
  const calls = props.message.toolCalls ?? []
  const i = firstPlanIndex.value
  return i === -1 ? calls : calls.slice(0, i + 1) // includes plan_task itself
})

const callsAfterPlan = computed<ToolCallRecord[]>(() => {
  const calls = props.message.toolCalls ?? []
  const i = firstPlanIndex.value
  return i === -1 ? [] : calls.slice(i + 1)
})

const nonPlanToolCalls = computed<ToolCallRecord[]>(() =>
  (props.message.toolCalls ?? []).filter((tc) => tc.tool !== 'plan_task'),
)

const historicTasks = computed<AgentTask[]>(() => {
  const planCall = (props.message.toolCalls ?? []).find((tc) => tc.tool === 'plan_task')
  if (!planCall?.output) return []
  const rawTasks = (planCall.output as Record<string, unknown>).tasks
  if (!Array.isArray(rawTasks)) return []
  return rawTasks.map((t: Record<string, unknown>) => {
    const status = (t.status as AgentTask['status']) ?? 'completed'
    return {
      id: String(t.id ?? t.task_id ?? ''),
      title: String(t.title ?? ''),
      // A settled message can't have a live running task — a straggler here
      // means the run was interrupted, so show it as pending.
      status: status === 'running' ? 'pending' : status,
      modelTier: t.estimated_complexity ? String(t.estimated_complexity) : undefined,
      summary: t.summary ? String(t.summary) : undefined,
      error: t.error ? String(t.error) : undefined,
      dependsOn: Array.isArray(t.depends_on) ? (t.depends_on as string[]) : undefined,
    }
  })
})

const roleLabel = computed(() => {
  if (props.message.role === 'user') return t('messageBubble.roleUser')
  if (props.message.role === 'assistant') return t('messageBubble.roleAssistant')
  return t('messageBubble.roleTool')
})

const renderedContent = computed(() => {
  // Strip mermaid blocks from inline HTML — they're rendered by MermaidBlock
  const html = renderMarkdown(props.message.content)
  return html.replace(/<pre class="mermaid-block"[^>]*><\/pre>/g, '')
})

const mermaidBlocks = computed(() => {
  // (?:```|$) — an aborted stream can end INSIDE the mermaid fence; the
  // unclosed block is stripped from renderedContent above and must still be
  // extracted here, or the diagram silently vanishes from the message.
  const matches = [...props.message.content.matchAll(/```\s*mermaid\s*\r?\n([\s\S]*?)(?:```|$)/gi)]
  return matches.map((m) => m[1].trim())
})

function handleContentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (target.tagName === 'A' && target.classList.contains('knowledge-link')) {
    e.preventDefault()
  }
}

function openImage(img: ImageRecord) {
  lightboxImg.value = img
  scale = 1.5
  tx = 0
  ty = 0
  // apply after DOM updates
  requestAnimationFrame(() => applyTransform())
}

function closeLightbox() {
  lightboxImg.value = null
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', stopDrag)
}

function applyTransform() {
  if (lightboxEl.value) {
    lightboxEl.value.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`
  }
}

function onWheel(e: WheelEvent) {
  scale = Math.min(8, Math.max(0.25, scale - e.deltaY * 0.001))
  applyTransform()
}

function zoomIn() {
  scale = Math.min(8, scale * 1.3)
  applyTransform()
}

function zoomOut() {
  scale = Math.max(0.25, scale / 1.3)
  applyTransform()
}

function resetZoom() {
  scale = 1
  tx = 0
  ty = 0
  applyTransform()
}

function startDrag(e: MouseEvent) {
  dragging = true
  dragStart = { x: e.clientX - tx, y: e.clientY - ty }
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', stopDrag)
}

function onDrag(e: MouseEvent) {
  if (!dragging) return
  tx = e.clientX - dragStart.x
  ty = e.clientY - dragStart.y
  applyTransform()
}

function stopDrag() {
  dragging = false
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', stopDrag)
}

// If the component unmounts mid-drag (e.g. conversation switch), the window
// listeners would otherwise stay registered forever, keeping a dead handler
// alive and mutating detached state on every mouse move.
onUnmounted(() => {
  dragging = false
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', stopDrag)
})
</script>
