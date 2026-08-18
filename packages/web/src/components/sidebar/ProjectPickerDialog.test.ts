import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import ProjectPickerDialog from './ProjectPickerDialog.vue'
import type { Project } from '@/types'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

const projectStore = vi.hoisted(() => ({
  projects: [] as Project[],
  currentProject: null as Project | null,
  setCurrentProject: vi.fn(),
  addProject: vi.fn(),
  clearProject: vi.fn(),
  refreshProjects: vi.fn(),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))

const favSet = new Set<string>()
const favorites = vi.hoisted(() => ({
  isProjectFavorited: (id: string) => favSet.has(id),
  toggleProjectFavorite: vi.fn((id: string) => {
    if (favSet.has(id)) favSet.delete(id)
    else favSet.add(id)
  }),
  removeProjectFavorite: vi.fn((id: string) => favSet.delete(id)),
  resolvedFavoriteProjects: [] as Project[],
}))
vi.mock('@/stores/favorites', () => ({ useFavoritesStore: () => favorites }))

const agentStore = vi.hoisted(() => ({
  agents: [] as Array<{ slug: string; project_id: string | null }>,
  fetchAgents: vi.fn(),
  setCurrentAgent: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

vi.mock('@/api/projects', () => ({
  projectsApi: {
    getCategories: vi.fn().mockResolvedValue([]),
    createProject: vi.fn(),
  },
}))

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

describe('ProjectPickerDialog', () => {
  let wrapper: VueWrapper | null = null

  beforeEach(() => {
    projectStore.projects = []
    favSet.clear()
    router.push.mockClear()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('renders the category tag for each favoritable project', async () => {
    projectStore.projects = [makeProject()]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    await vi.waitFor(() => {
      const items = document.querySelectorAll('.picker-item')
      expect(items.length).toBe(1)
      const tag = items[0].querySelector('.picker-category-tag')
      expect(tag?.textContent).toBe('数据分析')
    })
  })

  it('omits the tag when a project has no category_label', async () => {
    projectStore.projects = [makeProject({ category_label: undefined })]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    await vi.waitFor(() => {
      const tag = document.querySelector('.picker-category-tag')
      expect(tag).toBeNull()
    })
  })

  it('filters projects by search query', async () => {
    projectStore.projects = [
      makeProject({ project_id: 'p-1', display_name: '销售分析' }),
      makeProject({ project_id: 'p-2', display_name: '财务报表', category_label: '软件项目', category: 'software' }),
    ]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    const input = document.querySelector<HTMLInputElement>('.picker-search')
    expect(input).not.toBeNull()
    input!.value = '财务'
    input!.dispatchEvent(new Event('input'))
    await vi.waitFor(() => {
      const items = document.querySelectorAll('.picker-item')
      expect(items.length).toBe(1)
      expect(items[0].querySelector('.picker-item-name')?.textContent).toBe('财务报表')
    })
  })

  it('shows the empty hint when no project matches', async () => {
    projectStore.projects = [makeProject()]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    const input = document.querySelector<HTMLInputElement>('.picker-search')
    input!.value = '不存在的项目'
    input!.dispatchEvent(new Event('input'))
    await vi.waitFor(() => {
      expect(document.querySelectorAll('.picker-item').length).toBe(0)
      expect(document.querySelector('.picker-empty')?.textContent).toContain('没有匹配的项目')
    })
  })

  it('toggles the favorite when clicking a row (no navigation)', async () => {
    projectStore.projects = [makeProject()]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    await vi.waitFor(() => {
      const item = document.querySelector<HTMLElement>('.picker-item')
      expect(item).not.toBeNull()
      item!.click()
      expect(favSet.has('p-1')).toBe(true)
      // 行点击只收藏,不切换项目/不跳转
      expect(projectStore.setCurrentProject).not.toHaveBeenCalled()
      expect(router.push).not.toHaveBeenCalled()
    })
  })

  it('marks favorited rows with the favorited class', async () => {
    projectStore.projects = [makeProject()]
    wrapper = mount(ProjectPickerDialog, { attachTo: document.body })
    await vi.waitFor(() => {
      const item = document.querySelector<HTMLElement>('.picker-item')
      expect(item?.classList.contains('favorited')).toBe(false)
    })
    const item = document.querySelector<HTMLElement>('.picker-item')!
    item.click()
    // mock 的 favSet 非响应式,手动触发重新渲染以反映收藏态
    await wrapper.vm.$forceUpdate()
    await wrapper.vm.$nextTick()
    expect(item.classList.contains('favorited')).toBe(true)
  })
})
