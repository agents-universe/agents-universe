import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useFavoritesStore } from './favorites'
import { useAuthStore } from './auth'
import { useProjectStore } from './project'
import { useAgentStore } from './agent'
import type { Project, AgentInfo } from '@/types'

function makeProject(id: string, category = 'software'): Project {
  return {
    project_id: id,
    slug: id,
    display_name: id,
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category,
    created_by: 'u-1',
    visibility: 'public',
    is_owner: true,
    can_manage: true,
  }
}

function makeAgent(slug: string): AgentInfo {
  return {
    agent_id: `a-${slug}`,
    slug,
    label: slug,
    description: '',
    category: 'general',
    skills: [],
    workflows: [],
  }
}

const SOFTWARE_DEFAULTS = ['project-owner', 'tech-lead', 'quality-assurance']
const DATA_DEFAULTS = ['data-analyst', 'office-assistant']

/** Sign in and land on a project — the scope the store keys its storage by. */
function enterProject(userId: string, projectId: string, category = 'software') {
  useAuthStore().setUser({ userId, displayName: userId })
  // Assign directly: setCurrentProject() runs an async chain that resets every
  // project-scoped store, which is not what these tests are about.
  useProjectStore().currentProject = makeProject(projectId, category)
}

function scopeKey(userId: string, projectId: string): string {
  return `agents-universe:agentFav:${userId}:${projectId}`
}

describe('agent favorites: category defaults', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('resolves a project to its category defaults without any stored delta', () => {
    enterProject('u-1', 'p1', 'software')
    const store = useFavoritesStore()
    expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)
    expect(store.hasAgentFavoriteOverrides).toBe(false)
  })

  it('resolves an unknown category to nothing rather than another category', () => {
    enterProject('u-1', 'p1', 'brand-new-category')
    expect(useFavoritesStore().favoriteAgentSlugs).toEqual([])
  })

  it('follows the category when the project changes without a remount', () => {
    enterProject('u-1', 'p1', 'software')
    const store = useFavoritesStore()
    useProjectStore().currentProject = makeProject('p2', 'data-analysis')
    expect(store.favoriteAgentSlugs).toEqual(DATA_DEFAULTS)
  })
})

describe('agent favorites: overrides', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('appends a manual favorite after the defaults', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('pentest-expert')
    expect(store.addedAgentSlugs).toEqual(['pentest-expert'])
    expect(store.removedAgentSlugs).toEqual([])
    expect(store.favoriteAgentSlugs).toEqual([...SOFTWARE_DEFAULTS, 'pentest-expert'])
    expect(store.hasAgentFavoriteOverrides).toBe(true)
  })

  it('leaves a tombstone when a default is unfavorited', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('quality-assurance')
    expect(store.removedAgentSlugs).toEqual(['quality-assurance'])
    expect(store.addedAgentSlugs).toEqual([])
    expect(store.favoriteAgentSlugs).toEqual(['project-owner', 'tech-lead'])
  })

  it('clears the tombstone when the default is favorited again', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('quality-assurance')
    store.toggleAgentFavorite('quality-assurance')
    expect(store.removedAgentSlugs).toEqual([])
    expect(store.addedAgentSlugs).toEqual([])
    expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)
    expect(store.hasAgentFavoriteOverrides).toBe(false)
  })

  it('persists only the deviation from the defaults', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('project-owner')
    expect(JSON.parse(localStorage.getItem(scopeKey('u-1', 'p1'))!))
      .toEqual({ added: [], removed: ['project-owner'] })
  })

  it('reads the stored deviation back on reload', () => {
    localStorage.setItem(scopeKey('u-1', 'p1'), JSON.stringify({
      added: ['pentest-expert'], removed: ['tech-lead'],
    }))
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    expect(store.favoriteAgentSlugs).toEqual(['project-owner', 'quality-assurance', 'pentest-expert'])
  })

  it('drops a stored addition that the defaults now cover', () => {
    // e.g. the agent was favorited manually and later became a category default
    localStorage.setItem(scopeKey('u-1', 'p1'), JSON.stringify({
      added: ['tech-lead', 'tech-lead', 'pentest-expert'], removed: [],
    }))
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    expect(store.addedAgentSlugs).toEqual(['pentest-expert'])
    expect(store.favoriteAgentSlugs).toEqual([...SOFTWARE_DEFAULTS, 'pentest-expert'])
  })

  it('ignores a stray tombstone for an agent that is not a default', () => {
    localStorage.setItem(scopeKey('u-1', 'p1'), JSON.stringify({
      added: [], removed: ['pentest-expert'],
    }))
    enterProject('u-1', 'p1')
    expect(useFavoritesStore().hasAgentFavoriteOverrides).toBe(false)
  })

  it('treats a slug listed as both added and removed as favorited', () => {
    localStorage.setItem(scopeKey('u-1', 'p1'), JSON.stringify({
      added: ['pentest-expert'], removed: ['pentest-expert'],
    }))
    enterProject('u-1', 'p1')
    expect(useFavoritesStore().favoriteAgentSlugs).toEqual([...SOFTWARE_DEFAULTS, 'pentest-expert'])
  })

  it('restores the defaults on reset', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('quality-assurance')
    store.toggleAgentFavorite('pentest-expert')
    store.resetAgentFavoritesToDefaults()
    expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)
    expect(store.hasAgentFavoriteOverrides).toBe(false)
    expect(JSON.parse(localStorage.getItem(scopeKey('u-1', 'p1'))!))
      .toEqual({ added: [], removed: [] })
  })
})

