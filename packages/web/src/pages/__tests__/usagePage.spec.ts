import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'
import UsagePage from '@/pages/UsagePage.vue'
import type { UsageDashboard } from '@/api/usage'

const route = vi.hoisted(() => ({ params: { projectId: 'p-1' } as Record<string, unknown> }))
vi.mock('vue-router', () => ({ useRoute: () => route }))

const usageApi = vi.hoisted(() => ({ getUsage: vi.fn() }))
vi.mock('@/api/usage', () => ({ usageApi }))

function makeDaily(count: number): UsageDashboard['daily'] {
  return Array.from({ length: count }, (_, i) => ({
    date: `2026-10-${String(i + 1).padStart(2, '0')}`,
    tokens: i === 0 ? 4000 : i === 1 ? 1000 : 0,
    run_count: i === 0 ? 4 : i === 1 ? 1 : 0,
  }))
}

function makeUsage(over: Partial<UsageDashboard> = {}): UsageDashboard {
  return {
    days: 30,
    since: '2026-10-01T00:00:00+00:00',
    generated_at: '2026-10-30T00:00:00+00:00',
    summary: {
      total_tokens: 5500,
      run_count: 5,
      user_count: 2,
      conversation_count: 2,
    },
    top_users: [
      { rank: 1, user_id: 'alice', display_name: 'Alice Zhang', tokens: 4000, run_count: 4, conversation_count: 1 },
      { rank: 2, user_id: 'bob', display_name: null, tokens: 1500, run_count: 1, conversation_count: 1 },
    ],
    daily: makeDaily(30),
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  route.params = reactive({ projectId: 'p-1' })
  usageApi.getUsage.mockResolvedValue(makeUsage())
})

describe('UsagePage', () => {
  it('renders summary tiles, ranked bars and the trend after load', async () => {
    const wrapper = mount(UsagePage)
    await flushPromises()

    expect(wrapper.findAll('.usage-tile')).toHaveLength(4)
    expect(wrapper.find('.usage-tile-value').text()).toBe('5.5K')
    expect(wrapper.find('.usage-tile').attributes('title')).toBe('5,500')

    const rows = wrapper.findAll('.usage-bar-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].find('.usage-rank').text()).toBe('1')
    expect(rows[1].find('.usage-rank').text()).toBe('2')
    expect(rows[0].find('.usage-user-id').text()).toBe('Alice Zhang')
    // Tooltip keeps the raw id reachable when a display name is shown.
    expect(rows[0].attributes('title')).toContain('Alice Zhang (alice)')
    // Unknown name → raw id, both in the row and the tooltip.
    expect(rows[1].find('.usage-user-id').text()).toBe('bob')
    expect(rows[1].attributes('title')).toContain('bob')

    // The leader fills the track; the runner-up is proportional (1500/4000).
    expect(rows[0].find('.usage-bar-fill').attributes('style')).toContain('width: 100%')
    expect(rows[1].find('.usage-bar-fill').attributes('style')).toContain('width: 37.5%')
    expect(rows[0].find('.usage-bar-value').text()).toBe('4K')
    expect(rows[0].find('.usage-bar-value').attributes('title')).toBe('4,000')

    // One hit target per day + a table twin row per day (no hover-only data).
    expect(wrapper.findAll('.usage-hit')).toHaveLength(30)
    expect(wrapper.findAll('.usage-col')).toHaveLength(2) // two non-zero days
    expect(wrapper.findAll('.usage-table tbody tr')).toHaveLength(30)
  })

  it('switching the range refetches with the new days param', async () => {
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(usageApi.getUsage).toHaveBeenCalledWith('p-1', 30)

    await wrapper.findAll('.usage-range button')[0].trigger('click')
    await flushPromises()
    expect(usageApi.getUsage).toHaveBeenLastCalledWith('p-1', 7)
    expect((usageApi.getUsage.mock.calls.at(-1) as unknown[])[1]).toBe(7)
  })

  it('shows the empty state when nothing was consumed', async () => {
    usageApi.getUsage.mockResolvedValue(
      makeUsage({
        summary: { total_tokens: 0, run_count: 0, user_count: 0, conversation_count: 0 },
        top_users: [],
        daily: makeDaily(30).map(d => ({ ...d, tokens: 0, run_count: 0 })),
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()

    expect(wrapper.find('.empty-state').exists()).toBe(true)
    expect(wrapper.findAll('.usage-bar-row')).toHaveLength(0)
    expect(wrapper.find('.usage-tile').exists()).toBe(false)
  })

  it('shows the error state with retry when the first load fails', async () => {
    usageApi.getUsage.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mount(UsagePage)
    await flushPromises()

    const msg = wrapper.find('.usage-message.error')
    expect(msg.exists()).toBe(true)
    expect(msg.text()).toContain('boom')

    usageApi.getUsage.mockResolvedValue(makeUsage())
    await msg.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.usage-bar-row').exists()).toBe(true)
    expect(wrapper.find('.usage-message.error').exists()).toBe(false)
  })

  it('holds the previous render dimmed while a range switch is in flight', async () => {
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.find('.usage-content.refreshing').exists()).toBe(false)

    let resolve!: (v: UsageDashboard) => void
    usageApi.getUsage.mockReturnValue(new Promise<UsageDashboard>(r => { resolve = r }))
    await wrapper.findAll('.usage-range button')[2].trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.usage-content.refreshing').exists()).toBe(true)
    // Previous data still in the DOM — no skeleton flash.
    expect(wrapper.findAll('.usage-bar-row')).toHaveLength(2)

    resolve(makeUsage())
    await flushPromises()
    expect(wrapper.find('.usage-content.refreshing').exists()).toBe(false)
  })
})
