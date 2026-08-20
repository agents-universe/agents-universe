import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Router } from 'vue-router'
import { patchPreferences } from '@/api/preferences'
import { useTourStore } from './tour'
import { useProjectStore } from './project'
import { TOUR_STEPS } from '@/tour/steps'

vi.mock('@/api/preferences', () => ({ patchPreferences: vi.fn() }))
vi.mock('@/tour/releases', () => ({
  RELEASES: [],
  CURRENT_VERSION: '0.4.0',
}))

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
  it('setServerState seeds completed + lastSeenVersion from the server', () => {
    const store = useTourStore()
    store.setServerState({ onboarding_completed: true, onboarding_completed_at: 'x', last_seen_version: '0.3.0' })
    expect(store.completed).toBe(true)
    expect(store.lastSeenVersion).toBe('0.3.0')
  })

  it('skip completes the tour: patches the server, resolves start(), stops', async () => {
    const store = useTourStore()
    const done = store.start(0, fakeRouter())
    expect(store.isActive).toBe(true)
    expect(store.stepIndex).toBe(0) // welcome step shows synchronously

    await store.skip()
    await flush()

    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true, last_seen_version: '0.4.0' })
    expect(store.completed).toBe(true)
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('finish behaves like skip for the done step', async () => {
    const store = useTourStore()
    const done = store.start(TOUR_STEPS.length - 1, fakeRouter())
    await store.finish()
    await flush()
    expect(mockPatch).toHaveBeenCalledWith({ onboarding_completed: true, last_seen_version: '0.4.0' })
    expect(store.isActive).toBe(false)
    await expect(done).resolves.toBeUndefined()
  })

  it('dismissWhatsNew patches only last_seen_version and hides the dialog', async () => {
    const store = useTourStore()
    store.whatsNewVisible = true
    await store.dismissWhatsNew()
    expect(mockPatch).toHaveBeenCalledWith({ last_seen_version: '0.4.0' })
    expect(mockPatch).not.toHaveBeenCalledWith(expect.objectContaining({ onboarding_completed: expect.anything() }))
    expect(store.whatsNewVisible).toBe(false)
    expect(store.lastSeenVersion).toBe('0.4.0')
  })

  it('optimistically marks completed before the PATCH resolves', async () => {
    const store = useTourStore()
    let resolvePatch: (v: unknown) => void
    mockPatch.mockReturnValueOnce(new Promise((r) => { resolvePatch = r }) as never)
    const p = store.completeTour()
    expect(store.completed).toBe(true)
    expect(store.lastSeenVersion).toBe('0.4.0')
    resolvePatch!({ onboarding_completed: true, onboarding_completed_at: null, last_seen_version: '0.4.0' })
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
})
