import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useSchedulesStore } from '@/stores/schedules'
import { schedulesApi } from '@/api/schedules'
import type { ScheduledTask } from '@/api/schedules'

vi.mock('@/api/schedules', () => ({
  schedulesApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    runNow: vi.fn(),
  },
}))

const listMock = vi.mocked(schedulesApi.list)
const updateMock = vi.mocked(schedulesApi.update)

function task(id: string): ScheduledTask {
  return {
    schedule_id: id,
    project_id: 'p1',
    name: id,
    description: null,
    target_type: 'script',
    script_id: 's1',
    spec_slug: null,
    agent_slug: null,
    prompt: null,
    env: {},
    conversation_id: null,
    cron_expr: '0 9 * * *',
    schedule: '每天 09:00',
    timezone: 'Asia/Shanghai',
    enabled: true,
    notify: true,
    next_run_at: null,
    last_run_at: null,
    last_status: null,
    created_at: null,
  }
}

describe('schedules store mutation reload guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('a second mutation still reloads while another mutation\'s reload is in flight', async () => {
    // load() bumps the load sequence on every call; comparing a mutation
    // against it made the second of two overlapping toggles skip its reload,
    // so its row could snap back to the pre-toggle state.
    let resolveSecond!: (value: ScheduledTask) => void
    const secondUpdate = new Promise<ScheduledTask>((resolve) => { resolveSecond = resolve })
    updateMock.mockImplementation((scheduleId: string) =>
      scheduleId === 'b' ? secondUpdate : Promise.resolve(task('a')))
    listMock.mockResolvedValue([task('a'), task('b')])
    const store = useSchedulesStore()

    const first = store.setEnabled('p1', 'a', true)
    const second = store.setEnabled('p1', 'b', true)
    await first
    expect(listMock).toHaveBeenCalledTimes(1)

    resolveSecond(task('b'))
    await second
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('a project reset mid-mutation skips the reload', async () => {
    updateMock.mockImplementation(async () => {
      store.reset()
      return task('a')
    })
    listMock.mockResolvedValue([task('a')])
    const store = useSchedulesStore()

    await store.setEnabled('p1', 'a', true)

    // Reloading the old project after reset() would overwrite the new
    // project's store.
    expect(listMock).not.toHaveBeenCalled()
  })
})
