import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useProjectMembersStore } from '@/stores/projectMembers'
import * as membersApi from '@/api/projectMembers'
import type { ProjectMember } from '@/types'

vi.mock('@/api/projectMembers', () => ({
  listProjectMembers: vi.fn(),
  addProjectMember: vi.fn(),
  removeProjectMember: vi.fn(),
}))

const listMock = vi.mocked(membersApi.listProjectMembers)
const addMock = vi.mocked(membersApi.addProjectMember)
const removeMock = vi.mocked(membersApi.removeProjectMember)

function member(userId: string): ProjectMember {
  return { user_id: userId, added_by: null, created_at: null }
}

describe('projectMembers store mutation guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('a removal still updates the list while an add\'s reload is in flight', async () => {
    // load() bumps the load sequence, so comparing a remove against it made
    // the remove skip its local update and leave the removed row visible.
    let resolveRemove!: () => void
    const removePending = new Promise<void>((resolve) => { resolveRemove = resolve })
    addMock.mockResolvedValue(undefined)
    removeMock.mockReturnValue(removePending)
    listMock.mockResolvedValue([member('u1'), member('u2')])
    const store = useProjectMembersStore()
    store.members = [member('u1'), member('u2')]

    const adding = store.add('p1', 'u1')
    const removing = store.remove('p1', 'u2')
    await adding
    expect(listMock).toHaveBeenCalledTimes(1)

    resolveRemove()
    await removing
    expect(store.members.map((m) => m.user_id)).toEqual(['u1'])
  })
})
