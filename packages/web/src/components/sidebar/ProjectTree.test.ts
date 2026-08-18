import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ProjectTree from './ProjectTree.vue'
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
  it('renders the category tag for favorited projects', () => {
    favorites.resolvedFavoriteProjects = [makeProject()]
    const wrapper = mount(ProjectTree)
    const tag = wrapper.find('.project-category-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toBe('数据分析')
  })

  it('omits the tag when a project has no category_label', () => {
    favorites.resolvedFavoriteProjects = [
      makeProject({ category_label: undefined }),
    ]
    const wrapper = mount(ProjectTree)
    expect(wrapper.find('.project-category-tag').exists()).toBe(false)
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
})
