/**
 * Integration scope detection.
 *
 * A service may have user_tokens rows for its extra fields without the main
 * key — the secret vault saves "jira:email" on its own (the tool's own example
 * key). Such a service IS user-configured: labelling it "project" used to hide
 * the shared fields and, worse, make saveIntegration run neither branch —
 * reporting "saved" for a token that was silently dropped.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TokenConfigModal from '../TokenConfigModal.vue'

const apiMock = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/client', () => ({ apiFetch: apiMock.apiFetch }))

vi.mock('@/api/projectSecrets', () => ({
  listProjectSecrets: vi.fn(async () => []),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))

const agentState = vi.hoisted(() => ({
  modelConfigs: [] as Array<Record<string, unknown>>,
  fetchModelConfigs: vi.fn(async () => {}),
}))
vi.mock('@/stores/agent', () => ({
  AUTO_MODEL_CONFIG_ID: 'auto',
  useAgentStore: () => agentState,
}))

const projectState = vi.hoisted(() => ({
  currentProject: { project_id: 'p-1' } as Record<string, unknown> | null,
  ensureCurrentProject: vi.fn(async () => {}),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectState }))

/** User tokens: only Jira's email field has ever been saved. */
const userTokens = [
  {
    service_key: 'jira:email',
    display_name: null,
    key_hint: '...ail',
    base_url: null,
    model_id: null,
  },
]

async function mountModal() {
  apiMock.apiFetch.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url === '/api/integrations/defaults') return {}
    if (url === '/api/tokens') return userTokens
    if (url === '/api/tokens/jira' && options?.method === 'PUT') return { key_hint: '...ken' }
    if (url === '/api/projects/p-1/mcp-servers') return []
    throw new Error(`unexpected request: ${options?.method ?? 'GET'} ${url}`)
  })

  const wrapper = mount(TokenConfigModal, {
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  // Switch to the integrations tab.
  await wrapper.findAll('.token-tab')[1].trigger('click')
  await flushPromises()
  return wrapper
}

describe('TokenConfigModal integration scopes', () => {
  beforeEach(() => {
    apiMock.apiFetch.mockReset()
    projectState.currentProject = { project_id: 'p-1' }
  })

  it('treats an extra-field user token as user-configured and saves a new main key', async () => {
    const wrapper = await mountModal()

    const row = wrapper.find('.token-service-row')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Jira')
    // The email row alone means user-scoped credentials exist.
    expect(row.find('.scope-badge.user-scope').exists()).toBe(true)
    expect(row.find('.scope-badge.project-scope').exists()).toBe(false)

    const inputs = row.findAll('.token-edit-block input')
    // base_url (user scope shows it) + main key + jira:email
    expect(inputs).toHaveLength(3)

    await inputs[1].setValue('new-jira-token')
    await row.find('.token-row-actions button').trigger('click')
    await flushPromises()

    const put = apiMock.apiFetch.mock.calls.find(
      ([url, options]) => url === '/api/tokens/jira' && options?.method === 'PUT',
    )
    expect(put).toBeTruthy()
    expect(JSON.parse(put![1].body as string)).toMatchObject({
      value: 'new-jira-token',
    })
    expect(wrapper.text()).toContain('✓')
  })
})
