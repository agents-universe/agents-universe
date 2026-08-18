import { watch, onUnmounted } from 'vue'
import type { Ref } from 'vue'
import { useMemoryStore } from '@/stores/memory'
import { memoriesApi } from '@/api/memories'

export function useMemoryData(projectId: Ref<string | undefined>) {
  const store = useMemoryStore()
  let controller: AbortController | null = null

  async function load(id: string) {
    controller?.abort()
    controller = new AbortController()
    const signal = controller.signal
    try {
      const [personal, episodic] = await Promise.all([
        memoriesApi.getPersonal(id, signal),
        memoriesApi.getEpisodic(id, signal),
      ])
      // The abort can arrive between the fetch resolving and this write —
      // a stale response for a previous project must not overwrite the
      // current project's data.
      if (signal.aborted) return
      store.setPersonalMemories(personal)
      store.setEpisodes(episodic)
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        console.error('Failed to load memory data', e)
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
