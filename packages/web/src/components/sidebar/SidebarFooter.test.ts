import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SidebarFooter from './SidebarFooter.vue'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

const authStore = vi.hoisted(() => ({
  displayName: 'Test User',
  activeUsersCount: 1,
  logout: vi.fn(),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => authStore }))

vi.mock('@/stores/tour', () => ({ useTourStore: () => ({ start: vi.fn() }) }))

describe('SidebarFooter', () => {
  it('no longer offers the publish-settings entry', () => {
    const wrapper = mount(SidebarFooter)
    const publishBtn = wrapper.findAll('button').find(b =>
      b.attributes('title') === '服务发布' || b.attributes('title') === 'Publish service',
    )
    expect(publishBtn).toBeUndefined()
  })

  it('keeps the remaining entries and token-settings navigation', async () => {
    const wrapper = mount(SidebarFooter)
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBeGreaterThanOrEqual(4) // language / tour / settings / logout

    const settingsBtn = buttons.find(b => b.attributes('title') === '密钥配置')
    expect(settingsBtn).toBeDefined()
    await settingsBtn!.trigger('click')
    expect(router.push).toHaveBeenCalledWith('/settings/tokens')
  })
})
