import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import SchedulesPage from '@/pages/SchedulesPage.vue'
import type { ScheduledTask } from '@/api/schedules'
import { apiFetch } from '@/api/client'

const route = vi.hoisted(() => ({ params: { projectId: 'p-1' } }))
vi.mock('vue-router', () => ({ useRoute: () => route }))
vi.mock('@/utils/basePath', () => ({ apiBase: '' }))

vi.mock('@/api/client', () => ({ apiFetch: vi.fn() }))
const apiFetchMock = vi.mocked(apiFetch) as unknown as ReturnType<typeof vi.fn>

const schedulesApi = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  preview: vi.fn(),
  get: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  runNow: vi.fn(),
  runs: vi.fn(),
}))
vi.mock('@/api/schedules', () => ({ schedulesApi }))

const conversationsApi = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api/conversations', () => ({ conversationsApi }))

const agentStore = vi.hoisted(() => ({
  agents: [] as Array<{ slug: string; label: string }>,
  fetchAgents: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({ useAgentStore: () => agentStore }))

function makeTask(over: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    schedule_id: 's-1',
    project_id: 'p-1',
    name: '每日数据汇总',
    description: null,
    target_type: 'script',
    script_id: 'sc-1',
    spec_slug: null,
    agent_slug: null,
    prompt: null,
    env: {},
    conversation_id: null,
    cron_expr: '0 9 * * *',
    schedule: 'at 09:00 every day',
    timezone: 'Asia/Shanghai',
    enabled: true,
    notify: true,
    next_run_at: '2026-09-10T01:00:00+00:00',
    last_run_at: null,
    last_status: null,
    created_at: '2026-09-09T00:00:00+00:00',
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  route.params = { projectId: 'p-1' }
  agentStore.agents = []
  agentStore.fetchAgents.mockResolvedValue(undefined)
  schedulesApi.list.mockResolvedValue([])
  schedulesApi.preview.mockResolvedValue({ description: 'at 09:00 every day', next_runs: [] })
  schedulesApi.runs.mockResolvedValue([])
  conversationsApi.list.mockResolvedValue([])
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/projects/p-1/scripts')) {
      return [{ script_id: 'sc-1', name: '每日汇总', script_type: 'python' }]
    }
    if (url.startsWith('/api/projects/p-1/playwright/specs')) {
      return [{ slug: 'login-flow', title: 'Login flow', file: 'tests/generated/login-flow.spec.ts' }]
    }
    return []
  })
})

describe('SchedulesPage', () => {
  it('lists the project scheduled tasks', async () => {
    schedulesApi.list.mockResolvedValue([
      makeTask({ schedule_id: 's-1', name: '每日数据汇总' }),
      makeTask({ schedule_id: 's-2', name: '每周回归', target_type: 'playwright', spec_slug: 'login-flow', script_id: null }),
    ])
    const wrapper = mount(SchedulesPage)
    await flushPromises()

    expect(schedulesApi.list).toHaveBeenCalledWith('p-1')
    const cards = wrapper.findAll('.schedule-card')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toContain('每日数据汇总')
    expect(cards[0].text()).toContain('at 09:00 every day')
    expect(cards[1].text()).toContain('login-flow')
  })

  it('shows the empty state when the project has no tasks', async () => {
    const wrapper = mount(SchedulesPage)
    await flushPromises()
    expect(wrapper.find('.schedules-empty').exists()).toBe(true)
  })

  it('creates a script task from the dialog', async () => {
    schedulesApi.create.mockResolvedValue(makeTask())
    const wrapper = mount(SchedulesPage, { attachTo: document.body })
    await flushPromises()

    await wrapper.find('.schedules-new').trigger('click')
    await flushPromises()

    const form = document.querySelector('.schedule-form') as HTMLElement
    const inputs = form.querySelectorAll('input.input')
    await new DOMWrapper(inputs[0] as HTMLInputElement).setValue('每小时汇总')
    // selects: [0] target type, [1] target (script), [2] cron preset, ...
    const selects = form.querySelectorAll('select.input')
    await new DOMWrapper(selects[1] as HTMLSelectElement).setValue('sc-1')

    await new DOMWrapper(form.querySelector('.schedule-form-actions .btn-primary') as HTMLButtonElement).trigger('click')
    await flushPromises()

    expect(schedulesApi.create).toHaveBeenCalledWith('p-1', expect.objectContaining({
      name: '每小时汇总',
      target_type: 'script',
      script_id: 'sc-1',
      spec_slug: null,
      agent_slug: null,
      cron_expr: '0 9 * * *',
      timezone: 'Asia/Shanghai',
      enabled: true,
    }))
    wrapper.unmount()
  })

  it('toggles a task through the API and reloads the list', async () => {
    schedulesApi.list.mockResolvedValue([makeTask()])
    schedulesApi.update.mockResolvedValue(makeTask({ enabled: false }))
    const wrapper = mount(SchedulesPage)
    await flushPromises()

    await wrapper.find('.schedule-switch input').setValue(false)
    await flushPromises()

    expect(schedulesApi.update).toHaveBeenCalledWith('s-1', { enabled: false })
    // Reload keeps the UI in sync with the server's recomputed next_run_at.
    expect(schedulesApi.list).toHaveBeenCalledTimes(2)
  })

  it('expands run history and offers a log link only for script runs', async () => {
    schedulesApi.list.mockResolvedValue([makeTask()])
    schedulesApi.runs.mockResolvedValue([
      {
        run_id: 'r-1', schedule_id: 's-1', trigger: 'schedule', status: 'completed',
        script_run_id: 'sr-1', conversation_id: null, summary: 'ok', error: null,
        started_at: null, completed_at: null, created_at: '2026-09-09T01:00:00+00:00',
      },
      {
        run_id: 'r-2', schedule_id: 's-1', trigger: 'schedule', status: 'skipped',
        script_run_id: null, conversation_id: null, summary: null, error: 'previous run still active',
        started_at: null, completed_at: null, created_at: '2026-09-08T01:00:00+00:00',
      },
    ])
    const wrapper = mount(SchedulesPage)
    await flushPromises()

    await wrapper.findAll('.schedule-card-actions-left button')[1].trigger('click')
    await flushPromises()

    expect(schedulesApi.runs).toHaveBeenCalledWith('s-1')
    const rows = wrapper.findAll('.schedule-run-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('成功')
    expect(rows[0].find('button').exists()).toBe(true)
    expect(rows[1].text()).toContain('previous run still active')
    expect(rows[1].find('button').exists()).toBe(false)
  })

  it('fires a manual run', async () => {
    schedulesApi.list.mockResolvedValue([makeTask()])
    schedulesApi.runNow.mockResolvedValue({ run_id: 'r-9', status: 'pending' })
    const wrapper = mount(SchedulesPage)
    await flushPromises()

    await wrapper.findAll('.schedule-card-actions-left button')[0].trigger('click')
    await flushPromises()

    expect(schedulesApi.runNow).toHaveBeenCalledWith('s-1')
    wrapper.unmount()
  })
})
