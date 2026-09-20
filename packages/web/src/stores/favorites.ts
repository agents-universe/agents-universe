import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { useAuthStore } from './auth'
import { useProjectStore } from './project'
import { useAgentStore } from './agent'
import { defaultFavoriteAgentSlugs } from '@/utils/agentFavorites'
import type { Project, AgentInfo } from '@/types'

const PROJECT_FAV_KEY = 'agents-universe:favoriteProjectIds'
const AGENT_FAV_KEY_PREFIX = 'agents-universe:agentFav:'
const LEGACY_AGENT_FAV_KEY = 'agents-universe:favoriteAgentSlugs'
/** Scope used while no project is selected — the sidebar renders before a
 *  project is restored, and after the last one is deleted. */
const NO_PROJECT_SCOPE = '__no-project__'

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

/** Only the deviation from the category defaults is persisted, so a change to
 *  the defaults table still reaches projects that already exist. */
interface AgentFavoriteDelta {
  added: string[]
  removed: string[]
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const item of value) {
    if (typeof item === 'string' && item && !out.includes(item)) out.push(item)
  }
  return out
}

function loadDelta(key: string): AgentFavoriteDelta {
  const empty: AgentFavoriteDelta = { added: [], removed: [] }
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return empty
    // JSON.parse also succeeds for "{}", '"abc"' and '[]' — anything but a
    // plain object is a foreign writer, not a delta.
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return empty
    const obj = parsed as Record<string, unknown>
    return { added: stringList(obj.added), removed: stringList(obj.removed) }
  } catch {
    return empty
  }
}

function saveDelta(key: string, delta: AgentFavoriteDelta) {
  try {
    localStorage.setItem(key, JSON.stringify(delta))
  } catch { /* storage unavailable — favorites survive in memory only */ }
}

/** Keep the two lists disjoint and minimal. `added` may not repeat a default
 *  (the agent could have become one after it was favorited), and a slug that
 *  somehow sits in both lists counts as favorited — hand-edited storage is the
 *  only way to get that state. */
function normalizeDelta(defaults: string[], raw: AgentFavoriteDelta): AgentFavoriteDelta {
  const removed = raw.removed.filter(slug => !raw.added.includes(slug))
  const defaultSet = new Set(defaults)
  const added = raw.added.filter(slug => !defaultSet.has(slug) && !removed.includes(slug))
  return { added, removed }
}

