import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Router } from 'vue-router'
import { patchPreferences } from '@/api/preferences'
import { useTourStore } from './tour'
import { useProjectStore } from './project'
import { MAIN_TOUR_STEPS } from '@/tour/steps'

vi.mock('@/api/preferences', () => ({ patchPreferences: vi.fn() }))

const mockPatch = vi.mocked(patchPreferences)

function fakeRouter(path = '/app'): Router {
  return {
    push: vi.fn().mockResolvedValue(undefined),
    currentRoute: { value: { path } },
  } as unknown as Router
}

/** A router whose push actually moves currentRoute, like the real one. */
function routingRouter(path = '/app'): Router {
  const router = {
    push: vi.fn(async (target: string) => {
      router.currentRoute.value.path = target
    }),
    currentRoute: { value: { path } },
  }
  return router as unknown as Router
}

/** Small helper: flush pending microtasks (fake timers are active). */
const flush = () => vi.advanceTimersByTimeAsync(0)

const originalInnerWidth = window.innerWidth
function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true })
}

function seedProject() {
  const projectStore = useProjectStore()
  projectStore.setCurrentProject({ project_id: 'p-1' } as never)
  projectStore.setProjects([{ project_id: 'p-1' }] as never)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.clearAllMocks()
})

afterEach(() => {
  document.body.innerHTML = ''
  setViewportWidth(originalInnerWidth)
})

