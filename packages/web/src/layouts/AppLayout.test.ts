import { describe, it, expect, vi, beforeEach } from 'vitest'
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
  conversationId: null as string | null,
  tokensUsed: 0,
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

// The conversation store mock is shared mutable state — reset the fields the
// topnav reads so no test inherits the previous one's ledger/conversation.
beforeEach(() => {
  conversationStore.conversationId = null
  conversationStore.tokensUsed = 0
  conversationStore.messages = []
})

describe('AppLayout center topnav', () => {
  it('renders 会话 / 工作区 / 发布 / 定时任务 with the publishes tab active', () => {
    routeState.path = '/projects/p-1/publishes'
    const wrapper = mount(AppLayout)
    const tabs = wrapper.findAll('.center-tab')
    expect(tabs.map(t => t.text().trim())).toEqual(['会话', '工作区', '发布', '定时任务'])
    expect(tabs[2].classes()).toContain('active')
    expect(tabs[0].classes()).not.toContain('active')
  })

  it('marks the schedules tab active on the schedules segment', () => {
    routeState.path = '/projects/p-1/schedules'
    const wrapper = mount(AppLayout)
    const tabs = wrapper.findAll('.center-tab')
    expect(tabs[3].classes()).toContain('active')
    expect(tabs[1].classes()).not.toContain('active')
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

describe('AppLayout session token counter', () => {
  it('shows the lifetime ledger next to the compress button on the chat page', () => {
    routeState.path = '/projects/p-1/chat'
    conversationStore.conversationId = 'c1'
    conversationStore.tokensUsed = 12345
    conversationStore.messages = [{ id: 'm1' }]
    const wrapper = mount(AppLayout)
    const counter = wrapper.find('.nav-tokens')
    expect(counter.exists()).toBe(true)
    expect(counter.text()).toContain('会话消耗')
    expect(counter.text()).toContain((12345).toLocaleString())
    // The compress button shares the right-aligned group with the counter.
    expect(wrapper.find('.topnav-right .compress-btn').exists()).toBe(true)
  })

  it('hides the counter when nothing was consumed yet', () => {
    routeState.path = '/projects/p-1/chat'
    conversationStore.conversationId = 'c1'
    conversationStore.tokensUsed = 0
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.nav-tokens').exists()).toBe(false)
  })

  it('hides the counter without an active conversation', () => {
    routeState.path = '/projects/p-1/chat'
    conversationStore.conversationId = null
    conversationStore.tokensUsed = 500
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.nav-tokens').exists()).toBe(false)
  })

  it('stays hidden on non-chat segments', () => {
    routeState.path = '/projects/p-1/publishes'
    conversationStore.conversationId = 'c1'
    conversationStore.tokensUsed = 500
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.nav-tokens').exists()).toBe(false)
    expect(wrapper.find('.topnav-right').exists()).toBe(false)
  })
})
