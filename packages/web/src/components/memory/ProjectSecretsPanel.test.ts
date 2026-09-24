import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import ProjectSecretsPanel from './ProjectSecretsPanel.vue'
import { useProjectStore } from '@/stores/project'
import { useProjectSecretsStore } from '@/stores/projectSecrets'
import type { Project, ProjectSecret } from '@/types'

const secretsApi = vi.hoisted(() => ({
  listProjectSecrets: vi.fn(),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))
vi.mock('@/api/projectSecrets', () => secretsApi)

function makeProject(projectId: string): Project {
  return { project_id: projectId, slug: projectId, display_name: projectId } as Project
}

function makeSecret(secretId: string): ProjectSecret {
  return {
    secret_id: secretId,
    service_key: `key-${secretId}`,
    environment: 'prod',
    display_name: secretId,
    key_hint: '***',
  } as ProjectSecret
}

describe('ProjectSecretsPanel project switch', () => {
  beforeEach(() => {
    secretsApi.listProjectSecrets.mockReset()
    secretsApi.createProjectSecret.mockReset()
    secretsApi.updateProjectSecret.mockReset()
    secretsApi.deleteProjectSecret.mockReset()
  })

  it('shows the new project secrets after a switch', async () => {
    // setCurrentProject sets currentProject synchronously but defers the
    // projectSecrets reset() behind a dynamic import().then — the panel's
    // watcher therefore starts load(B) FIRST, and the deferred reset then
    // bumps loadSeq, discarding B's in-flight response. Nothing retried, so
    // the panel stayed empty (while the DB rows were fine) until a
    // secrets_updated WS event or a panel remount.
    secretsApi.listProjectSecrets.mockImplementation(async (pid: string) =>
      pid === 'p-a' ? [makeSecret('s-a')] : [makeSecret('s-b')],
    )

    const pinia = createPinia()
    setActivePinia(pinia)
    const projectStore = useProjectStore()
    const secretsStore = useProjectSecretsStore()

    projectStore.setCurrentProject(makeProject('p-a'))
    // Let the deferred reset from the INITIAL selection land before mounting —
    // in the real app the startup selection long precedes opening the panel.
    await flushPromises()
    const wrapper = mount(ProjectSecretsPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(secretsStore.secrets.map((s) => s.secret_id)).toEqual(['s-a'])

    projectStore.setCurrentProject(makeProject('p-b'))
    await flushPromises()

    expect(secretsStore.secrets.map((s) => s.secret_id)).toEqual(['s-b'])
    wrapper.unmount()
  })
})