describe('tour store', () => {
  it('setServerState seeds completed from the server', () => {
    const store = useTourStore()
    store.setServerState({ onboarding_completed: true, onboarding_completed_at: 'x', locale: null })
    expect(store.completed).toBe(true)
  })

  it('skip completes the tour: patches the server, resolves start(), stops', async () => {
    const store = useTourStore()
    const done = store.start('main', 0, fakeRouter())
    expect(store.isActive).toBe(true)
    expect(store.activeTourId).toBe('main')
    expect(store.stepIndex).toBe(0) // language step shows synchronously

    await store.skip()
    await flush()

    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true })
    expect(store.completed).toBe(true)
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('finish at the last step completes the tour', async () => {
    const store = useTourStore()
    const done = store.start('main', MAIN_TOUR_STEPS.length - 1, fakeRouter())
    await store.finish()
    // Drain the zombie activation (route + dialog waits) aborted by stop().
    await vi.advanceTimersByTimeAsync(9000)
    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true })
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('optimistically marks completed before the PATCH resolves', async () => {
    const store = useTourStore()
    // completeTour only persists while the main tour is active (knowledge
    // tours must never touch preferences) — claim the slot first.
    store.start('main', 0, fakeRouter())
    let resolvePatch: (v: unknown) => void
    mockPatch.mockReturnValueOnce(new Promise((r) => { resolvePatch = r }) as never)
    const p = store.completeTour()
    expect(store.completed).toBe(true)
    resolvePatch!({ onboarding_completed: true, onboarding_completed_at: null })
    await p
    store.stop()
  })

  it('prev clamps at the first step', async () => {
    const store = useTourStore()
    store.start('main', 0, fakeRouter())
    store.prev()
    expect(store.stepIndex).toBe(0)
    await store.skip()
    await flush()
  })

  it('Esc key skips the tour', async () => {
    const store = useTourStore()
    store.start('main', 0, fakeRouter())
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flush()
    await flush()
    expect(store.isActive).toBe(false)
    expect(store.completed).toBe(true)
  })

  it('walks a no-project tour through to the create-project prompt and completes', async () => {
    // Elements the create steps interact with; without them every dialog
    // waitFor times out and the tour would auto-finish past the last step.
    document.body.innerHTML =
      '<button class="empty-project-btn"></button>' +
      '<div class="modal-dialog"></div><div class="create-project-dialog"></div>'

    const store = useTourStore()
    const done = store.start('main', 0, fakeRouter())
    await store.next() // language → welcome
    await store.next() // welcome → project-cta (no project yet)

    // create-project-form lands: its action/waitFor poll the injected dialog
    const toForm = store.next()
    await vi.advanceTimersByTimeAsync(200)
    await toForm
    expect(store.stepIndex).toBe(MAIN_TOUR_STEPS.findIndex((s) => s.id === 'create-project-form'))

    // From the create form to the final step: project-created/chat/workspace
    // conditions all fail (no project), settings-models waits out the fake
    // router's route (5s), settings-integrations times out on .token-tabs
    // (8s), then create-project-prompt lands on the injected dialog.
    const walking = store.next()
    await vi.advanceTimersByTimeAsync(15000)
    await walking

    expect(store.stepIndex).toBe(MAIN_TOUR_STEPS.findIndex((s) => s.id === 'create-project-prompt'))
    expect(store.isActive).toBe(true) // waits for the user's 完成
    expect(store.completed).toBe(false)

    await store.finish()
    await flush()
    expect(store.completed).toBe(true)
    expect(store.isActive).toBe(false)
    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true })
    await expect(done).resolves.toBeUndefined()
  })

  it('with a project, next() jumps to the chat-composer step', async () => {
    seedProject()

    const store = useTourStore()
    store.start('main', 0, fakeRouter('/projects/p-1/chat'))
    await store.next() // language → welcome
    await store.next()

    // project-cta / create-project-form / project-created all skipped (user
    // already has a project) → the tour lands on chat-composer.
    expect(store.stepIndex).toBe(MAIN_TOUR_STEPS.findIndex((s) => s.id === 'chat-composer'))
    await store.skip()
    await flush()
  })

  it('stop cancels the in-flight advance so an immediate restart shows its first step', async () => {
    const store = useTourStore()
    store.start('main', 0, fakeRouter())
    await store.next() // language → welcome
    await store.next() // welcome → project-cta (no project yet)
    const walking = store.next() // advanceTo(create-project-form): waitFor pending
    store.stop()
    expect(store.isActive).toBe(false)

    // Restart must claim the advance slot — pre-fix the dead transition's
    // `advancing` flag swallowed it and the first step never came back.
    store.start('main', 0, fakeRouter())
    expect(store.stepIndex).toBe(0)

    // The zombie's wait times out and must abort without touching the
    // restarted tour (no background step change, no self-completion).
    await vi.advanceTimersByTimeAsync(9000)
    await walking
    expect(store.stepIndex).toBe(0)
    expect(mockPatch).not.toHaveBeenCalled()

    await store.skip()
    await flush()
  })

  it('prev walks back over steps the forward pass would never show', async () => {
    seedProject()

    const store = useTourStore()
    store.start('main', 0, fakeRouter('/projects/p-1/chat'))
    await store.next() // language → welcome
    await store.next()
    const chatComposer = MAIN_TOUR_STEPS.findIndex((s) => s.id === 'chat-composer')
    expect(store.stepIndex).toBe(chatComposer)

    // The create-project steps are condition-gated on having NO project (and
    // project-created additionally on coming from the create form) — a bare
    // stepIndex-- lands on one whose overlay anchors/conditions don't hold.
    // The only eligible step before chat-composer is welcome.
    store.prev()
    expect(store.stepIndex).toBe(MAIN_TOUR_STEPS.findIndex((s) => s.id === 'welcome'))

    await store.skip()
    await flush()
  })

  it('prev applies the target step\'s route before showing it', async () => {
    seedProject()

    const router = routingRouter('/projects/p-1/workspace')
    const store = useTourStore()
    const workspace = MAIN_TOUR_STEPS.findIndex((s) => s.id === 'workspace')
    store.start('main', workspace, router)
    await flush()
    expect(store.stepIndex).toBe(workspace)

    // top-nav anchors on the chat page: walking back must push its
    // route, not render its overlay over the workspace page.
    await store.prev()
    expect(store.stepIndex).toBe(MAIN_TOUR_STEPS.findIndex((s) => s.id === 'top-nav'))
    expect(router.push).toHaveBeenCalledWith('/projects/p-1/chat')

    await store.skip()
    await flush()
  })
})

