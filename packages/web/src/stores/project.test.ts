import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Project } from '@/types'

vi.mock('@/api/projects', () => ({
  projectsApi: {
    getProjects: vi.fn(),
  },
}))

import { projectsApi } from '@/api/projects'
import { useProjectStore } from './project'

function makeProject(id: string): Project {
  return {
    project_id: id,
    slug: id,
    display_name: id,
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category: 'software',
    created_by: 'u-1',
    visibility: 'public',
    is_owner: true,
    can_manage: true,
  }
}

describe('project list request races', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.resetAllMocks()
  })

  it('drops an in-flight refresh that a mutation invalidated, then refetches', async () => {
    let resolveFirst!: (projects: Project[]) => void
    vi.mocked(projectsApi.getProjects)
      .mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValueOnce([makeProject('new-project')])
    const store = useProjectStore()
    const refresh = store.refreshProjects()

    store.addProject(makeProject('new-project'))
    resolveFirst([makeProject('old-project')])
    await refresh
    // The mutation scheduled a replacement fetch; give it a microtask turn.
    await vi.waitFor(() => {
      expect(store.projects.map(project => project.project_id)).toEqual(['new-project'])
    })
    expect(projectsApi.getProjects).toHaveBeenCalledTimes(2)
  })

  it('does not let ensureCurrentProject overwrite a mid-flight mutation', async () => {
    let resolveProjects!: (projects: Project[]) => void
    vi.mocked(projectsApi.getProjects).mockReturnValue(
      new Promise(resolve => { resolveProjects = resolve }),
    )
    const store = useProjectStore()
    const ensure = store.ensureCurrentProject()

    store.addProject(makeProject('created-elsewhere'))
    resolveProjects([makeProject('stale-snapshot')])
    await ensure

    expect(store.projects.map(project => project.project_id)).toEqual(['created-elsewhere'])
  })

  it('a local mutation discards an older snapshot but keeps refreshing to server truth', async () => {
    // First fetch lands via setProjects (old snapshot) while a refresh is
    // in flight; the mutation-bumped sequence must not pin the old data —
    // the rescue fetch converges on the server list.
    let resolveRefresh!: (projects: Project[]) => void
    vi.mocked(projectsApi.getProjects)
      .mockReturnValueOnce(new Promise(resolve => { resolveRefresh = resolve }))
      .mockResolvedValueOnce([makeProject('server-project')])
    const store = useProjectStore()
    const refresh = store.refreshProjects()

    store.removeProject('never-added') // no-op mutation still bumps + rescues
    resolveRefresh([makeProject('pre-mutation')])
    await refresh
    await vi.waitFor(() => {
      expect(store.projects.map(project => project.project_id)).toEqual(['server-project'])
    })
  })
})
