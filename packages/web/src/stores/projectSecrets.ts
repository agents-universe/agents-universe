import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ProjectSecret } from '@/types'
import { listProjectSecrets, createProjectSecret, updateProjectSecret, deleteProjectSecret } from '@/api/projectSecrets'

export const useProjectSecretsStore = defineStore('projectSecrets', () => {
  const secrets = ref<ProjectSecret[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // Seq guard: rapid project A→B switching fires overlapping load()s — a
  // stale response for A must not overwrite the current project's list.
  let loadSeq = 0

  async function load(projectId: string) {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
      const list = await listProjectSecrets(projectId)
      if (seq === loadSeq) secrets.value = list
    } catch (e: unknown) {
      if (seq === loadSeq) error.value = e instanceof Error ? e.message : 'Failed to load secrets'
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function create(
    projectId: string,
    data: { service_key: string; environment?: string | null; display_name?: string; value: string },
  ) {
    const seq = loadSeq
    await createProjectSecret(projectId, data)
    // Project may have switched while the create was in flight: reset()
    // bumped loadSeq, so a fresh load(projectId) here would write the old
    // project's list over the new project's store.
    if (seq === loadSeq) await load(projectId)
  }

  async function update(projectId: string, secretId: string, data: { display_name?: string; value?: string }) {
    const seq = loadSeq
    await updateProjectSecret(projectId, secretId, data)
    if (seq === loadSeq) await load(projectId)
  }

  async function remove(projectId: string, secretId: string) {
    await deleteProjectSecret(projectId, secretId)
    secrets.value = secrets.value.filter((s) => s.secret_id !== secretId)
  }

  function reset() {
    secrets.value = []
    loading.value = false
    error.value = null
    // Invalidate any in-flight load: without this, a load() started for the
    // previous project would still match its seq and write the old project's
    // secret list into the freshly reset store.
    loadSeq++
  }

  return { secrets, loading, error, load, create, update, remove, reset }
})
