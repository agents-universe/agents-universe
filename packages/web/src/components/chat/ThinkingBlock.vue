<template>
  <div class="thinking-block" :class="{ 'thinking-live': live }">
    <div class="thinking-header" @click="expanded = !expanded">
      <span class="thinking-icon" :class="{ 'thinking-icon-live': live }">✻</span>
      <span class="thinking-title">{{ t('thinkingBlock.title') }}</span>
      <span v-if="live" class="thinking-live-tag">{{ t('thinkingBlock.live') }}</span>
      <ChevronDown v-if="!expanded" :size="12" />
      <ChevronUp v-else :size="12" />
    </div>
    <!-- Plain <pre>, never markdown: CoT contains raw braces, HTML-ish
         fragments and code fences that must render verbatim. -->
    <div v-show="expanded" class="thinking-body">
      <pre class="thinking-text">{{ text }}</pre>
    </div>
    <div v-show="!expanded" class="thinking-clamp">{{ text }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'

const props = defineProps<{
  text: string
  /** True while the model is still emitting this trace: the block starts
   *  expanded and auto-collapses the moment live flips false (thinking_end
   *  / first content delta). History blocks pass live=false → collapsed. */
  live?: boolean
}>()

const { t } = useI18n()
const expanded = ref(props.live ?? false)

watch(
  () => props.live,
  (live) => {
    if (live) expanded.value = true
    else expanded.value = false
  },
)
</script>
