import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Router } from 'vue-router'
import { patchPreferences } from '@/api/preferences'
import { useTourStore } from './tour'
import { useProjectStore } from './project'
import { TOUR_STEPS } from '@/tour/steps'

vi.mock('@/api/preferences', () => ({ patchPreferences: vi.fn() }))

const mockPatch = vi.mocked(patchPreferences)

function fakeRouter(path = '/app'): Router {
  return {
    push: vi.fn().mockResolvedValue(undefined),
    currentRoute: { value: { path } },
  } as unknown as Router
}

/** Small helper: flush pending microtasks (fake timers are active). */
const flush = () => vi.advanceTimersByTimeAsync(0)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.clearAllMocks()
})

describe('tour store', () => {
  it('setServerState seeds completed from the server', () => {
    const store = useTourStore()
    store.setServerState({ onboarding_completed: true, onboarding_completed_at: 'x' })
    expect(store.completed).toBe(true)
  })

  it('skip completes the tour: patches the server, resolves start(), stops', async () => {
    const store = useTourStore()
    const done = store.start(0, fakeRouter())
    expect(store.isActive).toBe(true)
    expect(store.stepIndex).toBe(0) // welcome step shows synchronously

    await store.skip()
    await flush()

    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true })
    expect(store.completed).toBe(true)
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('finish behaves like skip for the done step', async () => {
    const store = useTourStore()
    const done = store.start(TOUR_STEPS.length - 1, fakeRouter())
    await store.finish()
    await flush()
    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true })
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('optimistically marks completed before the PATCH resolves', async () => {
    const store = useTourStore()
    let resolvePatch: (v: unknown) => void
    mockPatch.mockReturnValueOnce(new Promise((r) => { resolvePatch = r }) as never)
    const p = store.completeTour()
    expect(store.completed).toBe(true)
    resolvePatch!({ onboarding_completed: true, onboarding_completed_at: null })
    await p
  })

  it('prev clamps at the first step', async () => {
    const store = useTourStore()
    store.start(0, fakeRouter())
    store.prev()
    expect(store.stepIndex).toBe(0)
    await store.skip()
    await flush()
  })

  it('Esc key skips the tour', async () => {
    const store = useTourStore()
    store.start(0, fakeRouter())
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flush()
    await flush()
    expect(store.isActive).toBe(false)
    expect(store.completed).toBe(true)
  })

  it('walks a no-project tour through to done and completes', async () => {
    const store = useTourStore()
    const done = store.start(0, fakeRouter())
    await store.next() // welcome → project-cta (no project yet)

    // advanceTo(2): opens (missing) dialog → waitFor times out → skip
    // advanceTo(3): project-created waits for .message-user → times out → skip
    // steps 4+ require a project → all skipped → the tour lands on 'done'
    const walking = store.next()
    await vi.advanceTimersByTimeAsync(8500)
    await vi.advanceTimersByTimeAsync(8500)
    await walking

    expect(store.stepIndex).toBe(TOUR_STEPS.length - 1)
    expect(store.isActive).toBe(true) // 'done' waits for the user click
    expect(store.completed).toBe(false)

    await store.finish()
    await flush()
    expect(store.completed).toBe(true)
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('with a project, next() jumps to the chat-composer step', async () => {
    const projectStore = useProjectStore()
    projectStore.setCurrentProject({ project_id: 'p-1' } as never)
    projectStore.setProjects([{ project_id: 'p-1' }] as never)

    const store = useTourStore()
    store.start(0, fakeRouter('/projects/p-1/chat'))
    await store.next()

    // project-cta / create-project-form / project-created all skipped (user
    // already has a project) → the tour lands on chat-composer.
    expect(store.stepIndex).toBe(4)
    await store.skip()
    await flush()
  })

  it('stop cancels the in-flight advance so an immediate restart shows its first step', async () => {
    const store = useTourStore()
    store.start(0, fakeRouter())
    await store.next() // welcome → project-cta (no project yet)
    const walking = store.next() // advanceTo(create-project-form): waitFor pending
    store.stop()
    expect(store.isActive).toBe(false)

    // Restart must claim the advance slot — pre-fix the dead transition's
    // `advancing` flag swallowed it and welcome never came back.
    store.start(0, fakeRouter())
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
    const projectStore = useProjectStore()
    projectStore.setCurrentProject({ project_id: 'p-1' } as never)
    projectStore.setProjects([{ project_id: 'p-1' }] as never)

    const store = useTourStore()
    store.start(0, fakeRouter('/projects/p-1/chat'))
    await store.next()
    expect(store.stepIndex).toBe(4) // chat-composer

    // Steps 1–3 are condition-gated on having NO project (and
    // project-created additionally on coming from the create form) — a bare
    // stepIndex-- landed on 3, whose overlay anchors/conditions don't hold.
    // The only eligible step before 4 is welcome (0).
    store.prev()
    expect(store.stepIndex).toBe(0)

    await store.skip()
    await flush()
  })
})
