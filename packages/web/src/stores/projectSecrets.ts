import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ProjectSecret } from '@/types'
import { listProjectSecrets, createProjectSecret, updateProjectSecret, deleteProjectSecret } from '@/api/projectSecrets'
import { i18n } from '@/i18n'
import { translateApiErrorWith } from '@/utils/apiError'

export const useProjectSecretsStore = defineStore('projectSecrets', () => {
  const secrets = ref<ProjectSecret[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  // Bumped by reset() so mounted views can tell "cleared on purpose" from
  // "cleared while MY load for the current project was in flight" — the
  // deferred project-switch reset() invalidates the panel's load, and without
  // a re-trigger nothing ever refetched (empty panel until a WS event).
  const resetToken = ref(0)

  // Seq guard: rapid project A→B switching fires overlapping load()s — a
  // stale response for A must not overwrite the current project's list.
  let loadSeq = 0
  // Bumped only by reset(): load() bumps loadSeq on every call, so comparing
  // against it would make a mutation skip its reload whenever another
  // mutation's reload was already in flight.
  let resetEpoch = 0

  async function load(projectId: string) {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
      const list = await listProjectSecrets(projectId)
      if (seq === loadSeq) secrets.value = list
    } catch (e: unknown) {
      if (seq === loadSeq) error.value = translateApiErrorWith(e, i18n.global.t('common.loadFailed'))
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function create(
    projectId: string,
    data: { service_key: string; environment?: string | null; display_name?: string; value: string },
  ) {
    const epoch = resetEpoch
    await createProjectSecret(projectId, data)
    // Project may have switched while the create was in flight: reset()
    // bumped the epoch, so a fresh load(projectId) here would write the old
    // project's list over the new project's store.
    if (epoch === resetEpoch) await load(projectId)
  }

  async function update(projectId: string, secretId: string, data: { display_name?: string; value?: string }) {
    const epoch = resetEpoch
    await updateProjectSecret(projectId, secretId, data)
    if (epoch === resetEpoch) await load(projectId)
  }

  async function remove(projectId: string, secretId: string) {
    const epoch = resetEpoch
    await deleteProjectSecret(projectId, secretId)
    // Same epoch guard as create/update: a project switch while the DELETE
    // was in flight must not let this reload run against the NEW project's
    // list. Reloading the DB list is also strictly more correct than a blind
    // local filter (it cannot leave a stale row behind).
    if (epoch === resetEpoch) await load(projectId)
  }

  function reset() {
    secrets.value = []
    loading.value = false
    error.value = null
    // Invalidate any in-flight load: without this, a load() started for the
    // previous project would still match its seq and write the old project's
    // secret list into the freshly reset store.
    loadSeq++
    resetEpoch++
    resetToken.value++
  }

  return { secrets, loading, error, resetToken, load, create, update, remove, reset }
})