describe('agent favorites: isolation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('keeps each project on its own favorites', () => {
    enterProject('u-1', 'p1', 'software')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('project-owner')
    store.toggleAgentFavorite('pentest-expert')

    useProjectStore().currentProject = makeProject('p2', 'data-analysis')
    expect(store.favoriteAgentSlugs).toEqual(DATA_DEFAULTS)

    // p1's deviation is untouched on disk and comes back with the project
    useProjectStore().currentProject = makeProject('p1', 'software')
    expect(store.favoriteAgentSlugs).toEqual(['tech-lead', 'quality-assurance', 'pentest-expert'])
    expect(JSON.parse(localStorage.getItem(scopeKey('u-1', 'p1'))!))
      .toEqual({ added: ['pentest-expert'], removed: ['project-owner'] })
    expect(localStorage.getItem(scopeKey('u-1', 'p2'))).toBeNull()
  })

  it('keeps each user on their own favorites on a shared browser', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('project-owner')

    useAuthStore().logout()
    enterProject('u-2', 'p1')
    expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)

    store.toggleAgentFavorite('tech-lead')
    expect(JSON.parse(localStorage.getItem(scopeKey('u-2', 'p1'))!))
      .toEqual({ added: [], removed: ['tech-lead'] })
    expect(JSON.parse(localStorage.getItem(scopeKey('u-1', 'p1'))!))
      .toEqual({ added: [], removed: ['project-owner'] })
  })

  it('scopes a projectless session to a sentinel key with no defaults', () => {
    useAuthStore().setUser({ userId: 'u-1', displayName: 'u-1' })
    const store = useFavoritesStore()
    expect(store.favoriteAgentSlugs).toEqual([])
    store.toggleAgentFavorite('pentest-expert')
    expect(JSON.parse(localStorage.getItem(scopeKey('u-1', '__no-project__'))!))
      .toEqual({ added: ['pentest-expert'], removed: [] })
  })

  it('writes nothing before the user is known', () => {
    // /api/me lands after the components mount; a write now would need a
    // userless key, i.e. the cross-account sharing this replaced.
    useProjectStore().currentProject = makeProject('p1')
    const store = useFavoritesStore()
    store.toggleAgentFavorite('pentest-expert')
    expect(store.isAgentFavorited('pentest-expert')).toBe(true)
    expect(localStorage.length).toBe(0)

    useAuthStore().setUser({ userId: 'u-1', displayName: 'u-1' })
    // The unpersisted click is not carried over into the user's scope.
    expect(store.isAgentFavorited('pentest-expert')).toBe(false)
  })
})

describe('agent favorites: storage hygiene', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('deletes the legacy browser-wide list instead of migrating it', () => {
    localStorage.setItem('agents-universe:favoriteAgentSlugs', '["pentest-expert"]')
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    expect(localStorage.getItem('agents-universe:favoriteAgentSlugs')).toBeNull()
    expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)
  })

  it.each(['{oops', '{}', '"abc"', '[]', 'null', '{"added":"x","removed":7}'])(
    'tolerates the stored value %s',
    (raw) => {
      localStorage.setItem(scopeKey('u-1', 'p1'), raw)
      enterProject('u-1', 'p1')
      const store = useFavoritesStore()
      expect(store.addedAgentSlugs).toEqual([])
      expect(store.removedAgentSlugs).toEqual([])
      expect(store.favoriteAgentSlugs).toEqual(SOFTWARE_DEFAULTS)
      expect(() => store.resolvedFavoriteAgents).not.toThrow()
    },
  )

  it('resolves only agents that exist in the current scope', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    useAgentStore().setAgents([makeAgent('tech-lead')])
    store.toggleAgentFavorite('pentest-expert')
    expect(store.isAgentFavorited('pentest-expert')).toBe(true)
    expect(store.resolvedFavoriteAgents.map(a => a.slug)).toEqual(['tech-lead'])
  })

  it('survives setItem throwing', () => {
    enterProject('u-1', 'p1')
    const store = useFavoritesStore()
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('quota exceeded') }
    try {
      expect(() => store.toggleAgentFavorite('project-owner')).not.toThrow()
      expect(store.favoriteAgentSlugs).toEqual(['tech-lead', 'quality-assurance'])
    } finally {
      Storage.prototype.setItem = original
    }
  })
})
