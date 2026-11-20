import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ProjectTree from './ProjectTree.vue'
import { setLocale } from '@/i18n'
import type { Project } from '@/types'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

const projectStore = vi.hoisted(() => ({
  currentProject: null as Project | null,
  setCurrentProject: vi.fn(),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

const favorites = vi.hoisted(() => ({
  resolvedFavoriteProjects: [] as Project[],
}))
vi.mock('@/stores/favorites', () => ({ useFavoritesStore: () => favorites }))

const runCounts = vi.hoisted(() => {
  const counts: Record<string, number> = {}
  return {
    counts,
    projectCount: (id: string) => counts[id] ?? 0,
    reset: () => { for (const k of Object.keys(counts)) delete counts[k] },
  }
})
vi.mock('@/stores/runningCounts', () => ({ useRunningCountsStore: () => runCounts }))

function makeProject(over: Partial<Project> = {}): Project {
  return {
    project_id: 'p-1',
    slug: 'sales',
    display_name: '销售分析',
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category: 'data-analysis',
    category_label: '数据分析',
    created_by: 'u-1',
    visibility: 'public',
    is_owner: true,
    can_manage: true,
    ...over,
  }
}

describe('ProjectTree', () => {
  afterEach(() => {
    setLocale('zh-CN')
    runCounts.reset()
  })

  it('renders the project name without category or visibility tags', () => {
    favorites.resolvedFavoriteProjects = [makeProject()]
    const wrapper = mount(ProjectTree)
    expect(wrapper.find('.project-name').text()).toBe('销售分析')
    expect(wrapper.find('.project-category-tag').exists()).toBe(false)
    expect(wrapper.find('.project-visibility-badge').exists()).toBe(false)
  })

  it('shows the empty hint when no projects are favorited', () => {
    favorites.resolvedFavoriteProjects = []
    const wrapper = mount(ProjectTree)
    expect(wrapper.find('.empty-hint').text()).toContain('收藏项目')
  })

  it('selects the project and navigates on click', async () => {
    favorites.resolvedFavoriteProjects = [makeProject()]
    projectStore.currentProject = null
    const wrapper = mount(ProjectTree)
    await wrapper.find('.project-item').trigger('click')
    expect(projectStore.setCurrentProject).toHaveBeenCalledWith(
      favorites.resolvedFavoriteProjects[0],
    )
    expect(router.push).toHaveBeenCalledWith('/projects/p-1/chat')
  })

  it('shows the running-count badge when the project has running turns', () => {
    runCounts.counts['p-1'] = 2
    favorites.resolvedFavoriteProjects = [makeProject()]
    const wrapper = mount(ProjectTree)
    const badge = wrapper.find('.project-run-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('2')
  })

  it('hides the badge when the count is zero or absent', () => {
    favorites.resolvedFavoriteProjects = [makeProject()]
    const wrapper = mount(ProjectTree)
    expect(wrapper.find('.project-run-badge').exists()).toBe(false)

    runCounts.counts['p-1'] = 0
    expect(mount(ProjectTree).find('.project-run-badge').exists()).toBe(false)
  })

  it('does not leak another project count into this row', () => {
    runCounts.counts['p-other'] = 5
    favorites.resolvedFavoriteProjects = [makeProject()]
    const wrapper = mount(ProjectTree)
    expect(wrapper.find('.project-run-badge').exists()).toBe(false)
  })
})
