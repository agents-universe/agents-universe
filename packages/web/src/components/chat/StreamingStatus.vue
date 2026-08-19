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
import { useI18n } from 'vue-i18n'
import { useConversationStore } from '@/stores/conversation'
import { useAgentStore } from '@/stores/agent'

const props = defineProps<{ hideStepInfo?: boolean }>()

const { t } = useI18n()
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
    return t('streamingStatus.waitingInput', { title: prompt.title || prompt.question })
  }
  if (collabLabel.value) {
    if (runningTools.value.length === 0) {
      return convStore.streamingContent
        ? t('streamingStatus.collabOutput', { name: collabLabel.value })
        : t('streamingStatus.collabCalling', { name: collabLabel.value })
    }
    if (runningTools.value.length === 1) {
      return t('streamingStatus.collabCallingTool', { name: collabLabel.value, tool: runningTools.value[0].tool })
    }
    return t('streamingStatus.collabCallingTools', { name: collabLabel.value, count: runningTools.value.length })
  }
  if (runningTools.value.length === 0) {
    return convStore.streamingContent ? t('streamingStatus.output') : t('streamingStatus.thinking')
  }
  if (runningTools.value.length === 1) {
    return t('streamingStatus.callingTool', { tool: runningTools.value[0].tool })
  }
  return t('streamingStatus.callingTools', { count: runningTools.value.length })
})

const stepInfo = computed(() => {
  if (runningTasks.value.length > 1) {
    return t('streamingStatus.tasksParallel', { count: runningTasks.value.length })
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
    const next = task.nextStep ? t('streamingStatus.nextStep', { step: task.nextStep }) : ''
    return `${task.currentStep}${progress}${next}`
  }
  return null
})
</script>
