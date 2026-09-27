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

  it('does not let an in-flight refresh erase a newly added project', async () => {
    let resolveProjects!: (projects: Project[]) => void
    vi.mocked(projectsApi.getProjects).mockReturnValue(
      new Promise(resolve => { resolveProjects = resolve }),
    )
    const store = useProjectStore()
    const refresh = store.refreshProjects()

    store.addProject(makeProject('new-project'))
    resolveProjects([makeProject('old-project')])
    await refresh

    expect(store.projects.map(project => project.project_id)).toEqual(['new-project'])
  })
})
