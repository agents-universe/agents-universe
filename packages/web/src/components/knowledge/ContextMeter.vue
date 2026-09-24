<template>
  <div class="context-meter">
    <div class="context-meter-header">
      <span class="context-meter-label">
        <Gauge :size="11" />
        {{ t('contextMeter.label') }}
      </span>
      <span class="context-meter-fraction">{{ occupancy.toLocaleString() }} / {{ limit.toLocaleString() }}</span>
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

// Meter shows current context occupancy vs the provider window (falls back
// to the conversation budget before the first turn reports the real window).
// tokensUsed/tokenBudget are the billing ledger and are intentionally not
// read here — a cumulative counter overflows any window almost immediately.
const occupancy = computed(() => convStore.contextTokens)
const limit = computed(() => convStore.contextWindow ?? convStore.tokenBudget)
const pct = computed(() => limit.value ? (occupancy.value / limit.value) * 100 : 0)

const fillClass = computed(() => {
  if (pct.value > 90) return 'fill-red'
  if (pct.value > 75) return 'fill-amber'
  return 'fill-blue'
})
</script>
