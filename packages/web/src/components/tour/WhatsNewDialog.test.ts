import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { i18n } from '@/i18n'
import { patchPreferences } from '@/api/preferences'
import { useTourStore } from '@/stores/tour'
import WhatsNewDialog from './WhatsNewDialog.vue'

vi.mock('@/api/preferences', () => ({ patchPreferences: vi.fn() }))
vi.mock('@/tour/releases', () => ({
  RELEASES: [
    { version: '0.2.0', date: '2026-08-01', titleKey: 'whatsNew.t_0_2_0.title', featureKeys: ['whatsNew.t_0_2_0.f1'] },
    { version: '0.3.0', date: '2026-08-10', titleKey: 'whatsNew.t_0_3_0.title', featureKeys: ['whatsNew.t_0_3_0.f1', 'whatsNew.t_0_3_0.f2'] },
  ],
  CURRENT_VERSION: '0.3.0',
}))

const mockPatch = vi.mocked(patchPreferences)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  // i18n keys for the mocked release manifest (test-env pins zh-CN)
  i18n.global.mergeLocaleMessage('zh-CN', {
    whatsNew: {
      t_0_2_0: { title: '发布二', f1: '功能二一' },
      t_0_3_0: { title: '发布三', f1: '功能三一', f2: '功能三二' },
    },
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('WhatsNewDialog', () => {
  it('renders unseen entries newest first with their features', () => {
    const tour = useTourStore()
    tour.lastSeenVersion = '0.1.0'
    tour.whatsNewVisible = true

    mount(WhatsNewDialog) // content is teleported to <body>
    const text = document.body.textContent ?? ''
    expect(text).toContain('发布三')
    expect(text).toContain('功能三一')
    expect(text).toContain('功能三二')
    expect(text).toContain('发布二')
    expect(text).toContain('功能二一')
    // newest entry is listed before the older one
    expect(text.indexOf('发布三')).toBeLessThan(text.indexOf('发布二'))
  })

  it('shows the caught-up state when nothing is newer', () => {
    const tour = useTourStore()
    tour.lastSeenVersion = '0.3.0'
    tour.whatsNewVisible = true

    mount(WhatsNewDialog)
    const text = document.body.textContent ?? ''
    expect(text).toContain('已是最新版本')
    expect(text).not.toContain('发布二')
  })

  it('close patches last_seen_version and hides the dialog', async () => {
    const tour = useTourStore()
    tour.whatsNewVisible = true

    mount(WhatsNewDialog)
    const closeBtn = document.body.querySelector<HTMLButtonElement>('.whats-new-dialog .btn-primary')
    expect(closeBtn).not.toBeNull()
    closeBtn!.click()

    expect(mockPatch).toHaveBeenCalledWith({ last_seen_version: '0.3.0' })
    expect(tour.whatsNewVisible).toBe(false)
  })
})
