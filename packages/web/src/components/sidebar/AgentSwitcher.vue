<template>
  <div class="agent-switcher">
    <div class="nav-heading">
      <span>{{ t('sidebar.agents.title') }}</span>
      <button class="add-project-btn" :title="t('sidebar.common.manageFavorites')" @click="showPicker = true">
        <Plus :size="14" />
      </button>
    </div>

    <div v-if="favoritesStore.resolvedFavoriteAgents.length === 0" class="empty-hint">
      {{ t('sidebar.agents.emptyHint') }}
    </div>
    <div
      v-for="agent in favoritesStore.resolvedFavoriteAgents"
      :key="agent.agent_id"
      class="agent-item"
      :class="{ active: agentStore.currentAgent?.agent_id === agent.agent_id }"
      @click="agentStore.setCurrentAgent(agent)"
      @mouseenter="showTooltip(agent)"
      @mouseleave="scheduleHide()"
    >
      <div class="agent-avatar" :style="{ background: avatarColor(agent.slug) }">
        {{ initials(agent.label) }}
      </div>
      <span class="agent-label">{{ agent.label }}</span>
      <span v-if="agentStore.currentAgent?.agent_id === agent.agent_id" class="agent-active-pip" />
    </div>

    <!-- Tooltip -->
    <Teleport to="body">
      <div
        v-if="tooltipAgent"
        class="agent-tooltip agent-tooltip--detail"
        :style="tooltipStyle"
        @mouseenter="cancelHide()"
        @mouseleave="scheduleHide()"
      >
        <div class="agent-tooltip__slug">{{ tooltipAgent.label }}</div>
        <div class="agent-tooltip__desc">{{ tooltipAgent.description }}</div>
        <div v-if="tooltipAgent.skills.length" class="agent-tooltip__section">
          <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipSkills') }}</span>
          <div class="agent-tooltip__list">
            <div v-for="s in tooltipAgent.skills" :key="s.slug" class="agent-tooltip__item">
              <span class="agent-tooltip__slug">{{ s.slug }}</span>
              <span v-if="s.description" class="agent-tooltip__item-desc">{{ s.description }}</span>
            </div>
          </div>
        </div>
        <div v-if="tooltipAgent.workflows.length" class="agent-tooltip__section">
          <span class="agent-tooltip__heading">{{ t('sidebar.agents.tooltipWorkflows') }}</span>
          <div class="agent-tooltip__list">
            <div v-for="w in tooltipAgent.workflows" :key="w.slug" class="agent-tooltip__item">
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
    </Teleport>

    <AgentPickerDialog v-if="showPicker" @close="showPicker = false" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agent'
import { useFavoritesStore } from '@/stores/favorites'
import { useProjectStore } from '@/stores/project'
import { agentStaticTools, agentMcpServers, initials, avatarColor } from '@/utils/agent'
import type { AgentInfo } from '@/types'
import AgentPickerDialog from './AgentPickerDialog.vue'

const { t } = useI18n()
const agentStore = useAgentStore()
const favoritesStore = useFavoritesStore()
const projectStore = useProjectStore()
const showPicker = ref(false)
const tooltipAgent = ref<AgentInfo | null>(null)
let hideTimer: ReturnType<typeof setTimeout> | null = null

function showTooltip(agent: AgentInfo) {
  cancelHide()
  tooltipAgent.value = agent
}

function scheduleHide() {
  hideTimer = setTimeout(() => { tooltipAgent.value = null }, 150)
}

function cancelHide() {
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null }
}

// Project agents are scoped to the current project; refetch when it changes.
onMounted(() => agentStore.fetchAgents(projectStore.currentProject?.project_id ?? null))
watch(
  () => projectStore.currentProject?.project_id ?? null,
  (projectId) => agentStore.fetchAgents(projectId),
)
onBeforeUnmount(() => cancelHide())

const tooltipStyle = computed(() => ({ top: '50%', right: '16px', transform: 'translateY(-50%)' }))

/** Static (non-MCP) tool names from the agent's frontmatter tools list. */
const staticTools = computed(() => agentStaticTools(tooltipAgent.value))

/** MCP server slugs parsed from mcp / mcp:<slug> markers. */
const mcpServers = computed(() => agentMcpServers(tooltipAgent.value))
</script>
