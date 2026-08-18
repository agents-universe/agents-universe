import { watch, onUnmounted } from 'vue'
import type { Ref } from 'vue'
import { useKnowledgeStore } from '@/stores/knowledge'
import { knowledgeApi } from '@/api/knowledge'

export function useKnowledgeData(projectId: Ref<string | undefined>) {
  const store = useKnowledgeStore()
  let controller: AbortController | null = null

  async function load(id: string) {
    controller?.abort()
    controller = new AbortController()
    const signal = controller.signal
    try {
      const [items, completeness] = await Promise.all([
        knowledgeApi.getItems(id, false, signal),
        knowledgeApi.getCompleteness(id, signal),
      ])
      // The abort can arrive between the fetch resolving and this write —
      // a stale response for a previous project must not overwrite the
      // current project's data.
      if (signal.aborted) return
      store.setItems(items)
      store.setCompleteness(completeness)
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        console.error('Failed to load knowledge data', e)
      }
    }
  }

  const stop = watch(
    [projectId, () => store.refreshToken],
    ([id]) => { if (id) load(id) },
    { immediate: true },
  )

  onUnmounted(() => {
    stop()
    controller?.abort()
  })
}
