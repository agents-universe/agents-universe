/**
 * Tour + what's-new orchestration store.
 *
 * The tour is driven by the declarative registry in `src/tour/steps.ts`.
 * `start()` returns a promise that resolves when the tour stops (finish /
 * skip / Esc), which lets App.vue sequence tour → what's-new deterministically.
 *
 * Persistence is server-side (`/api/preferences`); all local writes are
 * optimistic with a console.warn on failure — a failed patch means the tour
 * re-runs next login, which is acceptable self-healing.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Router } from 'vue-router'
import { patchPreferences, type UserPreferences } from '@/api/preferences'
import { CURRENT_VERSION } from '@/tour/releases'
import { resolveRoute, waitForRoute, waitForSelector } from '@/tour/engine'
import { TOUR_STEPS, currentProjectId, isMobile, type TourStep } from '@/tour/steps'

let activeRouter: Router | null = null

export const useTourStore = defineStore('tour', () => {
  const isActive = ref(false)
  const stepIndex = ref(0)
  /** A step transition (route push / waitFor / action) is in flight. */
  const waiting = ref(false)
  const completed = ref(false)
  const lastSeenVersion = ref<string | null>(null)
  const whatsNewVisible = ref(false)

  let stopPromise: Promise<void> | null = null
  let stopResolve: (() => void) | null = null
  let advancing = false

  function setServerState(prefs: UserPreferences) {
    completed.value = prefs.onboarding_completed
    lastSeenVersion.value = prefs.last_seen_version
  }

  function currentStep(): TourStep | null {
    return TOUR_STEPS[stepIndex.value] ?? null
  }

  /**
   * Start (or resume) the tour. Resolves once the tour ends via
   * finish/skip/Esc. `fromIndex` re-runs the same step conditions.
   */
  function start(fromIndex = 0, router?: Router): Promise<void> {
    if (router) activeRouter = router
    if (stopPromise) return stopPromise
    isActive.value = true
    installKeydown()
    lockBodyScroll()
    stopPromise = new Promise<void>((resolve) => {
      stopResolve = resolve
    })
    // Advance evaluates conditions + routes; the tour always terminates at
    // the 'done' step (or by skipping past the end → finish()).
    void advanceTo(fromIndex)
    return stopPromise
  }

  async function next() {
    if (!isActive.value || advancing) return
    await advanceTo(stepIndex.value + 1)
  }

  function prev() {
    if (!isActive.value) return
    stepIndex.value = Math.max(0, stepIndex.value - 1)
  }

  /** Skip the rest of the tour — marks the user as onboarded. */
  async function skip() {
    if (!isActive.value) return
    await completeTour()
    stop()
  }

  /** Finish at the final step — marks the user as onboarded. */
  async function finish() {
    if (!isActive.value) return
    await completeTour()
    stop()
  }

  async function completeTour() {
    completed.value = true
    if (CURRENT_VERSION) lastSeenVersion.value = CURRENT_VERSION
    try {
      await patchPreferences({
        onboarding_completed: true,
        ...(CURRENT_VERSION ? { last_seen_version: CURRENT_VERSION } : {}),
      })
    } catch (err) {
      console.warn('tour: failed to persist completion', err)
    }
  }

  async function dismissWhatsNew() {
    whatsNewVisible.value = false
    if (CURRENT_VERSION) {
      lastSeenVersion.value = CURRENT_VERSION
      try {
        await patchPreferences({ last_seen_version: CURRENT_VERSION })
      } catch (err) {
        console.warn('tour: failed to persist last_seen_version', err)
      }
    }
  }

  function stop() {
    if (!isActive.value) return
    isActive.value = false
    uninstallKeydown()
    unlockBodyScroll()
    const resolve = stopResolve
    stopResolve = null
    stopPromise = null
    resolve?.()
  }

  /** Move to `index` (or the next eligible step after it). */
  async function advanceTo(index: number) {
    if (advancing) return
    advancing = true
    waiting.value = true
    try {
      while (index < TOUR_STEPS.length) {
        const step = TOUR_STEPS[index]
        if (step.skipOnMobile && isMobile()) {
          console.warn(`tour: skipping "${step.id}" — mobile layout`)
          index++
          continue
        }
        // Conditions see the step the tour is leaving — e.g. 'project-created'
        // only shows when the create-project form was actually displayed.
        const prevStepId = TOUR_STEPS[stepIndex.value]?.id ?? null
        if (step.condition && !step.condition({ prevStepId })) {
          console.warn(`tour: skipping "${step.id}" — condition not met`)
          index++
          continue
        }
        if (step.route) {
          const path = resolveRoute(step.route, currentProjectId())
          if (!path) {
            console.warn(`tour: skipping "${step.id}" — no project to route to`)
            index++
            continue
          }
          if (!activeRouter) {
            console.warn(`tour: skipping "${step.id}" — no router available`)
            index++
            continue
          }
          await activeRouter.push(path)
          const reached = await waitForRoute(path, () => activeRouter!.currentRoute.value.path)
          if (!reached) {
            console.warn(`tour: skipping "${step.id}" — route "${path}" never became current`)
            index++
            continue
          }
        }
        if (step.action) {
          await step.action({ router: activeRouter!, projectId: currentProjectId() })
        }
        if (step.waitFor) {
          const el = await waitForSelector(step.waitFor)
          if (!el) {
            console.warn(`tour: skipping "${step.id}" — "${step.waitFor}" never appeared`)
            index++
            continue
          }
        }
        stepIndex.value = index
        return
      }
      // Advanced past the last step → the tour is over.
      await finish()
    } finally {
      waiting.value = false
      advancing = false
    }
  }

  function lockBodyScroll() {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    ;(document.body as HTMLElement & { _tourPrevOverflow?: string })._tourPrevOverflow = prev
  }

  function unlockBodyScroll() {
    const body = document.body as HTMLElement & { _tourPrevOverflow?: string }
    body.style.overflow = body._tourPrevOverflow ?? ''
    delete body._tourPrevOverflow
  }

  function installKeydown() {
    window.addEventListener('keydown', onKeydown)
  }

  function uninstallKeydown() {
    window.removeEventListener('keydown', onKeydown)
  }

  function onKeydown(e: KeyboardEvent) {
    if (!isActive.value) return
    if (e.key === 'Escape') {
      e.preventDefault()
      void skip()
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      void next()
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault()
      prev()
    }
  }

  return {
    isActive,
    stepIndex,
    waiting,
    completed,
    lastSeenVersion,
    whatsNewVisible,
    setServerState,
    currentStep,
    start,
    next,
    prev,
    skip,
    finish,
    completeTour,
    dismissWhatsNew,
    stop,
  }
})
