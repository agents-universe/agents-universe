/**
 * Tour orchestration store.
 *
 * The tour is driven by the declarative registry in `src/tour/steps.ts`.
 * `start()` returns a promise that resolves when the tour stops (finish /
 * skip / Esc).
 *
 * Persistence is server-side (`/api/preferences`); all local writes are
 * optimistic with a console.warn on failure — a failed patch means the tour
 * re-runs next login, which is acceptable self-healing.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Router } from 'vue-router'
import { patchPreferences, type UserPreferences } from '@/api/preferences'
import { resolveRoute, waitForRoute, waitForSelector } from '@/tour/engine'
import { TOUR_STEPS, currentProjectId, isMobile, type TourStep } from '@/tour/steps'

let activeRouter: Router | null = null

export const useTourStore = defineStore('tour', () => {
  const isActive = ref(false)
  const stepIndex = ref(0)
  /** A step transition (route push / waitFor / action) is in flight. */
  const waiting = ref(false)
  const completed = ref(false)

  let stopPromise: Promise<void> | null = null
  let stopResolve: (() => void) | null = null
  let advancing = false
  // Bumped by stop() so an in-flight advanceTo aborts at its next checkpoint
  // instead of finishing in the background (route pushes and even completeTour
  // after the user closed the tour).
  let tourGen = 0

  function setServerState(prefs: UserPreferences) {
    completed.value = prefs.onboarding_completed
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
    if (!isActive.value || advancing) return
    let index = stepIndex.value - 1
    // Walk back over steps the forward pass would never have shown (mobile
    // skips, unmet or sequence-bound conditions) — landing on one renders an
    // overlay whose anchors/conditions don't hold. No eligible step back:
    // stay put rather than show a broken one.
    while (index >= 0) {
      const step = TOUR_STEPS[index]
      if (step.skipOnMobile && isMobile()) {
        index--
        continue
      }
      // Same context the forward pass gives it: the step being left.
      const prevStepId = TOUR_STEPS[stepIndex.value]?.id ?? null
      if (step.condition && !step.condition({ prevStepId })) {
        index--
        continue
      }
      break
    }
    if (index >= 0) stepIndex.value = index
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
    try {
      await patchPreferences({ onboarding_completed: true })
    } catch (err) {
      console.warn('tour: failed to persist completion', err)
    }
  }

  function stop() {
    if (!isActive.value) return
    tourGen += 1 // cancel the in-flight advanceTo, if any
    isActive.value = false
    // The zombie loop checks the generation before touching these — and an
    // immediate start() must be able to claim the slot right away instead
    // of being swallowed by the dead transition's flags.
    advancing = false
    waiting.value = false
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
    const gen = tourGen
    const aborted = () => gen !== tourGen
    try {
      while (index < TOUR_STEPS.length) {
        if (aborted()) return
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
          if (aborted()) return
          const reached = await waitForRoute(path, () => activeRouter!.currentRoute.value.path)
          if (aborted()) return
          if (!reached) {
            console.warn(`tour: skipping "${step.id}" — route "${path}" never became current`)
            index++
            continue
          }
        }
        if (step.action) {
          await step.action({ router: activeRouter!, projectId: currentProjectId() })
          if (aborted()) return
        }
        if (step.waitFor) {
          const el = await waitForSelector(step.waitFor)
          if (aborted()) return
          if (!el) {
            console.warn(`tour: skipping "${step.id}" — "${step.waitFor}" never appeared`)
            index++
            continue
          }
        }
        if (aborted()) return
        stepIndex.value = index
        return
      }
      // Advanced past the last step → the tour is over.
      if (aborted()) return
      await finish()
    } finally {
      // stop() already reset the flags for a possible restart; a zombie of a
      // past generation must never clobber the replacement loop's state.
      if (!aborted()) {
        waiting.value = false
        advancing = false
      }
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
    setServerState,
    currentStep,
    start,
    next,
    prev,
    skip,
    finish,
    completeTour,
    stop,
  }
})
