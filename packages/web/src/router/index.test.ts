import { describe, it, expect, vi, beforeEach } from 'vitest'

const projectStore = vi.hoisted(() => ({
  currentProject: null as { project_id: string } | null,
  projects: [] as Array<{ project_id: string }>,
  getSavedProjectId: vi.fn(),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

// Stub the lazy route components so navigation resolves without pulling in
// the real AppLayout/PublishesPage dependency graphs.
vi.mock('@/layouts/AppLayout.vue', () => ({ default: { name: 'AppLayoutStub', template: '<div />' } }))
vi.mock('@/pages/PublishesPage.vue', () => ({ default: { name: 'PublishesPageStub', template: '<div />' } }))
vi.mock('@/pages/EmptyProjectPage.vue', () => ({ default: { name: 'EmptyProjectPageStub', template: '<div />' } }))

import router from './index'

// The auth guard calls /api/me on first navigation; stub it so navigation
// proceeds without a real network round trip.
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
  projectStore.currentProject = null
  projectStore.projects = []
  projectStore.getSavedProjectId.mockReturnValue(null)
})

function project(id: string) {
  return { project_id: id }
}

describe('router publish routes', () => {
  it('serves /projects/:id/publishes directly', async () => {
    await router.push('/projects/p1/publishes')
    expect(router.currentRoute.value.path).toBe('/projects/p1/publishes')
  })

  it('redirects /settings/publishes to the current project publishes tab', async () => {
    projectStore.projects = [project('p1'), project('p2')]
    projectStore.getSavedProjectId.mockReturnValue('p1')
    await router.push('/settings/publishes')
    expect(router.currentRoute.value.path).toBe('/projects/p1/publishes')
  })

  it('falls back to the first project when the saved id is stale', async () => {
    projectStore.projects = [project('p1'), project('p2')]
    projectStore.getSavedProjectId.mockReturnValue('ghost')
    await router.push('/settings/publishes')
    expect(router.currentRoute.value.path).toBe('/projects/p1/publishes')
  })

  it('prefers the open project over the saved one', async () => {
    projectStore.projects = [project('p1'), project('p2')]
    projectStore.currentProject = project('p2')
    projectStore.getSavedProjectId.mockReturnValue('p1')
    await router.push('/settings/publishes')
    expect(router.currentRoute.value.path).toBe('/projects/p2/publishes')
  })

  it('lands on /app when there is no project to target', async () => {
    await router.push('/settings/publishes')
    expect(router.currentRoute.value.path).toBe('/app')
  })
})
