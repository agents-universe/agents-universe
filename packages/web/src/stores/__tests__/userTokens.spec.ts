import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useUserTokensStore } from '@/stores/userTokens'
import { userTokensApi } from '@/api/userTokens'
import type { UserTokenEntry } from '@/types'

vi.mock('@/api/userTokens', () => ({
  userTokensApi: {
    list: vi.fn(),
    upsert: vi.fn(),
    remove: vi.fn(),
  },
}))

const listMock = vi.mocked(userTokensApi.list)
const removeMock = vi.mocked(userTokensApi.remove)

function token(serviceKey: string): UserTokenEntry {
  return {
    service_key: serviceKey,
    display_name: null,
    key_hint: '****',
    base_url: null,
    model_id: null,
  }
}

describe('userTokens store seq guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    listMock.mockReset()
    removeMock.mockReset()
  })

  it('remove reloads the DB list after a successful delete', async () => {
    listMock.mockResolvedValue([token('gh')])
    removeMock.mockResolvedValue({ deleted: 'jira' })
    const store = useUserTokensStore()
    store.tokens = [token('jira'), token('gh')]

    await store.remove('jira')

    expect(removeMock).toHaveBeenCalledWith('jira')
    // The list is refreshed from the DB, not just locally filtered — a
    // blind filter let an in-flight load() resolve afterwards and write the
    // deleted row back.
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(store.tokens.map((t) => t.service_key)).toEqual(['gh'])
  })

  it('remove does not reload when reset() happened mid-delete', async () => {
    const store = useUserTokensStore()
    removeMock.mockImplementation(async () => {
      // A project switch (reset) bumps loadSeq while the DELETE is in flight.
      store.reset()
      return { deleted: 'jira' }
    })
    listMock.mockResolvedValue([token('jira')])

    store.tokens = [token('jira')]
    await store.remove('jira')

    // The seq guard skips the reload: refetching would resurrect the token
    // into the freshly reset (cleared) store.
    expect(listMock).not.toHaveBeenCalled()
    expect(store.tokens).toEqual([])
  })
})
