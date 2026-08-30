import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import AppLayout from './AppLayout.vue'

const routerPush = vi.hoisted(() => vi.fn())
const routeState = vi.hoisted(() => ({
  path: '/projects/p-1/publishes',
  params: { projectId: 'p-1' },
  fullPath: '/projects/p-1/publishes',
}))
vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => ({ push: routerPush }),
  RouterView: { name: 'RouterViewStub', template: '<div />' },
}))

vi.mock('@/composables/useWebSocket', () => ({ closeAllConnections: vi.fn() }))
vi.mock('@/composables/useProjectData', () => ({ useProjectData: vi.fn() }))
vi.mock('@/pages/ChatPage.vue', () => ({
  default: { name: 'ChatPageStub', template: '<div />' },
  invalidateLatestConversation: vi.fn(),
}))

const projectStore = vi.hoisted(() => ({
  currentProject: { project_id: 'p-1' },
  projects: [],
  setCurrentProject: vi.fn(),
  setProjects: vi.fn(),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => ({ currentAgent: null }) }))
const conversationStore = vi.hoisted(() => ({
  messages: [] as unknown[],
  isStreaming: false,
  isThinking: false,
  conversationId: null,
  reset: vi.fn(),
  loadHistory: vi.fn(),
}))
vi.mock('@/stores/conversation', () => ({ useConversationStore: () => conversationStore }))

vi.mock('@/api/projects', () => ({ projectsApi: { getProjects: vi.fn() } }))
vi.mock('@/api/conversations', () => ({ conversationsApi: { compress: vi.fn() } }))

// Child components — the topnav under test lives in this layout, not in them.
vi.mock('@/components/sidebar/ProjectTree.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/sidebar/AgentSwitcher.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/sidebar/SidebarFooter.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/knowledge/ContextMeter.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/conversations/ConversationTreePanel.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/knowledge/KnowledgePanel.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/memory/MemoryPanel.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))

describe('AppLayout center topnav', () => {
  it('renders 会话 / 工作区 / 发布 with the publishes tab active', () => {
    routeState.path = '/projects/p-1/publishes'
    const wrapper = mount(AppLayout)
    const tabs = wrapper.findAll('.center-tab')
    expect(tabs.map(t => t.text().trim())).toEqual(['会话', '工作区', '发布'])
    expect(tabs[2].classes()).toContain('active')
    expect(tabs[0].classes()).not.toContain('active')
  })

  it('marks the chat tab active on the chat segment', () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    const tabs = wrapper.findAll('.center-tab')
    expect(tabs[0].classes()).toContain('active')
    expect(tabs[2].classes()).not.toContain('active')
  })

  it('navigates via goToPage when a tab is clicked', async () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    await wrapper.findAll('.center-tab')[2].trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/projects/p-1/publishes')
  })

  it('hides the topnav on non-project routes', () => {
    routeState.path = '/settings/tokens'
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.center-topnav').exists()).toBe(false)
  })
})
