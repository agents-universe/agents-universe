<template>
  <div v-if="agent" class="agent-capabilities-card">
    <div class="agent-capabilities-header">
      <div class="agent-avatar" :style="{ background: avatarColor(agent.slug) }">
        {{ initials(agent.label) }}
      </div>
      <div class="agent-capabilities-titles">
        <div class="agent-capabilities-label">{{ agent.label }}</div>
        <div v-if="agent.description" class="agent-capabilities-desc">{{ agent.description }}</div>
      </div>
    </div>

    <div v-if="agent.skills.length" class="agent-tooltip__section">
      <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipSkills') }}</span>
      <div class="agent-tooltip__list">
        <div v-for="s in agent.skills" :key="s.slug" class="agent-tooltip__item">
          <span class="agent-tooltip__slug">{{ s.slug }}</span>
          <span v-if="s.description" class="agent-tooltip__item-desc">{{ s.description }}</span>
        </div>
      </div>
    </div>

    <div v-if="agent.workflows.length" class="agent-tooltip__section">
      <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipWorkflows') }}</span>
      <div class="agent-tooltip__list">
        <div v-for="w in agent.workflows" :key="w.slug" class="agent-tooltip__item">
          <span class="agent-tooltip__slug">{{ w.slug }}</span>
          <span v-if="w.description" class="agent-tooltip__item-desc">{{ w.description }}</span>
        </div>
      </div>
    </div>

    <div v-if="staticTools.length" class="agent-tooltip__section">
      <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipTools') }}</span>
      <div class="agent-tooltip__list">
        <div v-for="t in staticTools" :key="t" class="agent-tooltip__item">
          <span class="agent-tooltip__slug">{{ t }}</span>
        </div>
      </div>
    </div>

    <div v-if="mcpServers.length" class="agent-tooltip__section">
      <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipMcp') }}</span>
      <div class="agent-tooltip__list">
        <div v-for="s in mcpServers" :key="s" class="agent-tooltip__item">
          <span class="mcp-badge">MCP</span>
          <span class="agent-tooltip__slug">{{ s }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { AgentInfo } from '@/types'
import { agentStaticTools, agentMcpServers, initials, avatarColor } from '@/utils/agent'

const props = defineProps<{ agent: AgentInfo | null }>()
const { t } = useI18n()

const staticTools = computed(() => agentStaticTools(props.agent))
const mcpServers = computed(() => agentMcpServers(props.agent))
</script>

<style scoped>
.agent-capabilities-card {
  max-width: min(460px, 100%);
  width: 100%;
  text-align: left;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  padding: 14px 16px;
}

.agent-capabilities-header {
  display: flex;
  gap: 10px;
  align-items: center;
}

.agent-capabilities-header .agent-avatar {
  width: 34px;
  height: 34px;
  font-size: 12px;
  flex-shrink: 0;
}

.agent-capabilities-titles {
  min-width: 0;
}

.agent-capabilities-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.agent-capabilities-desc {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
  margin-top: 2px;
}
</style>
