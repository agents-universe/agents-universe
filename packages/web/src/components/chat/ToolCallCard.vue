<template>
  <div class="tool-call-card" :class="'tool-call-' + call.status">
    <div class="tool-call-header" @click="expanded = !expanded">
      <span class="tool-call-status-icon" :class="'status-' + call.status">{{ statusIcon }}</span>
      <template v-if="mcpServer">
        <span class="mcp-badge">MCP</span>
        <span class="tool-call-name">{{ mcpToolName }}</span>
        <span class="mcp-server-tag">{{ mcpServer }}</span>
      </template>
      <span v-else class="tool-call-name">{{ call.tool }}</span>
      <ChevronDown v-if="!expanded" :size="12" />
      <ChevronUp v-else :size="12" />
    </div>
    <div v-if="expanded" class="tool-call-body">
      <div class="tool-call-section">
        <span class="tool-call-label">输入</span>
        <pre class="tool-call-json">{{ JSON.stringify(call.input, null, 2) }}</pre>
      </div>
      <div v-if="call.output" class="tool-call-section">
        <span class="tool-call-label">输出</span>
        <pre class="tool-call-json">{{ JSON.stringify(call.output, null, 2) }}</pre>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import type { ToolCallRecord } from '@/types'

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
</script>
