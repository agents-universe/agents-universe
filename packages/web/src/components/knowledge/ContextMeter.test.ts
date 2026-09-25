import { describe, it, expect, beforeEach } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import ContextMeter from './ContextMeter.vue'

function setup(occupancy: number, window: number | null, budget = 128_000) {
  setActivePinia(createPinia())
  const convStore = useConversationStore()
  convStore.startConversation('c-1')
  convStore.setTokens(500_000, budget, 'c-1') // lifetime billing — must NOT drive the meter
  convStore.setContextOccupancy(occupancy, window, 'c-1')
  const wrapper = mount(ContextMeter)
  return { wrapper, convStore }
}

describe('ContextMeter', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it('renders occupancy / provider window as the fraction', async () => {
    const { wrapper } = setup(18_400, 200_000)
    await nextTick()
    expect(wrapper.find('.context-meter-fraction').text()).toBe('18,400 / 200,000')
  })

  it('falls back to tokenBudget when no window has been reported', async () => {
    const { wrapper } = setup(0, null, 128_000)
    await nextTick()
    expect(wrapper.find('.context-meter-fraction').text()).toBe('0 / 128,000')
  })

  it('ignores the billing ledger even when it exceeds the budget', async () => {
    // 500k lifetime spent on a 128k budget — meter must still read blue
    // from the real occupancy (10% of a 200k window).
    const { wrapper } = setup(20_000, 200_000, 128_000)
    await nextTick()
    expect(wrapper.find('.context-meter-fraction').text()).toBe('20,000 / 200,000')
    expect(wrapper.find('.context-meter-fill').classes()).toContain('fill-blue')
  })

  it('turns amber above 75% and red above 90%', async () => {
    const amber = setup(160_000, 200_000)
    await nextTick()
    expect(amber.wrapper.find('.context-meter-fill').classes()).toContain('fill-amber')

    const red = setup(190_000, 200_000)
    await nextTick()
    expect(red.wrapper.find('.context-meter-fill').classes()).toContain('fill-red')
  })

  it('exactly 75% is amber — the documented 75–90 band is inclusive at 75', async () => {
    const boundary = setup(150_000, 200_000)
    await nextTick()
    expect(boundary.wrapper.find('.context-meter-fill').classes()).toContain('fill-amber')
  })

  it('renders the context_usage breakdown row when present', async () => {
    const { wrapper, convStore } = setup(10_000, 200_000)
    convStore.setContextUsage(
      {
        staticFiles: 3,
        dynamicFiles: 1,
        deferredFiles: 0,
        overflowFiles: 0,
        conversationHistoryTokens: 4_200,
        pendingTaskTokens: 0,
        totalBudget: 100_000,
      },
      'c-1',
    )
    await nextTick()
    expect(wrapper.find('.context-meter-breakdown').exists()).toBe(true)
    expect(wrapper.find('.context-meter-breakdown').text()).toContain('4200')
  })
})
