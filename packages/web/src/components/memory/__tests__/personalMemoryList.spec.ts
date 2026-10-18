import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PersonalMemoryList from '../PersonalMemoryList.vue'
import { useProjectStore } from '@/stores/project'
import { useMemoryStore } from '@/stores/memory'
import { memoriesApi } from '@/api/memories'

vi.mock('@/api/memories', () => ({
  memoriesApi: { createPersonal: vi.fn(), archivePersonal: vi.fn() },
}))

const createPersonal = vi.mocked(memoriesApi.createPersonal)

describe('PersonalMemoryList', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().currentProject = { project_id: 'p-1' } as never
    vi.clearAllMocks()
  })

  it('posts once when Save is clicked while the first request is in flight', async () => {
    // The form text stays until the response lands, so a double click posted
    // the same memory twice.
    let resolve!: (v: never) => void
    createPersonal.mockReturnValue(new Promise((r) => { resolve = r as never }))

    const wrapper = mount(PersonalMemoryList)
    await wrapper.find('.section-label .icon-btn').trigger('click')
    await wrapper.find('textarea').setValue('偏好中文回答')

    const save = wrapper.find('.btn-primary')
    await save.trigger('click')
    await save.trigger('click')
    expect(createPersonal).toHaveBeenCalledTimes(1)

    resolve({
      memory_id: 'm-1',
      content: '偏好中文回答',
      tags: [],
      created_by: 'user',
      created_at: '2026-01-01T00:00:00Z',
    } as never)
    await flushPromises()

    expect(useMemoryStore().personalMemories).toHaveLength(1)
    wrapper.unmount()
  })
})
