<template>
  <div class="streaming-status">
    <span class="thinking-indicator"><span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" /></span>
    <span v-if="statusText" class="streaming-label">{{ statusText }}</span>
    <span v-if="stepInfo && !props.hideStepInfo" class="streaming-step">{{ stepInfo }}</span>
    <span v-if="elapsed" class="streaming-elapsed">{{ elapsed }}s</span>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'

const props = defineProps<{ hideStepInfo?: boolean }>()

const convStore = useConversationStore()
const agentStore = useAgentStore()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval>

onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => clearInterval(timer))

const elapsed = computed(() => {
  if (!convStore.streamingStartTime) return null
  return Math.floor((now.value - convStore.streamingStartTime) / 1000)
})

const runningTools = computed(() =>
  convStore.activeToolCalls.filter((tc) => tc.status === 'running'),
)

const runningTasks = computed(() =>
  convStore.tasks.filter((t) => t.status === 'running'),
)

const hasPendingPrompt = computed(() => convStore.pendingPrompts.length > 0)

// Label of the agent routed into this turn via @-mention when it differs from
// the conversation default — makes "which agent is being called" visible while
// streaming, before the finished-message badge in MessageBubble appears.
const collabLabel = computed(() => {
  const slug = convStore.turnAgentSlug
  if (!slug || slug === agentStore.currentAgent?.slug) return null
  return agentStore.agents.find((a) => a.slug === slug)?.label ?? slug
})

const statusText = computed(() => {
  if (hasPendingPrompt.value) {
    const prompt = convStore.pendingPrompts[0]
    return `等待输入：${prompt.title || prompt.question}`
  }
  if (collabLabel.value) {
    if (runningTools.value.length === 0) {
      return convStore.streamingContent
        ? `@${collabLabel.value} 正在输出…`
        : `正在调用 @${collabLabel.value}…`
    }
    if (runningTools.value.length === 1) {
      return `@${collabLabel.value} 正在调用 ${runningTools.value[0].tool}…`
    }
    return `@${collabLabel.value} 正在调用 ${runningTools.value.length} 个工具…`
  }
  if (runningTools.value.length === 0) {
    return convStore.streamingContent ? '正在输出…' : '正在思考…'
  }
  if (runningTools.value.length === 1) {
    return `正在调用 ${runningTools.value[0].tool}…`
  }
  return `正在调用 ${runningTools.value.length} 个工具…`
})

const stepInfo = computed(() => {
  if (runningTasks.value.length > 1) {
    return `${runningTasks.value.length} 个任务并行运行中`
  }
  const tool = runningTools.value[0]
  if (tool?.currentStep) {
    const next = tool.nextStep ? ` -> ${tool.nextStep}` : ''
    return `${tool.currentStep}${next}`
  }
  const task = runningTasks.value[0]
  if (task?.currentStep) {
    const progress = task.progressTotal
      ? ` (${task.progressCompleted ?? 0}/${task.progressTotal})`
      : ''
    const next = task.nextStep ? ` -> 下一步: ${task.nextStep}` : ''
    return `${task.currentStep}${progress}${next}`
  }
  return null
})
</script>
