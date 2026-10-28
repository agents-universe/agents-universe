import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import type { Router } from 'vue-router'
import { patchPreferences } from '@/api/preferences'
import { useTourStore } from '@/stores/tour'
import { useProjectStore } from '@/stores/project'
import { MAIN_TOUR_STEPS } from '@/tour/steps'
import TourSpotlight from './TourSpotlight.vue'

vi.mock('@/api/preferences', () => ({ patchPreferences: vi.fn() }))

function fakeRouter(path = '/projects/p-1/chat'): Router {
  return {
    push: vi.fn().mockResolvedValue(undefined),
    currentRoute: { value: { path } },
  } as unknown as Router
}

/** A router whose push actually moves currentRoute, like the real one. */
function routingRouter(path = '/projects/p-1/chat'): Router {
  const router = {
    push: vi.fn(async (target: string) => {
      router.currentRoute.value.path = target
    }),
    currentRoute: { value: { path } },
  }
  return router as unknown as Router
}

const originalInnerWidth = window.innerWidth
function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true })
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

describe('TourSpotlight', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    setViewportWidth(originalInnerWidth)
  })

  it('finishing an anchored step with its target still on screen unmounts cleanly', async () => {
    // Regression: stop() unmounts the layer (tooltipEl ref → null) while the
    // currentStep and its target are still present. The isActive watcher then
    // ran measure() → positionTooltip() → nextTick(positionTooltip) forever,
    // freezing the page in a microtask loop (any anchored finish/skip/Esc).
    document.body.innerHTML = '<div class="agent-switcher"></div>'
    const projectStore = useProjectStore()
    projectStore.setCurrentProject({ project_id: 'p-1' } as never)
    projectStore.setProjects([{ project_id: 'p-1' }] as never)

    const store = useTourStore()
    const idx = MAIN_TOUR_STEPS.findIndex((s) => s.id === 'agent-switcher')
    store.start('main', idx, fakeRouter())
    const wrapper = mount(TourSpotlight)
    await nextTick()
    expect(document.querySelector('.tour-layer')).not.toBeNull()
    expect(document.querySelector('.agent-switcher')).not.toBeNull()

    await store.finish()
    // If measurement rescheduled itself off the unmounted ref, this loop
    // would never yield and the test would time out instead of passing.
    for (let i = 0; i < 10; i++) await nextTick()
    expect(document.querySelector('.tour-layer')).toBeNull()
    expect(patchPreferences).toHaveBeenCalledWith({ onboarding_completed: true })
    wrapper.unmount()
  })

  it("anchors a tour's first step to a target mounted by the step's own action", async () => {
    // The knowledge tour opens on knowledge-intro: stepIndex stays 0 (no
    // watcher signal), and the action's tab click mounts the panel after
    // start() — without re-measuring when the transition settles (waiting →
    // false), the tooltip fell back to centered instead of highlighting it.
    setViewportWidth(1280) // knowledge steps are skipOnMobile (jsdom: 1024)
    const projectStore = useProjectStore()
    projectStore.setCurrentProject({ project_id: 'p-1' } as never)
    projectStore.setProjects([{ project_id: 'p-1' }] as never)

    document.body.innerHTML =
      '<div class="panel-right"><button data-tour-target="panel-tab-knowledge"></button></div>'
    const tab = document.querySelector<HTMLElement>('[data-tour-target="panel-tab-knowledge"]')!
    tab.addEventListener('click', () => {
      setTimeout(() => {
        const panel = document.createElement('div')
        panel.className = 'knowledge-panel'
        document.body.appendChild(panel)
      }, 10) // stand in for the app's reactive panel render
    })

    const store = useTourStore()
    store.start('knowledge', 0, routingRouter())
    const wrapper = mount(TourSpotlight)

    // waitForSelector (50ms poll) lands the step once the panel exists.
    for (let i = 0; i < 100 && !document.querySelector('.knowledge-panel'); i++) await sleep(20)
    expect(document.querySelector('.knowledge-panel')).not.toBeNull()
    expect(store.activeTourId).toBe('knowledge')
    expect(store.stepIndex).toBe(0)

    // The waiting-flip watcher re-measures with the panel now on screen.
    for (let i = 0; i < 100 && store.waiting; i++) await sleep(20)
    expect(store.waiting).toBe(false)
    for (let i = 0; i < 5; i++) await nextTick()
    expect(document.querySelector('.tour-hole')).not.toBeNull()
    expect(document.querySelector('.tour-tooltip--center')).toBeNull()
    expect(document.querySelector('.tour-tooltip')).not.toBeNull()

    store.stop()
    wrapper.unmount()
  })
})