export const useFavoritesStore = defineStore('favorites', () => {
  const authStore = useAuthStore()
  const projectStore = useProjectStore()

  const favoriteProjectIds = ref<string[]>(loadFromStorage(PROJECT_FAV_KEY))

  const addedAgentSlugs = ref<string[]>([])
  const removedAgentSlugs = ref<string[]>([])

  // Agent favorites are private to one user and scoped to one project; the
  // project list itself stays a browser-wide list (unchanged).
  const agentFavScopeKey = computed<string | null>(() => {
    const userId = authStore.userId
    // Signed out (or /api/me still in flight): no scope at all. Reading or
    // writing a userless key is exactly the cross-account sharing we removed.
    if (!userId) return null
    const projectId = projectStore.currentProject?.project_id ?? NO_PROJECT_SCOPE
    return `${AGENT_FAV_KEY_PREFIX}${userId}:${projectId}`
  })

  const defaultAgentSlugs = computed<string[]>(() =>
    defaultFavoriteAgentSlugs(projectStore.currentProject?.category)
  )

  /** Defaults first (table order), then the user's own picks in star order. */
  const favoriteAgentSlugs = computed<string[]>(() => {
    const removed = new Set(removedAgentSlugs.value)
    const seen = new Set<string>()
    const slugs: string[] = []
    for (const slug of [...defaultAgentSlugs.value, ...addedAgentSlugs.value]) {
      if (removed.has(slug) || seen.has(slug)) continue
      seen.add(slug)
      slugs.push(slug)
    }
    return slugs
  })

  const resolvedFavoriteProjects = computed<Project[]>(() => {
    const projects = projectStore.projects
    return favoriteProjectIds.value
      .map(id => projects.find(p => p.project_id === id))
      .filter((p): p is Project => p !== undefined)
  })

  const resolvedFavoriteAgents = computed<AgentInfo[]>(() => {
    const agents = useAgentStore().agents
    return favoriteAgentSlugs.value
      .map(slug => agents.find(a => a.slug === slug))
      .filter((a): a is AgentInfo => a !== undefined)
  })

  /** Whether the sidebar currently deviates from the category defaults. */
  const hasAgentFavoriteOverrides = computed<boolean>(() => {
    const defaults = defaultAgentSlugs.value
    return addedAgentSlugs.value.some(slug => !defaults.includes(slug))
      || removedAgentSlugs.value.some(slug => defaults.includes(slug))
  })

  /** Idempotent re-read of the current scope. Also called by the watcher below,
   *  and by tests that reassign `currentProject` directly. */
  function syncAgentFavorites() {
    const key = agentFavScopeKey.value
    if (!key) {
      addedAgentSlugs.value = []
      removedAgentSlugs.value = []
      return
    }
    const delta = normalizeDelta(defaultAgentSlugs.value, loadDelta(key))
    addedAgentSlugs.value = delta.added
    removedAgentSlugs.value = delta.removed
  }

  function persistAgentFavorites() {
    const key = agentFavScopeKey.value
    if (!key) return
    saveDelta(key, { added: addedAgentSlugs.value, removed: removedAgentSlugs.value })
  }

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

  function isProjectFavorited(projectId: string): boolean {
    return favoriteProjectIds.value.includes(projectId)
  }

  function isAgentFavorited(slug: string): boolean {
    return favoriteAgentSlugs.value.includes(slug)
  }

  /** Membership in the category defaults, ignoring tombstones — lets the picker
   *  explain why an agent is starred in a project the user just created. */
  function isDefaultFavorite(slug: string): boolean {
    return defaultAgentSlugs.value.includes(slug)
  }

  function toggleAgentFavorite(slug: string) {
    if (isAgentFavorited(slug)) {
      addedAgentSlugs.value = addedAgentSlugs.value.filter(s => s !== slug)
      // Un-starring a default leaves a tombstone, otherwise the next read would
      // resolve it right back into the list.
      if (isDefaultFavorite(slug) && !removedAgentSlugs.value.includes(slug)) {
        removedAgentSlugs.value = [...removedAgentSlugs.value, slug]
      }
    } else {
      removedAgentSlugs.value = removedAgentSlugs.value.filter(s => s !== slug)
      if (!isDefaultFavorite(slug) && !addedAgentSlugs.value.includes(slug)) {
        addedAgentSlugs.value = [...addedAgentSlugs.value, slug]
      }
    }
    persistAgentFavorites()
  }

  function resetAgentFavoritesToDefaults() {
    addedAgentSlugs.value = []
    removedAgentSlugs.value = []
    persistAgentFavorites()
  }

  // The legacy browser-wide list is dead weight: favorites are per (user,
  // project) now, and its contents are deliberately not migrated.
  try { localStorage.removeItem(LEGACY_AGENT_FAV_KEY) } catch { /* ignore */ }

  // Re-read on every scope change. A component-level load cannot cover this:
  // auth arrives after the components mount, projects change from four places,
  // and the sidebar (and with it AgentSwitcher) unmounts when collapsed.
  // `flush: 'sync'` closes the window between a synchronous project switch and
  // the reload, in which a click would persist one project's delta under
  // another project's key. Reads are synchronous, so no seq guard is needed.
  watch(agentFavScopeKey, syncAgentFavorites, { immediate: true, flush: 'sync' })

  return {
    favoriteProjectIds,
    favoriteAgentSlugs,
    addedAgentSlugs,
    removedAgentSlugs,
    agentFavScopeKey,
    defaultAgentSlugs,
    hasAgentFavoriteOverrides,
    resolvedFavoriteProjects,
    resolvedFavoriteAgents,
    syncAgentFavorites,
    toggleProjectFavorite,
    removeProjectFavorite,
    toggleAgentFavorite,
    resetAgentFavoritesToDefaults,
    isProjectFavorited,
    isAgentFavorited,
    isDefaultFavorite,
  }
})
