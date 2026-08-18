import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useProjectStore } from './project'
import { useAgentStore } from './agent'
import type { Project, AgentInfo } from '@/types'

const PROJECT_FAV_KEY = 'agents-universe:favoriteProjectIds'
const AGENT_FAV_KEY = 'agents-universe:favoriteAgentSlugs'

function loadFromStorage(key: string): string[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    // JSON.parse can succeed with a non-array (e.g. "{}" from a foreign
    // writer) — a truthy non-array would crash every .map() consumer.
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function saveToStorage(key: string, value: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch { /* storage unavailable — favorites survive in memory only */ }
}

export const useFavoritesStore = defineStore('favorites', () => {
  const favoriteProjectIds = ref<string[]>(loadFromStorage(PROJECT_FAV_KEY))
  const favoriteAgentSlugs = ref<string[]>(loadFromStorage(AGENT_FAV_KEY))

  const resolvedFavoriteProjects = computed<Project[]>(() => {
    const projectStore = useProjectStore()
    return favoriteProjectIds.value
      .map(id => projectStore.projects.find(p => p.project_id === id))
      .filter((p): p is Project => p !== undefined)
  })

  const resolvedFavoriteAgents = computed<AgentInfo[]>(() => {
    const agentStore = useAgentStore()
    return favoriteAgentSlugs.value
      .map(slug => agentStore.agents.find(a => a.slug === slug))
      .filter((a): a is AgentInfo => a !== undefined)
  })

  function toggleProjectFavorite(projectId: string) {
    const idx = favoriteProjectIds.value.indexOf(projectId)
    if (idx >= 0) {
      favoriteProjectIds.value.splice(idx, 1)
    } else {
      favoriteProjectIds.value.push(projectId)
    }
    saveToStorage(PROJECT_FAV_KEY, favoriteProjectIds.value)
  }

  function removeProjectFavorite(projectId: string) {
    favoriteProjectIds.value = favoriteProjectIds.value.filter(id => id !== projectId)
    saveToStorage(PROJECT_FAV_KEY, favoriteProjectIds.value)
  }

  function toggleAgentFavorite(slug: string) {
    const idx = favoriteAgentSlugs.value.indexOf(slug)
    if (idx >= 0) {
      favoriteAgentSlugs.value.splice(idx, 1)
    } else {
      favoriteAgentSlugs.value.push(slug)
    }
    saveToStorage(AGENT_FAV_KEY, favoriteAgentSlugs.value)
  }

  function isProjectFavorited(projectId: string): boolean {
    return favoriteProjectIds.value.includes(projectId)
  }

  function isAgentFavorited(slug: string): boolean {
    return favoriteAgentSlugs.value.includes(slug)
  }

  return {
    favoriteProjectIds,
    favoriteAgentSlugs,
    resolvedFavoriteProjects,
    resolvedFavoriteAgents,
    toggleProjectFavorite,
    removeProjectFavorite,
    toggleAgentFavorite,
    isProjectFavorited,
    isAgentFavorited,
  }
})
