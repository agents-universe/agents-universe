import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useProjectSecretsStore } from '@/stores/projectSecrets'
import * as secretsApi from '@/api/projectSecrets'
import type { ProjectSecret } from '@/types'

vi.mock('@/api/projectSecrets', () => ({
  listProjectSecrets: vi.fn(),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))

const listMock = vi.mocked(secretsApi.listProjectSecrets)
const deleteMock = vi.mocked(secretsApi.deleteProjectSecret)

function secret(id: string): ProjectSecret {
  return {
    secret_id: id,
    service_key: 'jira',
    environment: '',
    secret_name: 'default',
    display_name: null,
    key_hint: '****',
    created_by: 'u1',
    updated_at: null,
  }
}

describe('projectSecrets store seq guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    listMock.mockReset()
    deleteMock.mockReset()
  })

  it('remove reloads the DB list after a successful delete (same project)', async () => {
    listMock.mockResolvedValue([secret('s2')])
    deleteMock.mockResolvedValue(undefined)
    const store = useProjectSecretsStore()
    store.secrets = [secret('s1'), secret('s2')]

    await store.remove('proj-1', 's1')

    expect(deleteMock).toHaveBeenCalledWith('proj-1', 's1')
    // The list is refreshed from the DB, not just locally filtered.
    expect(listMock).toHaveBeenCalledWith('proj-1')
    expect(store.secrets.map((s) => s.secret_id)).toEqual(['s2'])
  })

  it('remove does not reload when the project switched mid-delete', async () => {
    deleteMock.mockImplementation(async () => {
      // A project switch (reset) bumps loadSeq while the DELETE is in flight.
      store.reset()
    })
    listMock.mockResolvedValue([secret('s9')])
    const store = useProjectSecretsStore()
    store.secrets = [secret('s1')]

    await store.remove('proj-1', 's1')

    // The seq guard skips the reload entirely: re-loading proj-1 after a
    // switch would overwrite the new project's freshly-loaded list. Without
    // the guard the old code did a blind local filter against whatever list
    // was current — the same collision the create/update guards prevent.
    expect(listMock).not.toHaveBeenCalled()
  })
})
