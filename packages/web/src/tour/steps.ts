/**
 * Tour step registry — the declarative definition of the guided tour.
 *
 * Steps are evaluated in order; every step's `condition` is re-evaluated at
 * advance time (not tour start), so "does the user have a project" stays
 * fresh. A step whose `waitFor` times out is skipped with a console warning
 * — the tour never blocks on a missing element.
 */
import type { Router } from 'vue-router'
import { useProjectStore } from '@/stores/project'

const MOBILE_BREAKPOINT = 1024 // must match AppLayout.vue

const CHAT_ROUTE = '/projects/{projectId}/chat'

export interface TourActionContext {
  router: Router
  projectId: string | null
}

export interface TourConditionContext {
  /** Id of the step the tour was showing before this one (null at tour start). */
  prevStepId: string | null
}

export interface TourStep {
  id: string
  titleKey: string
  bodyKey: string
  /** CSS selector of the highlighted element; omitted for centered steps. */
  target?: string
  placement?: 'top' | 'bottom' | 'left' | 'right'
  /** Route to navigate to before showing this step ('{projectId}' placeholder). */
  route?: string
  /** Side effect before highlighting (open dialog, click a panel tab…). */
  action?: (ctx: TourActionContext) => void | Promise<void>
  /** Selector polled before showing; timeout → step skipped. */
  waitFor?: string
  /** Evaluated at advance time; false → step skipped. */
  condition?: (ctx: TourConditionContext) => boolean
  /** Skip eagerly at ≤1024px (right-panel steps anchor inside overlays there). */
  skipOnMobile?: boolean
  /** No target — dimmed backdrop + centered card (welcome/done). */
  center?: boolean
}

export function isMobile(): boolean {
  return window.innerWidth <= MOBILE_BREAKPOINT
}

/** Project id used to resolve '{projectId}' route templates. */
export function currentProjectId(): string | null {
  const store = useProjectStore()
  return store.currentProject?.project_id ?? store.projects[0]?.project_id ?? null
}

/** Expand the right panel and activate the tab with the given data-tour-target. */
function clickRightPanelTab(tabId: 'conversations' | 'knowledge' | 'memory') {
  const panel = document.querySelector('.panel-right')
  if (!panel) return
  if (panel.classList.contains('collapsed')) {
    const toggle = panel.querySelector<HTMLElement>('.sidebar-toggle')
    toggle?.click()
  }
  const tab = document.querySelector<HTMLElement>(`[data-tour-target="panel-tab-${tabId}"]`)
  tab?.click()
}

function openCreateProjectDialog() {
  document.querySelector<HTMLElement>('.empty-project-btn')?.click()
}

function clickIntegrationsTab() {
  document.querySelector<HTMLElement>('[data-tour-target="token-tab-integrations"]')?.click()
}

const noProjects = () => useProjectStore().projects.length === 0
const hasProject = () => currentProjectId() != null

