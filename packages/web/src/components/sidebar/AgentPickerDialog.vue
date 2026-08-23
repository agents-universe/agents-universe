<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')" @keydown.esc="emit('close')">
      <div class="modal-dialog picker-dialog">
        <!-- Header -->
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon"><Bot :size="18" /></span>
            <h3 class="modal-title">{{ t('agentPicker.title') }}</h3>
          </div>
          <button class="modal-close" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">{{ t('agentPicker.hint') }}</p>

        <!-- Search -->
        <div class="picker-search-wrapper">
          <Search :size="14" class="picker-search-icon" />
          <input
            v-model="search"
            class="picker-search"
            :placeholder="t('agentPicker.searchPlaceholder')"
            autofocus
            @keydown.esc="emit('close')"
          />
        </div>

        <!-- Agent List -->
        <div class="picker-list">
          <template v-for="group in groupedAgents" :key="group.category">
            <div class="picker-category-heading">{{ group.label }}</div>
            <div
              v-for="agent in group.agents"
              :key="agent.agent_id"
              class="picker-item"
              :class="{ favorited: favoritesStore.isAgentFavorited(agent.slug) }"
              :title="t('sidebar.common.toggleFavorite')"
              @click="favoritesStore.toggleAgentFavorite(agent.slug)"
            >
            <div class="picker-agent-avatar" :style="{ background: avatarColor(agent.slug) }">
              {{ initials(agent.label) }}
            </div>
            <div class="picker-item-info">
              <span class="picker-item-name">{{ agent.label }}</span>
              <span v-if="agent.description" class="picker-item-desc">{{ agent.description }}</span>
            </div>
            <button
              class="picker-star"
              :class="{ favorited: favoritesStore.isAgentFavorited(agent.slug) }"
              :title="t('sidebar.common.favoriteToSidebar')"
              @click.stop="favoritesStore.toggleAgentFavorite(agent.slug)"
            >
              <Star :size="14" />
            </button>
            </div>
          </template>
          <div v-if="filteredAgents.length === 0" class="picker-empty">
            {{ t('agentPicker.noMatches') }}
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot, Star, Search, X } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agent'
import { useFavoritesStore } from '@/stores/favorites'
import { useProjectStore } from '@/stores/project'
import type { AgentInfo } from '@/types'

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const agentStore = useAgentStore()
const favoritesStore = useFavoritesStore()
const projectStore = useProjectStore()

const search = ref('')

// 每次打开都重新拉取智能体列表(含项目专属智能体);store 内为会话级缓存
onMounted(async () => {
  await agentStore.reloadAgents(projectStore.currentProject?.project_id ?? null)
})

const filteredAgents = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return agentStore.agents
  return agentStore.agents.filter(a =>
    a.label.toLowerCase().includes(q) ||
    a.slug.toLowerCase().includes(q) ||
    a.description.toLowerCase().includes(q)
  )
})

const categoryOrder = ['agile-development', 'platform-assistant', 'security', '']
const categoryLabels: Record<string, string> = {
  'agile-development': t('agentPicker.categoryAgile'),
  'platform-assistant': t('agentPicker.categoryPlatform'),
  'security': t('agentPicker.categorySecurity'),
  '': t('agentPicker.categoryUnknown'),
}
const groupedAgents = computed(() => {
  // Project-scoped agents form their own group, shown first.
  const projectAgents = filteredAgents.value.filter(a => a.project_id)
  const groups: Array<{ category: string; label: string; agents: AgentInfo[] }> = []
  if (projectAgents.length > 0) {
    groups.push({ category: '__project__', label: t('agentPicker.groupProject'), agents: projectAgents })
  }
  groups.push(...categoryOrder
    .map(category => ({
      category,
      label: categoryLabels[category],
      agents: filteredAgents.value.filter(agent => {
        if (agent.project_id) return false
        const agentCategory = agent.category || ''
        return category === ''
          ? !categoryOrder.slice(0, -1).includes(agentCategory)
          : agentCategory === category
      }),
    }))
    .filter(group => group.agents.length > 0))
  return groups
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
