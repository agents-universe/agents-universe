import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import TokenConfigModal from './TokenConfigModal.vue'
import { useProjectStore } from '@/stores/project'
import type { Project, ProjectSecret } from '@/types'

const apiMock = vi.hoisted(() => ({ apiFetch: vi.fn() }))
const secretsMock = vi.hoisted(() => ({
  listProjectSecrets: vi.fn(),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))
const agentsMock = vi.hoisted(() => ({
  getAgents: vi.fn(),
  getModelConfigs: vi.fn(),
  syncAgents: vi.fn(),
}))

vi.mock('@/api/client', () => ({ apiFetch: apiMock.apiFetch }))
vi.mock('@/api/projectSecrets', () => secretsMock)
vi.mock('@/api/agents', () => ({ agentsApi: agentsMock }))
vi.mock('@/api/modelConfigs', () => ({
  modelConfigsApi: { testConnection: vi.fn() },
}))

function makeProject(id: string): Project {
  return {
    project_id: id,
    slug: id,
    display_name: id,
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category: 'default',
    created_by: 'u-1',
    visibility: 'private',
    is_owner: true,
    can_manage: true,
  }
}

function makeSecret(serviceKey: string): ProjectSecret {
  return {
    secret_id: `s-${serviceKey}`,
    service_key: serviceKey,
    environment: null,
    secret_name: serviceKey.toUpperCase().replace(':', '_'),
    display_name: null,
    key_hint: 'you@example.com',
    created_by: 'u-1',
    updated_at: null,
  }
}

function stubApi() {
  apiMock.apiFetch.mockImplementation((url: string) => {
    if (url === '/api/integrations/defaults') return Promise.resolve({})
    if (url === '/api/tokens') return Promise.resolve([])
    if (url.endsWith('/mcp-servers')) return Promise.resolve([])
    return Promise.resolve({})
  })
  agentsMock.getModelConfigs.mockResolvedValue([])
}

async function mountOnIntegrationsTab() {
  const wrapper = mount(TokenConfigModal, {
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  await nextTick()
  await wrapper.findAll('.token-tab')[1].trigger('click')
  await nextTick()
  return wrapper
}

describe('TokenConfigModal integration scope detection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubApi()
    useProjectStore().currentProject = makeProject('p1')
  })

  it('an extra-field-only project secret marks the service configured in project scope', async () => {
    // An agent may save only "jira:email" into project_secrets, with no
    // "jira" row. The service is still configured project-side: hiding it
    // would offer to add it again and write the typed token to user_tokens.
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('jira:email')])
    const wrapper = await mountOnIntegrationsTab()

    const jiraRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))
    expect(jiraRow).toBeDefined()
    expect(jiraRow!.find('.scope-badge.project-scope').exists()).toBe(true)

    // The "add integration" picker must not offer Jira a second time.
    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const offered = wrapper.findAll('.token-add-form option').map((o) => o.text())
    expect(offered).not.toContain('Jira')
    expect(offered).toContain('GitHub / Git')
  })

  it('no secret at all leaves the service in the add picker', async () => {
    secretsMock.listProjectSecrets.mockResolvedValue([])
    const wrapper = await mountOnIntegrationsTab()

    const rows = wrapper.findAll('.token-service-label').map((n) => n.text())
    expect(rows).not.toContain('Jira')

    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const offered = wrapper.findAll('.token-add-form option').map((o) => o.text())
    expect(offered).toContain('Jira')
  })
})
