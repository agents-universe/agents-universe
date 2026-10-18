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

  it('saves a typed main token when only an extra-field project secret exists', async () => {
    // The service is already project-configured through "jira:email" alone.
    // Gating the project branch on the main secret ran neither branch and
    // still reported "saved": the typed token never reached the server.
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('jira:email')])
    secretsMock.createProjectSecret.mockResolvedValue({
      secret_id: 's-jira',
      service_key: 'jira',
      key_hint: '...ken',
    })
    const wrapper = await mountOnIntegrationsTab()

    const jiraRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))!
    expect(jiraRow.find('.scope-badge.project-scope').exists()).toBe(true)

    // Project scope hides base_url: main key + jira:email.
    const inputs = jiraRow.findAll('.token-edit-block input')
    expect(inputs).toHaveLength(2)
    await inputs[0].setValue('new-jira-token')
    await jiraRow.find('.token-row-actions button').trigger('click')
    await flushPromises()

    expect(secretsMock.createProjectSecret).toHaveBeenCalledWith('p1', {
      service_key: 'jira',
      value: 'new-jira-token',
    })
    expect(secretsMock.updateProjectSecret).not.toHaveBeenCalled()
    // The row is re-rendered after the save, so query it again rather than
    // asserting against the detached pre-click node.
    const savedRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))!
    expect(savedRow.text()).toContain('✓')
    expect(savedRow.text()).toContain('...ken')
  })

  it('cancelling an add leaves no extra-field values behind', async () => {
    secretsMock.listProjectSecrets.mockResolvedValue([])
    const wrapper = await mountOnIntegrationsTab()

    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const select = wrapper.find('.token-add-form select')
    await select.setValue('jira')
    const extra = wrapper.find('.token-add-form .token-url-input')
    await extra.setValue('someone@example.com')
    // Cancel is the third action (add / test / cancel).
    await wrapper.findAll('.token-add-form .token-row-actions button')[2].trigger('click')
    await nextTick()

    // Re-open and pick the same service: the old email must not pre-fill.
    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    await wrapper.find('.token-add-form select').setValue('jira')
    expect(
      (wrapper.find('.token-add-form .token-url-input').element as HTMLInputElement).value,
    ).toBe('')
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

  it('keeps the typed token after a project-scope failure so the retry writes it', async () => {
    // Scope 'both': the user-token branch used to clear the input before the
    // project_secrets write ran. When that write failed, the retry saw an
    // empty input, ran neither branch, and still reported "saved" — the
    // project copy was never written.
    apiMock.apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return Promise.resolve({})
      if (url === '/api/tokens') {
        return Promise.resolve([{ service_key: 'git', key_hint: '...old', base_url: null }])
      }
      if (url === '/api/tokens/git' && options?.method === 'PUT') {
        return Promise.resolve({ key_hint: '...new' })
      }
      if (url.endsWith('/mcp-servers')) return Promise.resolve([])
      return Promise.resolve({})
    })
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('git')])
    secretsMock.updateProjectSecret
      .mockRejectedValueOnce(new Error('project vault down'))
      .mockResolvedValue({ secret_id: 's-git', key_hint: '...new' })

    const wrapper = await mountOnIntegrationsTab()
    const row = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    // 'both' scope shows base_url plus the main token input.
    const inputs = row.findAll('.token-edit-block input')
    await inputs[1].setValue('new-token')
    await row.find('.token-row-actions button').trigger('click')
    await flushPromises()
    expect(secretsMock.updateProjectSecret).toHaveBeenCalledTimes(1)

    // Retry without retyping: the input must still hold the value.
    const retryRow = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    const retryInputs = retryRow.findAll('.token-edit-block input')
    expect((retryInputs[1].element as HTMLInputElement).value).toBe('new-token')
    await retryRow.find('.token-row-actions button').trigger('click')
    await flushPromises()

    expect(secretsMock.updateProjectSecret).toHaveBeenCalledTimes(2)
    expect(secretsMock.updateProjectSecret).toHaveBeenLastCalledWith('p1', 's-git', {
      value: 'new-token',
    })
    // Re-query: the row re-rendered after the save, so the pre-click wrapper
    // may still show the earlier failure message.
    const savedRow = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    expect(savedRow.text()).toContain('✓')
  })
})

describe('TokenConfigModal add-model form', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubApi()
    useProjectStore().currentProject = makeProject('p1')
  })

  it('cancel wipes the abandoned add-model values', async () => {
    // Cancel used to only hide the form: provider, model id and the API key
    // stayed behind and silently seeded the next add attempt.
    const wrapper = mount(TokenConfigModal, {
      global: { stubs: { teleport: true } },
    })
    await flushPromises()

    await wrapper.find('.add-model-btn').trigger('click')
    const form = wrapper.find('.token-add-form')
    await form.find('select').setValue('openai')
    const inputs = form.findAll('input')
    await inputs[0].setValue('gpt-5')
    await inputs[1].setValue('sk-secret')
    // add / test / cancel
    await form.findAll('.token-row-actions button')[2].trigger('click')
    await nextTick()

    await wrapper.find('.add-model-btn').trigger('click')
    const reopened = wrapper.find('.token-add-form')
    const reopenedInputs = reopened.findAll('input')
    expect((reopenedInputs[0].element as HTMLInputElement).value).toBe('')
    expect((reopenedInputs[1].element as HTMLInputElement).value).toBe('')
    expect((reopened.find('select').element as HTMLSelectElement).value).toBe('')
  })
})