describe('knowledge tour', () => {
  beforeEach(() => {
    // jsdom's default viewport sits exactly on the mobile breakpoint (1024px)
    // where every knowledge step is skipOnMobile — widen for landing tests.
    setViewportWidth(1280)
  })

  it('starts immediately when idle after a project creation', async () => {
    seedProject()
    document.body.innerHTML = '<div class="knowledge-panel"></div>'

    const store = useTourStore()
    store.notifyProjectCreated(routingRouter('/projects/p-1/chat'))
    await vi.advanceTimersByTimeAsync(200)

    expect(store.isActive).toBe(true)
    expect(store.activeTourId).toBe('knowledge')
    expect(store.stepIndex).toBe(0)
    expect(mockPatch).not.toHaveBeenCalled()

    // Completion never writes preferences — only the main tour does.
    await store.skip()
    await flush()
    expect(mockPatch).not.toHaveBeenCalled()
    expect(store.completed).toBe(false)
    expect(store.isActive).toBe(false)
  })

  it('defers while a tour is running and flushes it on stop', async () => {
    seedProject()
    document.body.innerHTML = '<div class="knowledge-panel"></div>'

    const store = useTourStore()
    store.start('main', 0, fakeRouter())
    store.notifyProjectCreated(routingRouter('/projects/p-1/chat'))
    expect(store.activeTourId).toBe('main')
    expect(store.isActive).toBe(true)

    await store.skip() // main completes → stop() flushes the pending knowledge tour
    await vi.advanceTimersByTimeAsync(200)
    expect(store.activeTourId).toBe('knowledge')
    expect(store.isActive).toBe(true)
    expect(store.stepIndex).toBe(0)

    await store.skip()
    await flush()
    expect(store.isActive).toBe(false)
    expect(mockPatch).toHaveBeenCalledTimes(1) // only the main tour's patch
  })

  it('Esc during the knowledge tour stops it without writing preferences', async () => {
    seedProject()
    document.body.innerHTML = '<div class="knowledge-panel"></div>'

    const store = useTourStore()
    store.notifyProjectCreated(routingRouter('/projects/p-1/chat'))
    await vi.advanceTimersByTimeAsync(200)
    expect(store.activeTourId).toBe('knowledge')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flush()
    await flush()
    expect(store.isActive).toBe(false)
    expect(mockPatch).not.toHaveBeenCalled()
    expect(store.completed).toBe(false)
  })

  it('falls off the end cleanly when no step qualifies, without preferences', async () => {
    const store = useTourStore()
    store.start('knowledge', 0, fakeRouter())
    await flush()

    expect(store.isActive).toBe(false)
    expect(store.activeTourId).toBeNull()
    expect(mockPatch).not.toHaveBeenCalled()
    expect(store.completed).toBe(false)
  })

  it('mobile: project creation never starts a tour', () => {
    setViewportWidth(500)
    const store = useTourStore()
    store.notifyProjectCreated(fakeRouter())
    expect(store.isActive).toBe(false)
    expect(store.activeTourId).toBeNull()
  })

  it('repeated creations while pending yield exactly one knowledge tour', async () => {
    seedProject()
    document.body.innerHTML = '<div class="knowledge-panel"></div>'

    const store = useTourStore()
    store.start('main', 0, fakeRouter())
    store.notifyProjectCreated(routingRouter('/projects/p-1/chat'))
    store.notifyProjectCreated(routingRouter('/projects/p-1/chat'))

    await store.skip()
    await vi.advanceTimersByTimeAsync(200)
    expect(store.activeTourId).toBe('knowledge')
    expect(store.isActive).toBe(true)

    // The single slot was cleared at flush — stopping the knowledge tour
    // must not start another one.
    await store.skip()
    await flush()
    expect(store.isActive).toBe(false)
    expect(store.activeTourId).toBeNull()
  })
})
