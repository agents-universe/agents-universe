<template>
  <div class="agent-switcher">
    <div class="nav-heading">
      <span>智能体</span>
      <button class="add-project-btn" title="管理收藏" @click="showPicker = true">
        <Plus :size="14" />
      </button>
    </div>

    <div v-if="favoritesStore.resolvedFavoriteAgents.length === 0" class="empty-hint">
      点击 + 收藏智能体
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
          <span class="agent-tooltip__heading">技能</span>
          <div class="agent-tooltip__list">
            <div v-for="s in tooltipAgent.skills" :key="s.slug" class="agent-tooltip__item">
              <span class="agent-tooltip__slug">{{ s.slug }}</span>
              <span v-if="s.description" class="agent-tooltip__item-desc">{{ s.description }}</span>
            </div>
          </div>
        </div>
        <div v-if="tooltipAgent.workflows.length" class="agent-tooltip__section">
          <span class="agent-tooltip__heading">工作流</span>
          <div class="agent-tooltip__list">
            <div v-for="w in tooltipAgent.workflows" :key="w.slug" class="agent-tooltip__item">
              <span class="agent-tooltip__slug">{{ w.slug }}</span>
              <span v-if="w.description" class="agent-tooltip__item-desc">{{ w.description }}</span>
            </div>
          </div>
        </div>
        <div v-if="staticTools.length" class="agent-tooltip__section">
          <span class="agent-tooltip__heading">工具</span>
          <div class="agent-tooltip__list">
            <div v-for="t in staticTools" :key="t" class="agent-tooltip__item">
              <span class="agent-tooltip__slug">{{ t }}</span>
            </div>
          </div>
        </div>
        <div v-if="mcpServers.length" class="agent-tooltip__section">
          <span class="agent-tooltip__heading">MCP 集成</span>
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
import { Plus } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agent'
import { useFavoritesStore } from '@/stores/favorites'
import { useProjectStore } from '@/stores/project'
import type { AgentInfo } from '@/types'
import AgentPickerDialog from './AgentPickerDialog.vue'

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
const staticTools = computed(() =>
  (tooltipAgent.value?.tools ?? []).filter(t => !t.startsWith('mcp')),
)

/** MCP server slugs parsed from mcp / mcp:<slug> markers. */
const mcpServers = computed(() => {
  const tools = tooltipAgent.value?.tools ?? []
  const servers: string[] = []
  for (const t of tools) {
    if (t === 'mcp') {
      servers.push('(all)')
    } else if (t.startsWith('mcp:')) {
      servers.push(t.slice(4))
    }
  }
  return servers
})

function initials(label: string): string {
  const words = label.trim().split(/\s+/)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return label.slice(0, 2).toUpperCase()
}

const COLORS = ['#5b7cf6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']
function avatarColor(slug: string): string {
  let h = 0
  for (let i = 0; i < slug.length; i++) h = (h * 31 + slug.charCodeAt(i)) & 0xffffffff
  return COLORS[Math.abs(h) % COLORS.length]
}
</script>
