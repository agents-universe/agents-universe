import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { scriptsApi, type RunDetail, type RunRow } from '@/api/scripts'

/**
 * Result, artifacts and history of script runs (manual and scheduled).
 *
 * Sibling of `useScriptRunLog`: that one owns the live socket and the log
 * buffer, this one owns the *outcome* - which the log WebSocket does not carry.
 * Opening a past run goes through `loadRun()` too, so reviewing history and
 * watching a fresh run land on the same card.
 */
export function useScriptRunResult() {
  const { t } = useI18n()

  const detail = ref<RunDetail | null>(null)
  const history = ref<RunRow[]>([])
  const loading = ref(false)
  const historyLoading = ref(false)
  const error = ref<string | null>(null)

  // Monotonic guards: a slow response must not clobber a newer selection
  // (rapid clicking through history rows).
  let loadSeq = 0
  let historySeq = 0

  const result = computed(() => detail.value?.result ?? null)
  const artifacts = computed(() => detail.value?.artifacts ?? [])
  const reportUrl = computed(() => detail.value?.report_url ?? null)

  async function loadRun(runId: string): Promise<void> {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
      const data = await scriptsApi.getRun(runId)
      if (seq !== loadSeq) return
      detail.value = data
    } catch (e) {
      if (seq !== loadSeq) return
      error.value = e instanceof Error ? e.message : t('workspace.loadRunFailed')
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function loadHistory(fetchRuns: () => Promise<RunRow[]>): Promise<void> {
    const seq = ++historySeq
    historyLoading.value = true
    try {
      const rows = await fetchRuns()
      if (seq !== historySeq) return
      history.value = rows
    } catch {
      // History is a convenience: a failure here must not blank the run panel.
      if (seq === historySeq) history.value = []
    } finally {
      if (seq === historySeq) historyLoading.value = false
    }
  }

  const loadSpecHistory = (projectId: string, slug: string) =>
    loadHistory(() => scriptsApi.listSpecRuns(projectId, slug))

  const loadScriptHistory = (scriptId: string) =>
    loadHistory(() => scriptsApi.listScriptRuns(scriptId))

  function reset(): void {
    loadSeq++
    historySeq++
    detail.value = null
    history.value = []
    loading.value = false
    historyLoading.value = false
    error.value = null
  }

  return {
    detail,
    result,
    artifacts,
    reportUrl,
    history,
    loading,
    historyLoading,
    error,
    loadRun,
    loadSpecHistory,
    loadScriptHistory,
    reset,
  }
}