export const TOUR_STEPS: TourStep[] = [
  { id: 'welcome', titleKey: 'tour.welcome.title', bodyKey: 'tour.welcome.body', center: true },

  // Create-first-project branch (only when the user has no projects yet)
  {
    id: 'project-cta',
    titleKey: 'tour.projectCta.title',
    bodyKey: 'tour.projectCta.body',
    target: '.empty-project-btn',
    condition: noProjects,
  },
  {
    id: 'create-project-form',
    titleKey: 'tour.createProjectForm.title',
    bodyKey: 'tour.createProjectForm.body',
    target: '.modal-dialog',
    action: openCreateProjectDialog,
    waitFor: '.modal-dialog',
    condition: noProjects,
  },
  {
    id: 'project-created',
    titleKey: 'tour.projectCreated.title',
    bodyKey: 'tour.projectCreated.body',
    center: true,
    waitFor: '.message-user',
    // Only shown right after the create-project form (which the has-project
    // flow never reaches); skipped if the dialog was dismissed without
    // creating anything.
    condition: ({ prevStepId }) => prevStepId === 'create-project-form' && hasProject(),
  },

  // Chat surface (needs a project; steps auto-skip without one)
  {
    id: 'chat-composer',
    titleKey: 'tour.chatComposer.title',
    bodyKey: 'tour.chatComposer.body',
    route: CHAT_ROUTE,
    target: '.composer',
    condition: hasProject,
  },
  {
    id: 'chat-providers',
    titleKey: 'tour.chatProviders.title',
    bodyKey: 'tour.chatProviders.body',
    target: '.composer-providers',
    condition: hasProject,
  },
  {
    id: 'chat-mentions',
    titleKey: 'tour.chatMentions.title',
    bodyKey: 'tour.chatMentions.body',
    target: '.composer-editor',
    condition: hasProject,
  },
  {
    id: 'chat-attach',
    titleKey: 'tour.chatAttach.title',
    bodyKey: 'tour.chatAttach.body',
    target: '.composer-attach',
    condition: hasProject,
  },
  {
    id: 'chat-send',
    titleKey: 'tour.chatSend.title',
    bodyKey: 'tour.chatSend.body',
    target: '.submit-btn',
    condition: hasProject,
  },
  {
    id: 'chat-messages',
    titleKey: 'tour.chatMessages.title',
    bodyKey: 'tour.chatMessages.body',
    target: '.messages-list',
    condition: hasProject,
  },
  {
    id: 'agent-switcher',
    titleKey: 'tour.agentSwitcher.title',
    bodyKey: 'tour.agentSwitcher.body',
    target: '.agent-switcher',
    condition: hasProject,
  },

  // Right panel — three tabs + the always-on context meter (desktop only)
  {
    id: 'right-conversations',
    titleKey: 'tour.rightConversations.title',
    bodyKey: 'tour.rightConversations.body',
    target: '.conv-tree',
    action: () => clickRightPanelTab('conversations'),
    waitFor: '.conv-tree',
    condition: hasProject,
    skipOnMobile: true,
  },
  {
    id: 'right-knowledge',
    titleKey: 'tour.rightKnowledge.title',
    bodyKey: 'tour.rightKnowledge.body',
    target: '.knowledge-panel',
    action: () => clickRightPanelTab('knowledge'),
    waitFor: '.knowledge-panel',
    condition: hasProject,
    skipOnMobile: true,
  },
  {
    id: 'right-memory',
    titleKey: 'tour.rightMemory.title',
    bodyKey: 'tour.rightMemory.body',
    target: '.memory-panel',
    action: () => clickRightPanelTab('memory'),
    waitFor: '.memory-panel',
    condition: hasProject,
    skipOnMobile: true,
  },
  {
    id: 'context-meter',
    titleKey: 'tour.contextMeter.title',
    bodyKey: 'tour.contextMeter.body',
    target: '.context-meter',
    action: () => clickRightPanelTab('conversations'),
    waitFor: '.context-meter',
    condition: hasProject,
    skipOnMobile: true,
  },

  // Full pages
  {
    id: 'top-nav',
    titleKey: 'tour.topNav.title',
    bodyKey: 'tour.topNav.body',
    route: CHAT_ROUTE,
    target: '.center-topnav',
    condition: hasProject,
  },
  {
    id: 'knowledge-browser',
    titleKey: 'tour.knowledgeBrowser.title',
    bodyKey: 'tour.knowledgeBrowser.body',
    route: '/projects/{projectId}/knowledge',
    target: '.knowledge-browser-page',
    condition: hasProject,
  },
  {
    id: 'scripts',
    titleKey: 'tour.scripts.title',
    bodyKey: 'tour.scripts.body',
    route: '/projects/{projectId}/scripts',
    target: '.script-executor-page',
    condition: hasProject,
  },

  // Settings page lives outside the AppLayout shell — keep it last
  {
    id: 'settings-models',
    titleKey: 'tour.settingsModels.title',
    bodyKey: 'tour.settingsModels.body',
    route: '/settings/tokens',
    target: '.token-config-modal',
    waitFor: '.token-config-modal',
  },
  {
    id: 'settings-integrations',
    titleKey: 'tour.settingsIntegrations.title',
    bodyKey: 'tour.settingsIntegrations.body',
    target: '.token-tabs',
    action: clickIntegrationsTab,
    waitFor: '.token-tabs',
  },

  { id: 'done', titleKey: 'tour.done.title', bodyKey: 'tour.done.body', center: true },
]
