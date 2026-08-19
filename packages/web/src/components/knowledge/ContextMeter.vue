<template>
  <div class="context-meter">
    <div class="context-meter-header">
      <span class="context-meter-label">
        <Gauge :size="11" />
        {{ t('contextMeter.label') }}
      </span>
      <span class="context-meter-fraction">{{ tokensUsed.toLocaleString() }} / {{ tokenBudget.toLocaleString() }}</span>
    </div>
    <div class="context-meter-track">
      <div
        class="context-meter-fill"
        :style="{ width: `${Math.min(pct, 100)}%` }"
        :class="fillClass"
      />
    </div>
    <div v-if="convStore.contextUsage" class="context-meter-breakdown">
      <span><FileStack :size="10" /> {{ t('contextMeter.staticLabel') }} {{ convStore.contextUsage.staticFiles }}</span>
      <span><Zap :size="10" /> {{ t('contextMeter.dynamicLabel') }} {{ convStore.contextUsage.dynamicFiles }}</span>
      <span><History :size="10" /> {{ t('contextMeter.historyLabel') }} {{ convStore.contextUsage.conversationHistoryTokens }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Gauge, FileStack, Zap, History } from 'lucide-vue-next'
import { useConversationStore } from '@/stores/conversation'

const { t } = useI18n()
const convStore = useConversationStore()

const tokensUsed = computed(() => convStore.tokensUsed)
const tokenBudget = computed(() => convStore.tokenBudget)
const pct = computed(() => tokenBudget.value ? (tokensUsed.value / tokenBudget.value) * 100 : 0)

const fillClass = computed(() => {
  if (pct.value > 90) return 'fill-red'
  if (pct.value > 75) return 'fill-amber'
  return 'fill-blue'
})
</script>
