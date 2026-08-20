/**
 * Small waiting primitives for the tour step engine.
 * Polling (setInterval) instead of MutationObserver: the center panel
 * renders inside a `<Transition mode="out-in">`, so elements appear
 * asynchronously, and polling is deterministic under Vitest fake timers
 * (elapsed time is counted in ticks, not Date/performance.now, which fake
 * timers do not mock by default).
 */

const POLL_INTERVAL_MS = 50
const ROUTE_POLL_MS = 30

/** Poll until `selector` matches an element or the timeout elapses.
 *  Resolves the element, or `null` on timeout (callers skip the step). */
export function waitForSelector(selector: string, timeoutMs = 8000): Promise<Element | null> {
  return new Promise((resolve) => {
    let elapsed = 0
    const timer = setInterval(() => {
      const el = document.querySelector(selector)
      if (el) {
        clearInterval(timer)
        resolve(el)
        return
      }
      elapsed += POLL_INTERVAL_MS
      if (elapsed >= timeoutMs) {
        clearInterval(timer)
        resolve(null)
      }
    }, POLL_INTERVAL_MS)
  })
}

/** Replace the '{projectId}' placeholder; null project id → null (step skipped). */
export function resolveRoute(template: string, projectId: string | null): string | null {
  if (projectId == null) return null
  return template.replace('{projectId}', projectId)
}

/** Wait until the router's current path equals `path` (timeout 5s). */
export async function waitForRoute(
  path: string,
  getCurrentPath: () => string,
  timeoutMs = 5000,
): Promise<boolean> {
  if (getCurrentPath() === path) return true
  let elapsed = 0
  while (elapsed < timeoutMs) {
    await new Promise((r) => setTimeout(r, ROUTE_POLL_MS))
    elapsed += ROUTE_POLL_MS
    if (getCurrentPath() === path) return true
  }
  return false
}
