import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { i18n, setLocale } from '@/i18n'
import TourTooltipCard from './TourTooltipCard.vue'
import type { TourStep } from '@/tour/steps'

const tour = vi.hoisted(() => ({
  stepIndex: 0,
  waiting: false,
  activeSteps: [{}, {}] as unknown[],
  next: vi.fn(),
  skip: vi.fn(),
  finish: vi.fn(),
}))
vi.mock('@/stores/tour', () => ({ useTourStore: () => tour }))

const mockPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
vi.mock('@/api/preferences', () => ({ patchPreferences: mockPatch }))

const LANGUAGE_STEP: TourStep = {
  id: 'language',
  titleKey: 'tour.language.title',
  bodyKey: 'tour.language.body',
  center: true,
  choices: [
    { label: '中文', value: 'zh-CN' },
    { label: 'English', value: 'en-US' },
  ],
}

afterEach(() => {
  setLocale('zh-CN')
  vi.clearAllMocks()
})

describe('TourTooltipCard', () => {
  it('renders choice buttons for a step with choices', () => {
    const wrapper = mount(TourTooltipCard, { props: { step: LANGUAGE_STEP }})
    const buttons = wrapper.findAll('.tour-choice-btn')
    expect(buttons.map((b) => b.text())).toEqual(['中文', 'English'])
    // Current locale (zh-CN) is highlighted.
    expect(buttons[0].classes()).toContain('tour-choice-btn--active')
    expect(buttons[1].classes()).not.toContain('tour-choice-btn--active')
  })

  it('clicking a choice switches the locale, persists it, and advances', async () => {
    const wrapper = mount(TourTooltipCard, { props: { step: LANGUAGE_STEP }})
    await wrapper.findAll('.tour-choice-btn')[1].trigger('click')
    expect(i18n.global.locale.value).toBe('en-US')
    expect(mockPatch).toHaveBeenCalledWith({ locale: 'en-US' })
    expect(tour.next).toHaveBeenCalled()
  })

  it('steps without choices render no choice buttons', () => {
    const step: TourStep = { id: 'welcome', titleKey: 'tour.welcome.title', bodyKey: 'tour.welcome.body', center: true }
    const wrapper = mount(TourTooltipCard, { props: { step }})
    expect(wrapper.find('.tour-choices').exists()).toBe(false)
  })

  it('the final step of the ACTIVE tour renders 完成 and calls finish', async () => {
    // isLast derives from the active tour's own length (mocked: 2 steps).
    tour.stepIndex = 1
    const step: TourStep = { id: 'welcome', titleKey: 'tour.welcome.title', bodyKey: 'tour.welcome.body', center: true }
    const wrapper = mount(TourTooltipCard, { props: { step }})
    const next = wrapper.find('.tour-next-btn')
    expect(next.text()).toBe('完成')
    await next.trigger('click')
    expect(tour.finish).toHaveBeenCalled()
    tour.stepIndex = 0
  })
})
