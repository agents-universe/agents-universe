import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { RunningCounts } from '@/types'
import { conversationsApi } from '@/api/conversations'

/** Running-turn counts for the sidebar badges (per project / per agent slug).
 *  Fed by `GET /api/running-counts` on load and the `/ws/user` push after. */
export const useRunningCountsStore = defineStore('runningCounts', () => {
  const byProject = ref<Record<string, number>>({})
  const byAgent = ref<Record<string, number>>({})

  function setCounts(counts: RunningCounts) {
    byProject.value = counts.by_project ?? {}
    byAgent.value = counts.by_agent ?? {}
  }

  /** Initial load / WS-less fallback. Best-effort: a failed fetch leaves the
   *  previous counts in place; the next `running_counts` frame corrects it. */
  async function fetch() {
    try {
      setCounts(await conversationsApi.getRunningCounts())
    } catch {
      /* silent — badges degrade to hidden, never a user-facing error */
    }
  }

  const projectCount = (projectId: string) => byProject.value[projectId] ?? 0
  const agentCount = (agentSlug: string) => byAgent.value[agentSlug] ?? 0

  return { byProject, byAgent, setCounts, fetch, projectCount, agentCount }
})
