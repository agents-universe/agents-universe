<template>
  <div v-if="agent" class="agent-capabilities-card">
    <div class="agent-capabilities-label">{{ agent.label }}</div>
    <div v-if="agent.description" class="agent-capabilities-desc">{{ agent.description }}</div>
  </div>
</template>

<script setup lang="ts">
import type { AgentInfo } from '@/types'

defineProps<{ agent: AgentInfo | null }>()
</script>

<style scoped>
.agent-capabilities-card {
  max-width: min(460px, 100%);
  width: 100%;
  text-align: left;
}

.agent-capabilities-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.agent-capabilities-desc {
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-top: 4px;
}
</style>
