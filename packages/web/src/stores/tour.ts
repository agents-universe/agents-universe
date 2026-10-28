/**
 * Tour orchestration store.
 *
 * Tours are driven by the declarative registry in `src/tour/steps.ts`.
 * `start(tourId, …)` returns a promise that resolves when that tour stops
 * (finish / skip / Esc). Only the `main` tour persists completion
 * (`/api/preferences`, optimistic writes with console.warn on failure — a
 * failed patch means the tour re-runs next login, acceptable self-healing);
 * the `knowledge` tour is fire-and-forget, triggered by project creation.
 *
 * A project created while any tour runs defers its knowledge tour into a
 * single pending slot, flushed when the active tour stops — the two tours
 * never overlap.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { Router } from 'vue-router'
import { patchPreferences, type UserPreferences } from '@/api/preferences'
import { resolveRoute, waitForRoute, waitForSelector } from '@/tour/engine'
import { TOURS, currentProjectId, isMobile, type TourStep, type TourId } from '@/tour/steps'

let activeRouter: Router | null = null

export const useTourStore = defineStore('tour', () => {
  const isActive = ref(false)
  const stepIndex = ref(0)
  /** A step transition (route push / waitFor / action) is in flight. */
  const waiting = ref(false)
  const completed = ref(false)
  const activeTourId = ref<TourId | null>(null)

  /** Single deferred slot: a project was created while a tour was running. */
  let pendingTourId: TourId | null = null

  const activeSteps = computed<TourStep[]>(() => TOURS[activeTourId.value ?? 'main'])

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
    return activeSteps.value[stepIndex.value] ?? null
  }

  /**
   * Start (or resume) a tour. Resolves once the tour ends via
   * finish/skip/Esc. `fromIndex` re-runs the same step conditions.
   */
  function start(tourId: TourId = 'main', fromIndex = 0, router?: Router): Promise<void> {
    if (router) activeRouter = router
    if (stopPromise) return stopPromise
    // Claim the tour id and index eagerly: currentStep() must index the new
    // tour's step array before advanceTo lands its first step.
    activeTourId.value = tourId
    stepIndex.value = fromIndex
    isActive.value = true
    installKeydown()
    lockBodyScroll()
    stopPromise = new Promise<void>((resolve) => {
      stopResolve = resolve
    })
    // Advance evaluates conditions + routes; the tour always terminates by
    // running past the last step → finish().
    void advanceTo(fromIndex)
    return stopPromise
  }

  async function next() {
    if (!isActive.value || advancing) return
    await advanceTo(stepIndex.value + 1)
  }

  async function prev() {
    if (!isActive.value || advancing) return
    advancing = true
    waiting.value = true
    const gen = tourGen
    const aborted = () => gen !== tourGen
    try {
      let index = stepIndex.value - 1
      // Walk back over steps the forward pass would never have shown (mobile
      // skips, unmet or sequence-bound conditions) — landing on one renders an
      // overlay whose anchors/conditions don't hold. No eligible step back:
      // stay put rather than show a broken one.
      while (index >= 0) {
        if (aborted()) return
        const step = activeSteps.value[index]
        if (step.skipOnMobile && isMobile()) {
          index--
          continue
        }
        // Same context the forward pass gives it: the step being left.
        const prevStepId = activeSteps.value[stepIndex.value]?.id ?? null
        if (step.condition && !step.condition({ prevStepId })) {
          index--
          continue
        }
        if (!step.route && !step.action && !step.waitFor) {
          stepIndex.value = index
          return
        }
        // Going back crosses pages and panel tabs too: the step's route,
        // action and anchor wait are as necessary here as when moving
        // forward, otherwise the tooltip points at an element the current
        // page/tab does not render.
        const outcome = await activateStep(step, aborted)
        if (outcome === 'aborted') return
        if (outcome === 'skip') {
          index--
          continue
        }
        stepIndex.value = index
        return
      }
    } finally {
      if (!aborted()) {
        waiting.value = false
        advancing = false
      }
    }
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
    // Only the main tour is persisted — the knowledge tour finishing (or
    // every step skipping on mobile) must never touch preferences.
    if (activeTourId.value !== 'main') return
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
    activeTourId.value = null
    const resolve = stopResolve
    stopResolve = null
    stopPromise = null
    resolve?.()
    // Single flush point (finish/skip/Esc/advanceTo fall-off all end here):
    // a project created while the tour ran gets its knowledge tour now.
    const pending = pendingTourId
    pendingTourId = null
    if (pending) void start(pending, 0, activeRouter ?? undefined)
  }

  /**
   * Called by both create dialogs after a successful creation — starts the
   * knowledge tour, or defers it until the currently running tour stops.
   */
  function notifyProjectCreated(router?: Router) {
    if (router) activeRouter = router
    if (isActive.value) {
      pendingTourId = 'knowledge' // latest creation wins — single slot
      return
    }
    if (isMobile()) return // every knowledge step is skipOnMobile — don't flash
    void start('knowledge', 0, activeRouter ?? undefined)
  }

  /**
   * Apply a step's navigation, side effect and anchor wait before it shows.
   * Returns 'skip' when the step cannot be shown, 'aborted' when the tour was
   * stopped mid-transition.
   */
  async function activateStep(
    step: TourStep, aborted: () => boolean,
  ): Promise<'ok' | 'skip' | 'aborted'> {
    if (step.route) {
      const path = resolveRoute(step.route, currentProjectId())
      if (!path) {
        console.warn(`tour: skipping "${step.id}" — no project to route to`)
        return 'skip'
      }
      if (!activeRouter) {
        console.warn(`tour: skipping "${step.id}" — no router available`)
        return 'skip'
      }
      await activeRouter.push(path)
      if (aborted()) return 'aborted'
      const reached = await waitForRoute(path, () => activeRouter!.currentRoute.value.path)
      if (aborted()) return 'aborted'
      if (!reached) {
        console.warn(`tour: skipping "${step.id}" — route "${path}" never became current`)
        return 'skip'
      }
    }
    if (step.action) {
      await step.action({ router: activeRouter!, projectId: currentProjectId() })
      if (aborted()) return 'aborted'
    }
    if (step.waitFor) {
      const el = await waitForSelector(step.waitFor)
      if (aborted()) return 'aborted'
      if (!el) {
        console.warn(`tour: skipping "${step.id}" — "${step.waitFor}" never appeared`)
        return 'skip'
      }
    }
    return 'ok'
  }

  /** Move to `index` (or the next eligible step after it). */
  async function advanceTo(index: number) {
    if (advancing) return
    advancing = true
    waiting.value = true
    const gen = tourGen
    const aborted = () => gen !== tourGen
    try {
      while (index < activeSteps.value.length) {
        if (aborted()) return
        const step = activeSteps.value[index]
        if (step.skipOnMobile && isMobile()) {
          console.warn(`tour: skipping "${step.id}" — mobile layout`)
          index++
          continue
        }
        // Conditions see the step the tour is leaving — e.g. 'project-created'
        // only shows when the create-project form was actually displayed.
        const prevStepId = activeSteps.value[stepIndex.value]?.id ?? null
        if (step.condition && !step.condition({ prevStepId })) {
          console.warn(`tour: skipping "${step.id}" — condition not met`)
          index++
          continue
        }
        // Steps with nothing to apply land synchronously: start() must leave
        // the first step visible before returning, and next()/prev() must not
        // bounce off the in-flight guard on a step that never awaited.
        if (!step.route && !step.action && !step.waitFor) {
          stepIndex.value = index
          return
        }
        const outcome = await activateStep(step, aborted)
        if (outcome === 'aborted') return
        if (outcome === 'skip') {
          index++
          continue
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
      void prev()
    }
  }

  return {
    isActive,
    stepIndex,
    waiting,
    completed,
    activeTourId,
    activeSteps,
    setServerState,
    currentStep,
    start,
    next,
    prev,
    skip,
    finish,
    completeTour,
    stop,
    notifyProjectCreated,
  }
})
