/**
 * Deleting a "both"-scoped integration removes a user token and a project
 * secret. When the second call failed, the first row was already gone, so
 * every retry hit a 404 on it and aborted before reaching the project secret —
 * the integration could never be cleaned up from the UI. A 404 now counts as
 * "already deleted".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TokenConfigModal from '../TokenConfigModal.vue'
import { ApiError } from '@/api/client'

const apiMock = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, apiFetch: apiMock.apiFetch }
})

const secretsMock = vi.hoisted(() => ({
  listProjectSecrets: vi.fn(),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))
vi.mock('@/api/projectSecrets', () => secretsMock)

vi.mock('@/stores/agent', () => ({
  AUTO_MODEL_CONFIG_ID: 'auto',
  useAgentStore: () => ({
    modelConfigs: [],
    fetchModelConfigs: vi.fn(async () => {}),
  }),
}))

const projectState = vi.hoisted(() => ({
  currentProject: { project_id: 'p-1' } as Record<string, unknown> | null,
  ensureCurrentProject: vi.fn(async () => {}),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectState }))

const userTokens = [
  { service_key: 'git', display_name: null, key_hint: '...abc', base_url: null, model_id: null },
]
const projectSecret = {
  secret_id: 'sec-git',
  service_key: 'git',
  environment: null,
  secret_name: 'git',
  display_name: null,
  key_hint: '...xyz',
  created_by: 'u-1',
  updated_at: null,
}

describe('TokenConfigModal both-scope delete', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    projectState.currentProject = { project_id: 'p-1' }
    secretsMock.listProjectSecrets.mockResolvedValue([projectSecret])
    let userDeletes = 0
    apiMock.apiFetch.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return {}
      if (url === '/api/tokens' && !options?.method) return userTokens
      if (url === '/api/projects/p-1/mcp-servers') return []
      if (url === '/api/tokens/git' && options?.method === 'DELETE') {
        userDeletes++
        // The first attempt deletes the user row; the retry finds it gone.
        if (userDeletes === 1) return { deleted: 'git' }
        throw new ApiError(404, 'Token not found')
      }
      throw new Error(`unexpected request: ${options?.method ?? 'GET'} ${url}`)
    })
  })

  it('retries the project secret after the user token is already gone', async () => {
    // First attempt: user delete succeeds, project delete fails.
    secretsMock.deleteProjectSecret
      .mockRejectedValueOnce(new ApiError(500, 'db down'))
      .mockResolvedValue(undefined)

    const wrapper = mount(TokenConfigModal, { global: { stubs: { teleport: true } } })
    await flushPromises()
    await wrapper.findAll('.token-tab')[1].trigger('click')
    await flushPromises()

    const row = wrapper.find('.token-service-row')
    expect(row.exists()).toBe(true)
    expect(row.find('.scope-badge.user-scope').exists()).toBe(true)
    expect(row.find('.scope-badge.project-scope').exists()).toBe(true)

    await row.find('.btn-sm.danger').trigger('click')
    await flushPromises()

    // Partial failure surfaces, and the row stays visible.
    expect(wrapper.find('.token-service-row').exists()).toBe(true)
    expect(wrapper.find('.token-inline-msg.error').exists()).toBe(true)
    expect(secretsMock.deleteProjectSecret).toHaveBeenCalledTimes(1)

    // Retry: the user-token DELETE 404s (already removed) but must not abort
    // the project-secret cleanup or leave the row stuck in the UI.
    await wrapper.find('.token-service-row .btn-sm.danger').trigger('click')
    await flushPromises()

    expect(secretsMock.deleteProjectSecret).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.token-service-row').exists()).toBe(false)

    wrapper.unmount()
  })
})
