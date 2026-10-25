<template>
  <div v-if="agent" class="agent-capabilities-card">
    <div class="agent-capabilities-label">{{ agentName }}</div>
    <div v-if="agentDescription" class="agent-capabilities-desc">{{ agentDescription }}</div>
    <!-- Agent-authored prompts rendered raw via interpolation — never routed
         through t(), so @ { | in the strings stay inert (see locales.test.ts). -->
    <div v-if="starterPrompts.length" class="agent-capabilities-prompts">
      <div class="agent-capabilities-prompts-label">{{ t('agentCard.starterLabel') }}</div>
      <div class="agent-capabilities-chips">
        <button
          v-for="(prompt, i) in starterPrompts"
          :key="i"
          type="button"
          class="agent-capabilities-chip"
          @click="emit('apply-prompt', prompt)"
        >
          {{ prompt }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { resolveLocalized, localizedText } from '@/utils/localize'
import type { AgentInfo } from '@/types'

const props = defineProps<{ agent: AgentInfo | null }>()
const emit = defineEmits<{ 'apply-prompt': [prompt: string] }>()

const { t } = useI18n()

const agentName = computed(() =>
  props.agent ? localizedText(props.agent.label, props.agent.i18n?.display_name) : '',
)
const agentDescription = computed(() =>
  props.agent ? localizedText(props.agent.description, props.agent.i18n?.description) : '',
)
const starterPrompts = computed(() => {
  const prompts = props.agent?.starter_prompts
  if (!prompts?.length) return []
  return resolveLocalized(prompts, props.agent?.i18n?.starter_prompts) ?? prompts
})
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

.agent-capabilities-prompts {
  margin-top: 12px;
}

.agent-capabilities-prompts-label {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.agent-capabilities-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.agent-capabilities-chip {
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  padding: 4px 10px;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}

.agent-capabilities-chip:hover {
  border-color: var(--accent);
  color: var(--text-primary);
}
</style>
