<template>
  <div class="tool-call-card" :class="'tool-call-' + call.status">
    <div class="tool-call-header" @click="expanded = !expanded">
      <span class="tool-call-status-icon" :class="'status-' + call.status">{{ statusIcon }}</span>
      <template v-if="delegation">
        <span class="tool-call-name">{{ t('toolCall.delegateTo', { agent: delegation.agent }) }}</span>
        <span
          v-if="delegation.status"
          class="tool-call-badge"
          :class="'badge-' + delegation.status"
        >{{ delegation.statusLabel }}</span>
      </template>
      <template v-else-if="mcpServer">
        <span class="mcp-badge">MCP</span>
        <span class="tool-call-name">{{ mcpToolName }}</span>
        <span class="mcp-server-tag">{{ mcpServer }}</span>
      </template>
      <span v-else class="tool-call-name">{{ call.tool }}</span>
      <ChevronDown v-if="!expanded" :size="12" />
      <ChevronUp v-else :size="12" />
    </div>

    <!-- A delegation's brief and reason are prose meant to be read, so they sit
         outside the collapsed JSON view: what was handed over, and why. -->
    <div v-if="delegation" class="tool-call-body tool-call-delegation">
      <div v-if="delegation.reason" class="tool-call-section">
        <span class="tool-call-label">{{ t('toolCall.delegateReason') }}</span>
        <div class="tool-call-prose">{{ delegation.reason }}</div>
      </div>
      <div v-if="delegation.brief" class="tool-call-section">
        <span class="tool-call-label">{{ t('toolCall.delegateBrief') }}</span>
        <div class="tool-call-prose">{{ delegation.brief }}</div>
      </div>
      <div v-if="delegation.summary" class="tool-call-section">
        <span class="tool-call-label">{{ t('toolCall.delegateSummary') }}</span>
        <div class="tool-call-prose">{{ delegation.summary }}</div>
      </div>
      <div v-else-if="delegation.error" class="tool-call-section">
        <div class="tool-call-prose delegation-error">{{ delegation.error }}</div>
      </div>
      <div v-else-if="delegation.status && delegation.status !== 'ok'" class="tool-call-section">
        <div class="tool-call-prose">{{ t('toolCall.delegateCancelled') }}</div>
      </div>
      <div v-if="delegation.meta" class="tool-call-meta">{{ delegation.meta }}</div>
    </div>

    <div v-if="expanded" class="tool-call-body">
      <div class="tool-call-section">
        <span class="tool-call-label">{{ t('toolCall.input') }}</span>
        <pre class="tool-call-json">{{ JSON.stringify(call.input, null, 2) }}</pre>
      </div>
      <div v-if="call.output" class="tool-call-section">
        <span class="tool-call-label">{{ t('toolCall.output') }}</span>
        <pre class="tool-call-json">{{ JSON.stringify(call.output, null, 2) }}</pre>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import type { ToolCallRecord } from '@/types'

const { t } = useI18n()
const props = defineProps<{ call: ToolCallRecord; defaultExpanded?: boolean }>()
const expanded = ref(props.defaultExpanded ?? false)

// Auto-collapse when tool call finishes (running → done/error)
watch(() => props.call.status, (status) => {
  if (status === 'done' || status === 'error' || status === 'interrupted') {
    expanded.value = false
  }
})

const statusIcon = computed(() => {
  switch (props.call.status) {
    case 'preparing': return '◌'
    case 'running': return '⟳'
    case 'done': return '✓'
    case 'error': return '✗'
    case 'interrupted': return '⚡'
    default: return '○'
  }
})

/** Parse mcp__<server>__<tool> names; null when not an MCP tool. */
const mcpServer = computed(() => {
  const parts = props.call.tool.split('__')
  if (parts.length >= 3 && parts[0] === 'mcp') return parts[1]
  return null
})

const mcpToolName = computed(() => {
  const parts = props.call.tool.split('__')
  if (parts.length >= 3 && parts[0] === 'mcp') return parts.slice(2).join('__')
  return props.call.tool
})

/**
 * Delegation card, when this call is `delegate_agent`.
 *
 * Everything shown comes from the tool call itself: the header from the
 * start event's input, the outcome from the end event's output (the result
 * dict `run_delegated_turn` returns). No new event type is involved.
 */
const delegation = computed(() => {
  if (props.call.tool !== 'delegate_agent') return null
  const input = props.call.input ?? {}
  const output = props.call.output ?? {}
  const agent = str(input.agent) || str(output.agent)
  if (!agent) return null

  const status = str(output.status)
  const meta: string[] = []
  const tokens = Number(output.tokens_used)
  if (tokens > 0) meta.push(t('toolCall.delegateTokens', { count: tokens }))
  const durationMs = Number(output.duration_ms)
  if (durationMs > 0) {
    meta.push(t('toolCall.delegateDuration', { seconds: Math.round(durationMs / 100) / 10 }))
  }

  return {
    // The agent's own name once it has replied; the slug until then.
    agent: str(output.agent_name) || agent,
    reason: str(input.reason),
    brief: str(input.brief),
    summary: str(output.summary),
    error: str(output.error),
    status,
    // A status this build does not know renders raw rather than as a
    // "toolCall.delegateStatus.xyz" path.
    statusLabel: DELEGATE_STATUSES.includes(status)
      ? t(`toolCall.delegateStatus.${status}`)
      : status,
    meta: meta.join(' · '),
  }
})

const DELEGATE_STATUSES: readonly string[] = ['ok', 'timeout', 'error', 'refused', 'aborted']

function str(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}
</script>
